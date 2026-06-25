"""Streaming restoration: surrogates split across chunk boundaries are still
restored whole."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine, Settings


def fresh_engine():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    return Engine(engagement="stream", settings=s)


def _chunk(text, size):
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_stream_split_surrogate_is_restored():
    engine = fresh_engine()
    text = "Server dc01.acmecorp.local at 10.20.0.10 responded"
    anon = engine.anonymize(text)
    originals = [r["original"] for r in engine.export()]

    # stream the (bolded) model reply 3 characters at a time — worst case splits
    reply = f"The host **{anon}** is reachable."
    sr = engine.stream_restorer()
    out = []
    for ch in _chunk(reply, 3):
        out.append(sr.push(ch))
    out.append(sr.flush())
    restored = "".join(out)

    for original in originals:
        assert original in restored, f"lost {original!r} in stream\n{restored!r}"


def test_stream_matches_batch():
    engine = fresh_engine()
    text = "user CORP\\jsmith hash 8846f7eaee8fb117ad06bdd830b7586c host FILESERVER"
    anon = engine.anonymize(text)
    reply = f"Analysis: `{anon}` looks crackable."

    batch = engine.deanonymize(reply)
    sr = engine.stream_restorer()
    streamed = "".join(sr.push(c) for c in reply) + sr.flush()
    assert streamed == batch
