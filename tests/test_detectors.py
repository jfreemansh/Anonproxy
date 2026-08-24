"""Pluggable detector registry: graceful with unknown/uninstalled backends."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine
from anonproxy.config import Settings
from anonproxy.detectors import build_detectors


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


# --- IPv6 vs timestamps -----------------------------------------------------
# Regression: the loose hex-colon IPv6 rule flagged HH:MM:SS timestamps and
# uptimes as IP_ADDRESS, mangling them into TEST-NET addresses.

def test_timestamps_not_flagged_as_ipv6():
    dets = build_detectors(_settings(["regex"]))
    text = ("start 09:00:00 end 14:23:01 elapsed 05:23:01 "
            "uptime 3d 04:15:32 took 00:00:02.481")
    found = {m.text for m in dets[0].detect(text)}
    assert not found, f"false positives: {found}"


def test_real_ipv6_full_forms_still_detected():
    from anonproxy.detectors.regex_detector import detect
    # every form the loose rule matched before the plausibility filter must
    # still match (compressed :: forms were never matched — unchanged gap)
    for ip in ("2001:0db8:0000:0000:0000:0000:0000:0001",
               "2001:db8:0:0:0:0:2:1",
               "2001:db8:85a3:8a2e:370:7334"):
        assert ip in {m.text for m in detect(f"addr {ip} end")}, ip


# --- configurable noise floor for short contextual tokens -------------------

class _StubContextual:
    name = "stub"
    floor = False

    def __init__(self, matches):
        self._matches = matches

    def available(self):
        return True

    def detect(self, text):
        from anonproxy.detectors import Match
        return [Match(text=t, entity_type=ty, source="stub")
                for t, ty in self._matches if t in text]

    def status(self):
        return {"available": True}


def _engine_with_stub(matches, **overrides):
    s = _settings(["regex"])
    for k, v in overrides.items():
        setattr(s, k, v)
    eng = Engine(settings=s)
    eng.detectors = [build_detectors(s)[0], _StubContextual(matches)]
    return eng


def test_short_contextual_token_dropped_by_default():
    import re
    eng = _engine_with_stub([("db", "HOSTNAME"), ("webserver01", "HOSTNAME")])
    out = eng.anonymize("connect to db via webserver01")
    assert "connect to db via" in out           # 2-char token survives
    assert re.search(r"\bwebserver01\b", out) is None


def test_contextual_min_len_override_keeps_short_tokens():
    import re
    eng = _engine_with_stub([("db", "HOSTNAME")], contextual_min_len=2)
    out = eng.anonymize("connect to db now")
    assert re.search(r"\bdb\b", out) is None    # now treated as a hostname
    assert eng.deanonymize(out) == "connect to db now"
