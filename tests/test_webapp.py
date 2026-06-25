"""Web-app-specific regex coverage: cards, session cookies, auth headers, SSN."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy import Engine
from anonproxy.detectors.regex_detector import _luhn_ok, detect


def _engine():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    return Engine(settings=s)


def _types(text):
    return {(m.text, m.entity_type) for m in detect(text)}


def test_payment_card_luhn():
    assert _luhn_ok("4111111111111111")          # valid Visa test number
    assert not _luhn_ok("1234567890123456")       # fails checksum
    found = {t for t, _ in _types("card 4111 1111 1111 1111 on file")}
    assert "4111 1111 1111 1111" in found


def test_random_long_number_not_flagged_as_card():
    # an order id / timestamp that fails Luhn must NOT be anonymized
    assert _types("order 1234567890123456 placed") == set()


def test_session_cookies_and_auth_headers():
    assert ("9f8a7b6c5d4e3f2a1b0c", "TOKEN") in _types("Cookie: PHPSESSID=9f8a7b6c5d4e3f2a1b0c;")
    assert ("YWxhZGRpbjpvcGVu", "CREDENTIAL") in _types("Authorization: Basic YWxhZGRpbjpvcGVu")
    bearer = _types("Authorization: Bearer abc123DEF456ghi789")
    assert ("abc123DEF456ghi789", "TOKEN") in bearer


def test_ssn():
    assert ("123-45-6789", "IDENTIFIER") in _types("SSN 123-45-6789 verified")


def test_webapp_roundtrip():
    eng = _engine()
    req = ('POST /pay HTTP/1.1\nHost: shop.acme.com\n'
           'Cookie: PHPSESSID=9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c\n'
           '{"card":"4111 1111 1111 1111","email":"a@acme.com"}')
    anon = eng.anonymize(req)
    for secret in ("9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c", "4111 1111 1111 1111",
                   "a@acme.com", "shop.acme.com"):
        assert secret not in anon, f"leaked {secret}"
    assert eng.deanonymize(anon) == req
