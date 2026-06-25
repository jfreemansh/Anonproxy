"""
Contextual detection via a local Ollama model.

Catches what regex provably cannot: bare hostnames (``DC01``), org and project
names in prose, person names, cleartext credentials without a label, sensitive
file paths.  It is *additive* — if Ollama is down or slow, the engine falls back
to regex + the consistency rescan and keeps working.

Reliability measures baked in here (vs. the original's single-shot approach):

* **Overlapping chunking** so an entity straddling a chunk boundary is still
  seen whole in the neighbouring chunk.
* **Strict JSON contract** with defensive parsing — malformed output yields zero
  entities rather than an exception.
* **Substring verification** — the model must return spans that literally occur
  in the text; hallucinated entities are dropped.
"""
from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

if TYPE_CHECKING:
    from . import Match

log = logging.getLogger("anonproxy.llm")

_SYSTEM = """You are a PII/infrastructure detector for penetration-test data.
Return ONLY a JSON array. Each element: {"text": <exact substring>, "type": <TYPE>}.

Detect and label these context-dependent entities:
- HOSTNAME: bare machine names (DC01, FILESERVER-PRD, web01)
- ORGANIZATION: company / project / product names that identify the client
- PERSON: human names
- USERNAME: account names (jsmith, CORP\\jsmith, first.last)
- CREDENTIAL: cleartext passwords or secrets, even without a label
- PATH: filesystem paths that reveal users, clients, or engagements

Do NOT flag (these must stay so the AI can still help):
- technology/product names & versions (IIS 10, Apache 2.4.49, OpenSSH 8.2)
- CVE ids, tool names (nmap, mimikatz, bloodhound), protocols (SMB, LDAP), ports
- generic English words

Return the exact substring as it appears. If nothing qualifies, return [].
No prose, no markdown, JSON array only."""


class LLMDetector:
    name = "ollama"

    def __init__(self, settings):
        self.settings = settings
        self._available: bool | None = None
        self._model: str | None = None     # effective model actually used
        self._tags: list[str] = []
        self._reason: str = ""

    @staticmethod
    def _match_model(configured: str, installed: list[str]) -> str | None:
        """Resolve the configured model name against what Ollama has pulled.

        Handles the common ``name`` vs ``name:tag`` / ``name:latest`` mismatch.
        """
        if configured in installed:
            return configured
        if f"{configured}:latest" in installed:
            return f"{configured}:latest"
        base = configured.split(":")[0]
        for name in installed:
            if name.split(":")[0] == base:
                return name
        return None

    def available(self) -> bool:
        if not self.settings.llm_enabled or httpx is None:
            self._reason = "LLM disabled" if httpx else "httpx missing"
            return False
        if self._available is not None:
            return self._available

        try:
            r = httpx.get(f"{self.settings.ollama_host}/api/tags", timeout=3.0)
            r.raise_for_status()
            self._tags = [m.get("name", "") for m in r.json().get("models", [])]
        except Exception:
            self._available = False
            self._reason = f"Ollama unreachable at {self.settings.ollama_host}"
            log.warning("%s — running regex-only", self._reason)
            return False

        if not self._tags:
            self._available = False
            self._reason = ("no models installed — run "
                            f"`ollama pull {self.settings.ollama_model}`")
            log.warning("Ollama has %s — running regex-only", self._reason)
            return False

        resolved = self._match_model(self.settings.ollama_model, self._tags)
        if resolved is None:
            # configured model isn't pulled; fall back to an installed one
            self._model = self._tags[0]
            self._reason = (f"configured model '{self.settings.ollama_model}' not "
                            f"installed; using '{self._model}'. "
                            f"`ollama pull {self.settings.ollama_model}` to use it")
            log.warning(self._reason)
        else:
            self._model = resolved
            self._reason = f"using model '{self._model}'"
        self._available = True
        return True

    def status(self) -> dict:
        return self.model_status()

    def model_status(self) -> dict:
        """Report what the detector will actually do — surfaced via /health."""
        avail = self.available()
        return {
            "available": avail,
            "enabled": self.settings.llm_enabled,
            "host": self.settings.ollama_host,
            "configured_model": self.settings.ollama_model,
            "effective_model": self._model,
            "installed_models": self._tags,
            "detail": self._reason,
        }

    def _chunks(self, text: str):
        size = self.settings.llm_chunk_size
        overlap = self.settings.llm_chunk_overlap
        if len(text) <= size:
            yield text
            return
        start = 0
        while start < len(text):
            yield text[start:start + size]
            start += size - overlap

    def _query(self, chunk: str) -> list[tuple[str, str]]:
        payload = {
            "model": self._model or self.settings.ollama_model,
            "system": _SYSTEM,
            "prompt": chunk,
            "stream": False,
            "format": "json",
            "options": {"temperature": 0.0},
        }
        try:
            r = httpx.post(
                f"{self.settings.ollama_host}/api/generate",
                json=payload, timeout=self.settings.ollama_timeout,
            )
            r.raise_for_status()
            raw = r.json().get("response", "")
        except Exception as e:
            log.debug("Ollama query failed: %s", e)
            return []
        return _parse(raw)

    def detect(self, text: str) -> list["Match"]:
        from . import Match
        if not self.available():
            return []
        found: dict[str, str] = {}
        for chunk in self._chunks(text):
            for value, etype in self._query(chunk):
                # verification: must literally appear in the source text
                if value and value in text:
                    found.setdefault(value, etype.upper())
        return [Match(text=v, entity_type=t, source="llm", confidence=0.8)
                for v, t in found.items()]


def _parse(raw: str) -> list[tuple[str, str]]:
    """Defensively pull (text, type) pairs out of whatever the model returned."""
    raw = raw.strip()
    # ollama with format=json may wrap the array in an object
    candidates = []
    try:
        obj = json.loads(raw)
        if isinstance(obj, list):
            candidates = obj
        elif isinstance(obj, dict):
            for v in obj.values():
                if isinstance(v, list):
                    candidates = v
                    break
            else:
                candidates = [obj]
    except json.JSONDecodeError:
        # last-ditch: grab the first [...] block
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        if m:
            try:
                candidates = json.loads(m.group(0))
            except json.JSONDecodeError:
                return []

    out: list[tuple[str, str]] = []
    for item in candidates:
        if isinstance(item, dict):
            t = item.get("text") or item.get("value")
            ty = item.get("type") or item.get("entity_type") or "OTHER"
            if isinstance(t, str) and t.strip():
                out.append((t.strip(), str(ty)))
    return out
