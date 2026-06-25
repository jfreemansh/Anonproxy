"""
Pluggable detection layers.

Each detector returns a list of ``Match`` and reports its own availability, so
the engine can run a configurable *chain* of them. The default chain is
``regex,ollama`` (no extra dependencies). Heavier, more accurate backends
(``gliner``, ``piiranha``, ``anonymizer-slm``) are optional and lazily imported,
so naming one you don't have installed degrades gracefully to a warning rather
than an import error.

Add a backend by writing a class with ``name``, ``available()``, ``detect()``
and ``status()`` and registering it in ``_REGISTRY``.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("anonproxy.detectors")


@dataclass(frozen=True)
class Match:
    text: str          # the exact substring detected
    entity_type: str   # IP_ADDRESS, HASH, HOSTNAME, ...
    source: str        # "regex" | "ollama" | "gliner" | "piiranha" | "anonymizer-slm"
    confidence: float = 1.0


from .regex_detector import detect as regex_detect  # noqa: E402
from .llm_detector import LLMDetector  # noqa: E402


class RegexDetector:
    """The deterministic floor — always available, no dependencies."""
    name = "regex"
    floor = True

    def __init__(self, settings):
        self.settings = settings

    def available(self) -> bool:
        return True

    def detect(self, text: str) -> list[Match]:
        return regex_detect(text)

    def status(self) -> dict:
        return {"available": True, "detail": "deterministic regex floor"}


# name -> factory(settings) -> detector instance. Factories may import optional
# dependencies lazily; if those are missing the detector's available() returns
# False with a reason, and the engine skips it.
def _make_gliner(settings):
    from .gliner_detector import GlinerDetector
    return GlinerDetector(settings)


def _make_gliner2(settings):
    from .gliner2_detector import Gliner2Detector
    return Gliner2Detector(settings)


def _make_piiranha(settings):
    from .piiranha_detector import PiiranhaDetector
    return PiiranhaDetector(settings)


def _make_openai_pii(settings):
    from .openai_pii_detector import OpenAIPrivacyFilterDetector
    return OpenAIPrivacyFilterDetector(settings)


def _make_anonymizer_slm(settings):
    from .anonymizer_slm import AnonymizerSLMDetector
    return AnonymizerSLMDetector(settings)


def _make_scope(settings):
    from .scope_detector import ScopeDetector
    return ScopeDetector(settings)


_REGISTRY: dict[str, Callable] = {
    "regex": lambda s: RegexDetector(s),
    "scope": _make_scope,
    "ollama": lambda s: LLMDetector(s),
    "llm": lambda s: LLMDetector(s),              # alias
    "gliner": _make_gliner,                        # original urchade GLiNER
    "gliner2": _make_gliner2,                      # SOTA Fastino GLiNER2-PII
    "piiranha": _make_piiranha,
    "openai-privacy-filter": _make_openai_pii,
    "openai-pii": _make_openai_pii,                # alias
    "anonymizer-slm": _make_anonymizer_slm,
    "anonymizer_slm": _make_anonymizer_slm,        # alias
}


def build_detectors(settings) -> list:
    """Instantiate the configured detector chain, regex always first."""
    names = list(settings.detectors)
    # ensure the regex floor is present and leads the chain; auto-include the
    # scope seed (right after regex) whenever scope terms are configured.
    ordered = ["regex"] + [n for n in names if n not in ("regex",)]
    if (getattr(settings, "scope_terms", None) or getattr(settings, "scope_file", "")) \
            and "scope" not in ordered:
        ordered.insert(1, "scope")
    detectors = []
    seen = set()
    for name in ordered:
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        factory = _REGISTRY.get(key)
        if factory is None:
            log.warning("unknown detector %r — skipping (known: %s)",
                        name, ", ".join(sorted(_REGISTRY)))
            continue
        try:
            detectors.append(factory(settings))
        except Exception as e:   # missing optional dependency, etc.
            log.warning("detector %r unavailable: %s", name, e)
    return detectors


__all__ = ["Match", "regex_detect", "LLMDetector", "RegexDetector",
           "build_detectors"]
