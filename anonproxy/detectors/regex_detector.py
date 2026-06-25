"""
Deterministic regex detection — the precise, zero-LLM floor.

Ordering matters: more specific / longer patterns (JWTs, AWS keys, FQDNs) are
declared before more general ones (bare IPs, hostnames) so that when the engine
sorts matches longest-first the structured tokens win their span.

This layer intentionally errs toward precision.  Recall for context-dependent
entities (bare hostnames, org names, person names) is the LLM layer's job, and
the engine's consistency rescan re-catches anything either layer found *once*.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from . import Match


# Each rule: (entity_type, compiled_pattern, capture_group)
_RULES: list[tuple[str, re.Pattern, int]] = []


def _rule(entity_type: str, pattern: str, group: int = 0, flags: int = 0) -> None:
    _RULES.append((entity_type, re.compile(pattern, flags), group))


# --- High-specificity secrets / tokens (declare first) ---------------------
_rule("TOKEN", r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b")  # JWT
_rule("TOKEN", r"\b(?:AKIA|ASIA|AIDA|AROA|AGPA|AIPA|ANPA|ANVA|APKA)[A-Z0-9]{16}\b")    # AWS key id
_rule("TOKEN", r"\bAIza[0-9A-Za-z_-]{35}\b")                                            # Google API
_rule("TOKEN", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b")                          # GitHub PAT
_rule("TOKEN", r"\bglpat-[A-Za-z0-9_-]{20}\b")                                          # GitLab PAT
_rule("TOKEN", r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b")                       # Stripe
_rule("TOKEN", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")                                      # Slack
_rule("TOKEN", r"-----BEGIN [A-Z ]*PRIVATE KEY-----")                                    # PEM marker

# --- Web app: auth headers & session cookies (high specificity, declare early) ---
_rule("TOKEN", r"(?i)\bauthorization\s*:\s*bearer\s+([A-Za-z0-9._~+/=-]{8,})", group=1)
_rule("CREDENTIAL", r"(?i)\bauthorization\s*:\s*basic\s+([A-Za-z0-9+/=]{8,})", group=1)
# Common session / CSRF / OAuth cookie + param names -> capture the value
_rule("TOKEN",
      r"(?i)\b(?:PHPSESSID|JSESSIONID|ASP\.NET_SessionId|connect\.sid|laravel_session|"
      r"sessionid|session|sid|csrftoken|csrf_token|xsrf[-_]token|x-csrf-token|"
      r"access_token|refresh_token|id_token|auth_token|remember_token|bearer)"
      r"\s*[=:]\s*\"?([A-Za-z0-9._~+/=-]{6,})\"?", group=1)

# --- Hashes ----------------------------------------------------------------
# LM:NT combined (pwdump / secretsdump format) — match before bare md5/sha
_rule("HASH", r"\b[0-9a-fA-F]{32}:[0-9a-fA-F]{32}\b")
_rule("HASH", r"\b[0-9a-fA-F]{64}\b")   # sha256
_rule("HASH", r"\b[0-9a-fA-F]{40}\b")   # sha1
_rule("HASH", r"\$(?:1|2[aby]|5|6|y)\$[./A-Za-z0-9$]{8,}")  # crypt / bcrypt
_rule("HASH", r"\b[0-9a-fA-F]{32}\b")   # md5 / NTLM (declared after the longer ones)

# --- Personal identifiers (common in web app request/response bodies) -------
_rule("IDENTIFIER", r"\b\d{3}-\d{2}-\d{4}\b")   # US SSN

# --- Network ---------------------------------------------------------------
_rule("MAC_ADDRESS", r"\b(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}\b")
# CIDR before bare IP
_rule("CIDR", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)/\d{1,2}\b")
_rule("IP_ADDRESS", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
# IPv6 (loose but anchored on multiple hextet colons)
_rule("IP_ADDRESS", r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b")

# --- URLs / domains / email ------------------------------------------------
_rule("URL", r"\b[a-zA-Z][a-zA-Z0-9+.-]*://[^\s\"'`<>\]]+")
_rule("EMAIL_ADDRESS", r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# FQDN incl. internal TLDs (.local/.corp/.lan/.internal) and public TLDs
_rule(
    "DOMAIN",
    r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+"
    r"(?:"
    # internal / lab suffixes
    r"local|corp|lan|internal|intra|intranet|home|test|example|localdomain|"
    # generic TLDs common on engagements
    r"com|net|org|io|ai|app|dev|cloud|tech|xyz|online|site|store|info|biz|co|"
    r"gov|edu|mil|int|me|tv|gg|sh|so|to|cc|io|id|"
    # country TLDs
    r"uk|de|fr|es|pt|nl|br|eu|cz|sk|us|ca|au|nz|jp|cn|ru|in|ch|at|be|se|no|dk|"
    r"fi|it|pl|ie|gr|hu|ro|ua|kr|sg|hk|za|mx|ar|cl|tr|il|ae|sa"
    r")\b",
)

# --- Windows domain accounts ----------------------------------------------
_rule("USERNAME", r"\b[A-Za-z0-9.-]{2,30}\\[A-Za-z0-9._-]{2,30}\b")   # CORP\jsmith

# --- Credentials in labeled contexts (password=..., pass: ...) -------------
_rule("CREDENTIAL",
      r"(?i)(?:password|passwd|pwd|pass|secret|api[_-]?key|token)\s*[:=]\s*"
      r"(\S{6,})", group=1)

# Payment card numbers: 13–19 digits, optionally space/dash grouped. Validated
# with the Luhn checksum to keep false positives near zero (so we don't anonymize
# random long digit strings like order ids or timestamps).
_CC_CANDIDATE = re.compile(r"(?<![\d.])(?:\d[ -]?){13,19}(?<![ -])")


def _luhn_ok(digits: str) -> bool:
    if not 13 <= len(digits) <= 19:
        return False
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# Tool names / protocols / very common words never to flag as ORG/HOST etc.
SAFE_WORDS: frozenset[str] = frozenset(w.lower() for w in {
    "localhost", "example", "pentest", "8.8.8.8", "1.1.1.1", "127.0.0.1",
    "0.0.0.0", "255.255.255.255", "169.254.169.254", "::1",
    "github.com", "gitlab.com", "anthropic.com", "openai.com",
})


def detect(text: str) -> list["Match"]:
    from . import Match  # local import avoids circular import at module load

    spans: list[tuple[int, int, str, str]] = []  # (start, end, text, type)
    occupied: list[tuple[int, int]] = []

    def overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in occupied)

    # Payment cards first (Luhn-checked) so they claim their span before the
    # generic numeric rules.
    for m in _CC_CANDIDATE.finditer(text):
        value = m.group()
        digits = value.replace(" ", "").replace("-", "")
        if _luhn_ok(digits):
            occupied.append((m.start(), m.start() + len(value)))
            spans.append((m.start(), m.start() + len(value), value, "PAYMENT_CARD"))

    for entity_type, pattern, group in _RULES:
        for m in pattern.finditer(text):
            start, end = m.span(group)
            if start < 0:
                continue
            value = m.group(group)
            if not value or value.lower() in SAFE_WORDS:
                continue
            if overlaps(start, end):
                continue
            occupied.append((start, end))
            spans.append((start, end, value, entity_type))

    # de-dup identical strings, keep first type seen
    seen: dict[str, str] = {}
    for _, _, value, etype in spans:
        seen.setdefault(value, etype)
    return [Match(text=v, entity_type=t, source="regex") for v, t in seen.items()]
