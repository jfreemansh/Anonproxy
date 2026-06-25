"""
Engagement scope seed — the most reliable way to cover *bare* hostnames.

Regex can't safely guess that ``DC01`` or ``WEB-PRD-03`` is a hostname (it would
flag random words). But on a real engagement you already *know* your scope. Drop
the client's domains, hostnames, org names and IP ranges into a scope list and
this detector anonymizes every occurrence, deterministically, as part of the
floor — no model required.

Sources (combined):
    ANONPROXY_SCOPE="acme.com,DC01,WEB-PRD-03,Acme Corp"
    ANONPROXY_SCOPE_FILE=engagement-scope.txt   # one term per line; optional value=TYPE
        # comments and blank lines ignored
        dc01=HOSTNAME
        acmecorp.local
        10.20.0.0/16=CIDR
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("anonproxy.detectors.scope")

_IP_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_CIDR_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}/\d{1,2}$")


def _infer_type(value: str) -> str:
    if _CIDR_RE.match(value):
        return "CIDR"
    if _IP_RE.match(value):
        return "IP_ADDRESS"
    if "@" in value:
        return "EMAIL_ADDRESS"
    if re.search(r"\.[a-zA-Z]{2,}$", value):
        return "DOMAIN"
    if " " in value:
        return "ORGANIZATION"
    return "HOSTNAME"


class ScopeDetector:
    name = "scope"
    floor = True   # deterministic — always runs, even in regex-only mode

    def __init__(self, settings):
        self.settings = settings
        self._terms = self._load()
        # match each term as a whole token (not inside a larger word/host)
        # token boundaries: don't match inside a larger word/identifier, and don't
        # match a bare label inside a domain ("acme" in "acme.com"), but DO allow a
        # trailing sentence period ("Acme Corp.").
        self._compiled = [
            (re.compile(r"(?<![\w@-])" + re.escape(term) + r"(?![\w-])(?!\.[\w-])",
                        re.IGNORECASE), etype)
            for term, etype in self._terms
        ]

    def _load(self) -> list[tuple[str, str]]:
        raw: list[str] = list(self.settings.scope_terms)
        if self.settings.scope_file:
            try:
                for line in Path(self.settings.scope_file).read_text().splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        raw.append(line)
            except Exception as e:
                log.warning("could not read scope file %s: %s",
                            self.settings.scope_file, e)
        terms: list[tuple[str, str]] = []
        for entry in raw:
            if "=" in entry:
                value, _, etype = entry.partition("=")
                value, etype = value.strip(), etype.strip().upper()
            else:
                value, etype = entry.strip(), ""
            if value:
                terms.append((value, etype or _infer_type(value)))
        # longest first so a domain claims its span before a bare label inside it
        terms.sort(key=lambda t: len(t[0]), reverse=True)
        return terms

    def available(self) -> bool:
        return bool(self._terms)

    def detect(self, text: str):
        from . import Match
        out = []
        for pattern, etype in self._compiled:
            for m in pattern.finditer(text):
                out.append(Match(text=m.group(), entity_type=etype, source="scope"))
        return out

    def status(self) -> dict:
        return {"available": bool(self._terms), "terms": len(self._terms),
                "detail": f"{len(self._terms)} scope term(s) loaded"}
