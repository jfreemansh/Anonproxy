"""
OpenAI Privacy Filter backend — open-weight (Apache-2.0) PII token classifier.

~96–97% F1 on PII-Masking-300k, runs locally. A strong alternative to GLiNER2 /
Piiranha. Optional dependency:

    pip install "anonproxy[openai-pii]"

SECURITY: the official model org is *exactly* ``openai``. Typosquats have hit HF
trending (e.g. ``Open-OSS/privacy-filter``); only load from ``openai/privacy-filter``.
"""
from __future__ import annotations

import logging

log = logging.getLogger("anonproxy.detectors.openai_pii")

# best-effort mapping of common Privacy Filter labels -> our entity types
_LABEL_MAP = {
    "NAME": "PERSON", "PERSON": "PERSON", "GIVENNAME": "PERSON", "SURNAME": "PERSON",
    "EMAIL": "EMAIL_ADDRESS", "EMAIL_ADDRESS": "EMAIL_ADDRESS",
    "USERNAME": "USERNAME",
    "PASSWORD": "CREDENTIAL", "SECRET": "CREDENTIAL",
    "IP": "IP_ADDRESS", "IP_ADDRESS": "IP_ADDRESS", "IPV4": "IP_ADDRESS",
    "PHONE": "IDENTIFIER", "PHONE_NUMBER": "IDENTIFIER",
    "SSN": "IDENTIFIER", "CREDIT_CARD": "IDENTIFIER", "ACCOUNT": "IDENTIFIER",
    "ADDRESS": "OTHER", "LOCATION": "OTHER", "ORG": "ORGANIZATION",
    "ORGANIZATION": "ORGANIZATION", "URL": "URL",
}


class OpenAIPrivacyFilterDetector:
    name = "openai-privacy-filter"

    def __init__(self, settings):
        self.settings = settings
        self._pipe = None
        self._reason = ""

    def _load(self):
        if self._pipe is None:
            model = self.settings.openai_pii_model
            if not model.startswith("openai/"):
                log.warning("openai_pii_model %r is not in the official 'openai/' "
                            "namespace — verify it is not a typosquat", model)
            from transformers import pipeline  # lazy: optional dependency
            self._pipe = pipeline("token-classification", model=model,
                                  aggregation_strategy="simple")

    def available(self) -> bool:
        try:
            self._load()
            self._reason = f"using {self.settings.openai_pii_model}"
            return True
        except Exception as e:
            self._reason = f"unavailable ({e}); pip install 'anonproxy[openai-pii]'"
            return False

    def detect(self, text: str):
        from . import Match
        self._load()
        out = []
        for ent in self._pipe(text):
            group = str(ent.get("entity_group", "")).upper().replace("-", "_")
            etype = _LABEL_MAP.get(group, "OTHER")
            start, end = ent.get("start"), ent.get("end")
            value = text[start:end] if start is not None and end is not None \
                else ent.get("word", "")
            value = value.strip()
            if value:
                out.append(Match(text=value, entity_type=etype,
                                 source="openai-privacy-filter",
                                 confidence=float(ent.get("score", 0.5))))
        return out

    def status(self) -> dict:
        ok = self.available()
        return {"available": ok, "model": self.settings.openai_pii_model,
                "detail": self._reason}
