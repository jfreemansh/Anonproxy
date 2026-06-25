"""
The engine: detect -> vault -> replace (anonymize) and the tolerant reverse
(deanonymize).  Sync API; the proxy calls it from a thread.

Three things make this more reliable than match/replace:

* **Consistency rescan** — every value the vault has *ever* seen this engagement
  is re-detected in new text, so an entity caught once (by regex or the LLM) is
  caught every time afterwards, even if a layer would miss it on its own.
* **Single-pass replacement** — all originals are replaced in one left-to-right
  regex scan (longest first), so substitutions can't cascade into each other.
* **Tolerant restoration** — see ``restorer.py``.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

from .config import Settings
from .vault import Vault
from .detectors import Match, LLMDetector, build_detectors
from .restorer import TolerantRestorer, StreamRestorer

log = logging.getLogger("anonproxy.engine")

# Structured types where regex classification beats the LLM's guess.
_REGEX_WINS = frozenset({
    "TOKEN", "HASH", "CREDENTIAL", "IDENTIFIER", "MAC_ADDRESS", "IP_ADDRESS",
    "CIDR", "URL", "DOMAIN", "HOSTNAME", "EMAIL_ADDRESS", "PATH", "PAYMENT_CARD",
})

# A contextual entity that is just "<label>=<value>" / "<label>: <value>" wrapping
# a precise floor entity (e.g. the model grabbed "PHPSESSID=<token>" while regex
# already captured "<token>"). Matches the leftover label/separator after removing
# the inner value — so we can drop the wrapper and keep structure (cookie names,
# "password:" prefixes) while still anonymizing the value.
_LABELISH = re.compile(r'^[\w.\-]{0,24}\s*["\']?\s*[:=]?\s*["\']?$')

# Things that must never be anonymized — they carry no client identity and the
# AI needs them to give useful, version-specific advice.
_SAFE: frozenset[str] = frozenset(w.lower() for w in {
    "nmap", "metasploit", "mimikatz", "bloodhound", "crackmapexec", "impacket",
    "hashcat", "john", "responder", "certipy", "rubeus", "secretsdump", "netexec",
    "evil-winrm", "wireshark", "burpsuite", "sqlmap", "nuclei", "ffuf", "gobuster",
    "smb", "ldap", "kerberos", "ntlm", "http", "https", "ftp", "ssh", "rdp", "dns",
    "smtp", "imap", "snmp", "nfs", "rpc", "winrm", "krbtgt",
    "apache", "nginx", "iis", "tomcat", "openssh", "openssl", "mysql", "postgres",
    "postgresql", "mariadb", "mssql", "mongodb", "redis", "samba", "exchange",
    "windows", "linux", "ubuntu", "debian", "centos", "kali", "fedora",
    "microsoft", "docker", "kubernetes", "git", "github", "gitlab", "jenkins",
    "domain users", "domain admins", "enterprise admins", "administrators",
    "localhost", "true", "false", "null", "none",
})


class Engine:
    def __init__(self, engagement: Optional[str] = None,
                 settings: Optional[Settings] = None,
                 vault: Optional[Vault] = None):
        self.settings = settings or Settings()
        if engagement:
            self.settings.engagement_id = engagement
        self.vault = vault or Vault(self.settings)
        self.detectors = build_detectors(self.settings)
        # convenience handle to the Ollama backend (if configured) for status/verify
        self.llm = next((d for d in self.detectors if d.name == "ollama"),
                        LLMDetector(self.settings))
        self.restorer = TolerantRestorer(tolerant=self.settings.restore_tolerant)

    # -- detection ----------------------------------------------------------
    def _detect(self, text: str, contextual: bool) -> dict[str, str]:
        entities: dict[str, str] = {}
        from_regex: set[str] = set()

        # 1) deterministic floor (regex + scope seed) — trusted, wins for type
        for det in self.detectors:
            if not getattr(det, "floor", False):
                continue
            try:
                matches = det.detect(text)
            except Exception as e:
                log.warning("floor detector %r failed: %s", det.name, e)
                continue
            for m in matches:
                entities[m.text] = m.entity_type
                from_regex.add(m.text)

        # 2) contextual backends (ollama / gliner / piiranha / anonymizer-slm)
        if contextual:
            for det in self.detectors:
                if getattr(det, "floor", False):
                    continue
                try:
                    if not det.available():
                        continue
                    matches = det.detect(text)
                except Exception as e:
                    log.warning("detector %r failed: %s", det.name, e)
                    continue
                for m in matches:
                    if entities.get(m.text) in _REGEX_WINS:
                        continue   # regex type is more precise for structured data
                    entities[m.text] = m.entity_type

        # 3) consistency rescan: anything the vault already knows, re-catch it now
        for original, etype in self.vault.known_originals():
            if original in text and original not in entities:
                entities[original] = etype

        # 4) prefer precise floor spans over a broader wrapper a model produced
        #    ("PHPSESSID=<tok>" -> keep "<tok>", preserve the cookie name).
        for word in list(entities):
            if word in from_regex:
                continue
            for fe in from_regex:
                if len(fe) >= 6 and fe in word and fe != word \
                        and _LABELISH.match(word.replace(fe, "", 1)):
                    del entities[word]
                    break

        return self._filter(entities, from_regex)

    @staticmethod
    def _filter(entities: dict[str, str], from_regex: set[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for word, etype in entities.items():
            w = word.strip()
            if not w:
                continue
            lower = w.lower()
            tokens = w.split()
            # drop allowlisted / too-short single tokens (unless regex found them)
            if w not in from_regex:
                if len(tokens) == 1 and len(w) < 4:
                    continue
                if lower in _SAFE:
                    continue
                first = lower.split()[0]
                is_proper = len(tokens) >= 2 and all(
                    t[:1].isupper() for t in tokens if t
                )
                if not is_proper and first in _SAFE:
                    continue
            out[word] = etype
        return out

    # -- public: anonymize --------------------------------------------------
    def anonymize(self, text: str, is_tool_output: bool = True,
                  use_llm: Optional[bool] = None) -> str:
        if not text or not text.strip():
            return text
        contextual = is_tool_output if use_llm is None else use_llm
        entities = self._detect(text, contextual=contextual)
        if not entities:
            return text

        surrogate_map: dict[str, str] = {}
        for original, etype in entities.items():
            surrogate, _ = self.vault.get_or_create(original, etype)
            surrogate_map[original] = surrogate

        # single left-to-right pass, longest original first. Each original is
        # guarded with word boundaries on any alphanumeric edge so a short term
        # (e.g. a scope hostname "acme") is replaced as a whole token and never
        # inside a larger word ("acmespeak").
        def _bounded(o: str) -> str:
            pat = re.escape(o)
            if o[:1].isalnum() or o[:1] == "_":
                pat = r"(?<!\w)" + pat
            if o[-1:].isalnum() or o[-1:] == "_":
                pat = pat + r"(?!\w)"
            return pat

        originals = sorted(surrogate_map, key=len, reverse=True)
        pattern = re.compile("|".join(_bounded(o) for o in originals))
        result = pattern.sub(lambda m: surrogate_map[m.group(0)], text)
        log.info("anonymized %d entities", len(entities))
        return result

    # -- public: deanonymize ------------------------------------------------
    def deanonymize(self, text: str) -> str:
        return self.restorer.restore(text, self.vault.all_mappings())

    def stream_restorer(self) -> StreamRestorer:
        return StreamRestorer(self.vault.all_mappings(),
                              tolerant=self.settings.restore_tolerant)

    # -- introspection ------------------------------------------------------
    def stats(self) -> dict:
        return self.vault.stats()

    def llm_status(self) -> dict:
        return self.llm.model_status()

    def detector_status(self) -> list[dict]:
        """Per-backend availability + detail, for /health and verify."""
        out = []
        for det in self.detectors:
            try:
                st = det.status()
            except Exception as e:
                st = {"available": False, "detail": str(e)}
            out.append({"name": det.name, **st})
        return out

    def contextual_available(self) -> bool:
        """True if any non-regex backend is ready to add contextual recall."""
        for det in self.detectors:
            if det.name == "regex":
                continue
            try:
                if det.available():
                    return True
            except Exception:
                continue
        return False

    def export(self) -> list[dict]:
        return self.vault.export()
