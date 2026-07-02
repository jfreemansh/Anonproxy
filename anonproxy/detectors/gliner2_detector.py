"""
GLiNER2-PII backend (Fastino) — current SOTA for PII span extraction.

205M params, 42 PII entity types, 7 languages, highest span-level F1 on the SPY
benchmark (beats the older urchade GLiNER, NVIDIA GLiNER-PII and OpenAI Privacy
Filter), <100ms inference. Best *recall*, which is what you want for redaction.

Uses the separate ``gliner2`` library (different API from the original GLiNER):

    pip install "anonproxy[gliner2]"
"""
from __future__ import annotations

import logging

log = logging.getLogger("anonproxy.detectors.gliner2")

# subset of GLiNER2-PII's 42 labels we ask for, mapped to our entity types.
_LABEL_MAP = {
    "person": "PERSON", "full_name": "PERSON", "first_name": "PERSON",
    "middle_name": "PERSON", "last_name": "PERSON",
    "email": "EMAIL_ADDRESS",
    "username": "USERNAME",
    "ip_address": "IP_ADDRESS",
    "password": "CREDENTIAL", "secret": "CREDENTIAL",
    "api_key": "TOKEN", "access_token": "TOKEN", "recovery_code": "CREDENTIAL",
    "phone_number": "IDENTIFIER", "government_id": "IDENTIFIER",
    "national_id_number": "IDENTIFIER", "passport_number": "IDENTIFIER",
    "drivers_license_number": "IDENTIFIER", "tax_id": "IDENTIFIER",
    "bank_account": "IDENTIFIER", "account_number": "IDENTIFIER",
    "iban": "IDENTIFIER", "payment_card": "IDENTIFIER", "card_number": "IDENTIFIER",
    "account_id": "IDENTIFIER", "sensitive_account_id": "IDENTIFIER",
    "address": "OTHER", "street_address": "OTHER", "city": "OTHER",
}
# Labels relevant to pentest/PII work that we request at inference time.
_REQUEST_LABELS = ["person", "email", "username", "ip_address", "password",
                   "secret", "api_key", "access_token", "phone_number",
                   "government_id", "passport_number", "bank_account",
                   "account_number", "address"]


class Gliner2Detector:
    name = "gliner2"

    def __init__(self, settings):
        self.settings = settings
        self._model = None
        self._reason = ""

    def _load(self):
        if self._model is None:
            from gliner2 import GLiNER2  # lazy: optional dependency
            self._model = GLiNER2.from_pretrained(self.settings.gliner2_model)

    def available(self) -> bool:
        try:
            self._load()
            self._reason = f"using {self.settings.gliner2_model}"
            return True
        except Exception as e:
            self._reason = f"unavailable ({e}); pip install 'anonproxy[gliner2]'"
            return False

    def detect(self, text: str):
        from . import Match, chunk_text
        self._load()
        out = []
        seen = set()
        for chunk in chunk_text(text):
            result = self._model.extract_entities(
                chunk, _REQUEST_LABELS,
                threshold=self.settings.gliner2_threshold,
                include_spans=True,
            )
            # GLiNER2 returns {"entities": {label: [value, ...]}}
            entities = result.get("entities", {}) if isinstance(result, dict) else {}
            for label, values in entities.items():
                etype = _LABEL_MAP.get(str(label).lower(), "OTHER")
                for value in values:
                    v = value.get("text", "") if isinstance(value, dict) else str(value)
                    if v and v not in seen:
                        seen.add(v)
                        out.append(Match(text=v, entity_type=etype, source="gliner2"))
        return out

    def status(self) -> dict:
        ok = self.available()
        return {"available": ok, "model": self.settings.gliner2_model,
                "detail": self._reason}
