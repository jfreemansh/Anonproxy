"""Pluggable detector registry: graceful with unknown/uninstalled backends."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.detectors import build_detectors
from anonproxy import Engine


def _settings(detectors):
    s = Settings()
    s.ephemeral = True
    s.detectors = detectors
    return s


def test_regex_always_present_and_first():
    dets = build_detectors(_settings(["ollama"]))
    assert dets[0].name == "regex"


def test_unknown_backend_skipped():
    dets = build_detectors(_settings(["regex", "does-not-exist"]))
    names = [d.name for d in dets]
    assert "regex" in names
    assert "does-not-exist" not in names


def test_optional_backend_unavailable_is_graceful():
    # gliner/piiranha deps aren't installed in CI — they must report unavailable,
    # never raise, and never block the regex floor.
    eng = Engine(settings=_settings(
        ["regex", "gliner", "gliner2", "piiranha", "openai-privacy-filter"]))
    status = {d["name"]: d for d in eng.detector_status()}
    assert status["regex"]["available"] is True
    for opt in ("gliner", "gliner2", "piiranha", "openai-privacy-filter"):
        if opt in status:
            assert status[opt]["available"] in (True, False)
    # anonymization still works via the regex floor
    out = eng.anonymize("host 10.20.0.10")
    assert "10.20.0.10" not in out


def test_regex_only_chain_detects_structured():
    eng = Engine(settings=_settings(["regex"]))
    out = eng.anonymize("ip 192.168.1.5 hash 8846f7eaee8fb117ad06bdd830b7586c")
    assert "192.168.1.5" not in out
    assert "8846f7eaee8fb117ad06bdd830b7586c" not in out
    assert eng.deanonymize(out) == "ip 192.168.1.5 hash 8846f7eaee8fb117ad06bdd830b7586c"
