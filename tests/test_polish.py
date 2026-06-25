"""Polish: Luhn-valid card surrogates + precise-span-wins de-wrapping."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine, surrogates
from anonproxy.config import Settings
from anonproxy.detectors import Match
from anonproxy.detectors.regex_detector import _luhn_ok


def _engine(detectors=("regex",)):
    s = Settings()
    s.ephemeral = True
    s.detectors = list(detectors)
    return Engine(settings=s)


def test_card_surrogate_is_luhn_valid_and_formatted():
    s = surrogates.generate("PAYMENT_CARD", "4111 1111 1111 1111", engagement="t")
    assert s.count(" ") == 3                       # spacing preserved
    digits = s.replace(" ", "")
    assert len(digits) == 16 and digits.isdigit()
    assert _luhn_ok(digits)                        # valid card number
    assert s != "4111 1111 1111 1111"


def test_card_surrogate_dashes_layout():
    s = surrogates.generate("PAYMENT_CARD", "5500-0000-0000-0004", engagement="t")
    assert s.count("-") == 3 and _luhn_ok(s.replace("-", ""))


class _WrapDetector:
    """Fake contextual backend that over-captures 'PHPSESSID=<value>'."""
    name = "wrap"
    floor = False

    def available(self):
        return True

    def detect(self, text):
        return [Match("PHPSESSID=9f8a7b6c5d4e3f2a1b0c", "CREDENTIAL", "wrap")]

    def status(self):
        return {"available": True, "detail": "test"}


def test_precise_span_wins_preserves_cookie_name():
    eng = _engine(["regex"])
    eng.detectors.append(_WrapDetector())           # simulate the LLM over-capture
    text = "Cookie: PHPSESSID=9f8a7b6c5d4e3f2a1b0c; path=/"
    anon = eng.anonymize(text, use_llm=True)
    assert "PHPSESSID=" in anon                       # generic name kept
    assert "9f8a7b6c5d4e3f2a1b0c" not in anon         # value anonymized
    assert eng.deanonymize(anon) == text


def test_card_roundtrip_through_engine():
    eng = _engine(["regex"])
    text = 'pay {"card":"4111 1111 1111 1111"}'
    anon = eng.anonymize(text)
    assert "4111 1111 1111 1111" not in anon
    assert eng.deanonymize(anon) == text
