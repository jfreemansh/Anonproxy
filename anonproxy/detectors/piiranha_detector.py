"""
Piiranha backend — mDeBERTa token-classifier specialised for PII (multilingual).

Strength: very high accuracy on passwords, emails, usernames and phone numbers
across six languages — a strong complement to the regex floor for credential and
identity PII. Optional dependency:

    pip install "anonproxy[piiranha]"
"""
from __future__ import annotations

import logging

log = logging.getLogger("anonproxy.detectors.piiranha")

# Piiranha entity_group -> our entity type
_LABEL_MAP = {
    "GIVENNAME": "PERSON",
    "SURNAME": "PERSON",
    "EMAIL": "EMAIL_ADDRESS",
    "USERNAME": "USERNAME",
    "PASSWORD": "CREDENTIAL",
    "TELEPHONENUM": "IDENTIFIER",
    "IDCARDNUM": "IDENTIFIER",
    "ACCOUNTNUM": "IDENTIFIER",
    "SOCIALNUM": "IDENTIFIER",
    "CREDITCARDNUMBER": "IDENTIFIER",
    "BUILDINGNUM": "OTHER",
    "STREET": "OTHER",
    "CITY": "OTHER",
    "ZIPCODE": "OTHER",
    "TAXNUM": "IDENTIFIER",
    "DRIVERLICENSENUM": "IDENTIFIER",
}


class PiiranhaDetector:
    name = "piiranha"

    def __init__(self, settings):
        self.settings = settings
        self._pipe = None
        self._reason = ""

    def _load(self):
        if self._pipe is None:
            from transformers import pipeline  # lazy: optional dependency
            self._pipe = pipeline(
                "token-classification", model=self.settings.piiranha_model,
                aggregation_strategy="simple")

    def available(self) -> bool:
        try:
            self._load()
            self._reason = f"using {self.settings.piiranha_model}"
            return True
        except Exception as e:
            self._reason = f"unavailable ({e}); pip install 'anonproxy[piiranha]'"
            return False

    def detect(self, text: str):
        from . import Match
        self._load()
        out = []
        for ent in self._pipe(text):
            group = str(ent.get("entity_group", "")).upper()
            etype = _LABEL_MAP.get(group, "OTHER")
            start, end = ent.get("start"), ent.get("end")
            value = text[start:end] if start is not None and end is not None \
                else ent.get("word", "")
            value = value.strip()
            if value:
                out.append(Match(text=value, entity_type=etype,
                                 source="piiranha", confidence=float(ent.get("score", 0.5))))
        return out

    def status(self) -> dict:
        ok = self.available()
        return {"available": ok, "model": self.settings.piiranha_model,
                "detail": self._reason}
