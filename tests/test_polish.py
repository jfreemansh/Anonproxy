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


def test_stream_actually_grows_to_4096():
    # _stream previously returned after ONE extension round regardless of
    # length (a misplaced `return`), silently capping every keystream at 64
    # bytes — invisible until something needed more than that.
    s = surrogates._stream("engagement", "TOKEN", "x")
    assert len(s) == 4096


def test_credential_value_stops_at_ampersand():
    # form-urlencoded body, no whitespace anywhere — an unbounded \S{6,}
    # capture used to swallow every remaining field as one "credential",
    # which then collided with an unrelated match and got dropped entirely.
    eng = _engine(["regex"])
    body = "log=nickkilla&pwd=Jackhoffmaster1%21&wp-submit=Log+In&wfls-token=477728"
    anon = eng.anonymize(body)
    assert "Jackhoffmaster1%21" not in anon
    assert "477728" not in anon
    assert "wp-submit=Log+In" in anon  # untouched field must survive intact
    assert eng.deanonymize(anon) == body


def test_hash_after_percent_encoded_delimiter_is_caught():
    # a real WordPress auth-cookie shape: user|expiry|token|hash, %7C-joined.
    # The hex hash sits directly after "%7C" — "C" is a word char, so no \b
    # boundary exists there and \b[0-9a-fA-F]{64}\b silently never matched.
    eng = _engine(["regex"])
    sha = "df18448f2c8a23e1c221b72d1a7be60acca55cfac8c1f7cb2ecb59537ca52c6c"
    text = f"Set-Cookie: wordpress_sec_x=nickkilla%7C1783150759%7Ctoken123abc%7C{sha}; path=/"
    anon = eng.anonymize(text)
    assert sha not in anon
    assert eng.deanonymize(anon) == text


def test_cookie_value_redacted_regardless_of_cookie_name():
    # every app invents its own session-cookie name; a fixed name list will
    # always miss the next one. Redact by structure (Set-Cookie: / Cookie:
    # header shape) instead of matching known names.
    eng = _engine(["regex"])
    resp = "Set-Cookie: myapp_totally_custom_session_name=abc123opaqueSecretValue; path=/; secure"
    anon = eng.anonymize(resp)
    assert "abc123opaqueSecretValue" not in anon
    assert "myapp_totally_custom_session_name=" in anon  # cookie NAME stays, only value swaps
    assert eng.deanonymize(anon) == resp

    req = "Cookie: theme=dark; myapp_totally_custom_session_name=abc123opaqueSecretValue"
    anon2 = eng.anonymize(req)
    assert "abc123opaqueSecretValue" not in anon2
    assert eng.deanonymize(anon2) == req


def test_genuine_wrappers_survive_roundtrip():
    # a real failure from a captured wp-admin page: plugin UI text contained
    # literal asterisks around an example domain (*badsite.example.com*). The
    # restorer used to swallow hugging * ` ~ as if they were model-added
    # markdown, corrupting the round trip. Genuine wrappers must survive.
    eng = _engine(["regex"])
    for text in (
        'placeholder "e.g., *badsite.example.com*" here',
        'value is `10.20.0.10` inline',
        'wrapped ~10.20.0.10~ tilde',
    ):
        anon = eng.anonymize(text)
        assert eng.deanonymize(anon) == text, f"round trip corrupted: {text!r}"


def test_chunk_text_covers_full_input_with_overlap():
    from anonproxy.detectors import chunk_text
    assert chunk_text("short") == ["short"]
    big = "".join(chr(97 + (i % 26)) for i in range(10000))
    chunks = chunk_text(big, size=1500, overlap=200)
    assert len(chunks) > 1
    assert all(len(c) <= 1500 for c in chunks)
    # every original character is covered by at least one chunk
    assert "".join(chunks).find(big[:1500]) == 0
    # reassembling by stepping (size-overlap) reproduces the whole input
    step = 1500 - 200
    rebuilt = "".join(c[:step] for c in chunks[:-1]) + chunks[-1]
    assert rebuilt == big


def test_long_token_surrogate_does_not_crash():
    # a real captured WordPress session-cookie value (44 chars) — this
    # crashed with IndexError before the _stream fix, because _pick indexed
    # past the 64-byte-capped stream for anything longer than that.
    long_value = "zXE0oi5BfKMLCqElPvLFwbz3Xv6pdikk2v3hLXjKXtQ"
    assert len(long_value) > 64 // 2  # sanity: this is the class of input that broke
    surrogate = surrogates.generate("TOKEN", long_value, engagement="t")
    assert surrogate and surrogate != long_value

    # and well past the full 64-byte cap, to be sure
    very_long = "y" * 500
    surrogate2 = surrogates.generate("TOKEN", very_long, engagement="t")
    assert surrogate2 and surrogate2 != very_long
