"""
Anonymizer-SLM backend — Eternis's purpose-built Qwen3 fine-tune, served locally
via Ollama.

The model is trained to emit a ``replace_entities`` tool call identifying the
spans that need replacing. We use only its *detections* (the ``original`` spans)
and feed them through Anonproxy's own deterministic, format-preserving surrogate
engine, so consistency and reversibility stay under our control.

Setup (one time):
    # download a GGUF from the eternisai/anonymizer-model-series collection, then
    ollama create anonymizer-slm -f models/anonymizer-slm.Modelfile

Then run with:  ANONPROXY_DETECTORS=regex,anonymizer-slm
"""
from __future__ import annotations

import json
import logging
import re

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

log = logging.getLogger("anonproxy.detectors.anonymizer_slm")

_TOOLCALL_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


def _infer_type(value: str) -> str:
    v = value.strip()
    if "@" in v and "." in v:
        return "EMAIL_ADDRESS"
    if re.fullmatch(r"(?:\d{1,3}\.){3}\d{1,3}", v):
        return "IP_ADDRESS"
    if "\\" in v or re.fullmatch(r"[a-z]+\.[a-z]+", v.lower()):
        return "USERNAME"
    if re.search(r"[.](?:local|corp|com|net|org|lan)$", v.lower()):
        return "DOMAIN"
    if " " in v:
        return "PERSON"            # multi-word -> most likely a name
    return "OTHER"


class AnonymizerSLMDetector:
    name = "anonymizer-slm"

    def __init__(self, settings):
        self.settings = settings
        self._reason = ""
        self._ok = None

    def available(self) -> bool:
        if httpx is None:
            self._reason = "httpx missing"
            return False
        if self._ok is not None:
            return self._ok
        try:
            r = httpx.get(f"{self.settings.ollama_host}/api/tags", timeout=3.0)
            r.raise_for_status()
            names = [m.get("name", "") for m in r.json().get("models", [])]
            base = self.settings.anonymizer_slm_model.split(":")[0]
            self._ok = any(n == self.settings.anonymizer_slm_model
                           or n.split(":")[0] == base for n in names)
            self._reason = (f"using {self.settings.anonymizer_slm_model}" if self._ok
                            else f"model '{self.settings.anonymizer_slm_model}' not in "
                                 f"Ollama; see models/anonymizer-slm.Modelfile")
        except Exception as e:
            self._ok = False
            self._reason = f"Ollama unreachable: {e}"
        return self._ok

    def detect(self, text: str):
        from . import Match
        if not self.available():
            return []
        try:
            r = httpx.post(
                f"{self.settings.ollama_host}/api/generate",
                json={"model": self.settings.anonymizer_slm_model,
                      "prompt": text, "stream": False,
                      "options": {"temperature": 0.0}},
                timeout=self.settings.ollama_timeout)
            r.raise_for_status()
            raw = r.json().get("response", "")
        except Exception as e:
            log.debug("anonymizer-slm query failed: %s", e)
            return []

        out = []
        for original in _parse_replacements(raw):
            if original and original in text:
                out.append(Match(text=original, entity_type=_infer_type(original),
                                 source="anonymizer-slm", confidence=0.85))
        return out

    def status(self) -> dict:
        ok = self.available()
        return {"available": ok, "model": self.settings.anonymizer_slm_model,
                "detail": self._reason}


def _parse_replacements(raw: str) -> list[str]:
    """Extract the ``original`` spans from the model's replace_entities tool call."""
    blocks = _TOOLCALL_RE.findall(raw) or [raw]
    originals: list[str] = []
    for block in blocks:
        try:
            obj = json.loads(block)
        except json.JSONDecodeError:
            continue
        args = obj.get("arguments", obj)
        for rep in args.get("replacements", []) if isinstance(args, dict) else []:
            if isinstance(rep, dict) and isinstance(rep.get("original"), str):
                originals.append(rep["original"])
    return originals
