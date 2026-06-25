"""
GLiNER backend — lightweight zero-shot NER (no Ollama, runs on CPU).

Strength: you define the labels at inference time, so we ask it for exactly the
contextual entities our regex floor can't see (person, org, username, hostname…)
without training. Optional dependency:

    pip install "anonproxy[gliner]"
"""
from __future__ import annotations

import logging

log = logging.getLogger("anonproxy.detectors.gliner")

# GLiNER label -> our entity type
_LABEL_MAP = {
    "person": "PERSON",
    "organization": "ORGANIZATION",
    "company": "ORGANIZATION",
    "email": "EMAIL_ADDRESS",
    "email address": "EMAIL_ADDRESS",
    "username": "USERNAME",
    "hostname": "HOSTNAME",
    "ip address": "IP_ADDRESS",
    "credential": "CREDENTIAL",
    "password": "CREDENTIAL",
    "phone number": "IDENTIFIER",
}
_DEFAULT_LABELS = list(_LABEL_MAP.keys())


class GlinerDetector:
    name = "gliner"

    def __init__(self, settings):
        self.settings = settings
        self._model = None
        self._reason = ""
        self.labels = _DEFAULT_LABELS

    def _load(self):
        if self._model is None:
            from gliner import GLiNER  # lazy: optional dependency
            self._model = GLiNER.from_pretrained(self.settings.gliner_model)

    def available(self) -> bool:
        try:
            self._load()
            self._reason = f"using {self.settings.gliner_model}"
            return True
        except Exception as e:
            self._reason = f"unavailable ({e}); pip install 'anonproxy[gliner]'"
            return False

    def detect(self, text: str):
        from . import Match
        self._load()
        ents = self._model.predict_entities(
            text, self.labels, threshold=self.settings.gliner_threshold)
        out = []
        for e in ents:
            etype = _LABEL_MAP.get(str(e.get("label", "")).lower(), "OTHER")
            value = e.get("text", "")
            if value:
                out.append(Match(text=value, entity_type=etype,
                                 source="gliner", confidence=float(e.get("score", 0.5))))
        return out

    def status(self) -> dict:
        ok = self.available()
        return {"available": ok, "model": self.settings.gliner_model,
                "detail": self._reason}
