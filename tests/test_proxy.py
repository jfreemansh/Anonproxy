"""End-to-end proxy test against a mock upstream.

Verifies: (1) the request reaching "Anthropic" contains only surrogates, and
(2) the response the client receives has real values restored — even when the
mock model bolds and uppercases the surrogate (the pattern that breaks naive
str.replace), including over a fine-grained SSE stream.
"""
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.proxy.app import create_app, _strip_v1

CAPTURED = {}


def test_strip_v1_handles_both_forms():
    # OpenAI-style default: bare host, nothing to strip
    assert _strip_v1("https://api.openai.com") == "https://api.openai.com"
    # every OpenAI-compatible provider's docs give a base_url ending in /v1 —
    # that must not double up with our own hardcoded /v1/... routes
    assert _strip_v1("https://openrouter.ai/api/v1") == "https://openrouter.ai/api"
    assert _strip_v1("https://openrouter.ai/api/v1/") == "https://openrouter.ai/api"
    assert _strip_v1("https://openrouter.ai/api") == "https://openrouter.ai/api"


def _user_text(body):
    c = body["messages"][0]["content"]
    if isinstance(c, list):
        return " ".join(b.get("text", "") for b in c)
    return c


class _Stream(httpx.AsyncByteStream):
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aiter__(self):
        for c in self._chunks:
            yield c


def mock_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    CAPTURED["user_text"] = _user_text(body)
    tokens = [t.strip(".,") for t in CAPTURED["user_text"].split()
              if any(ch.isdigit() for ch in t) or "." in t or "\\" in t]
    # model echoes each surrogate bolded + uppercased
    echoed = "Analysis of " + " ".join(f"**{t.upper()}**" for t in tokens)

    if body.get("stream"):
        chunks = [b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n']
        for i in range(0, len(echoed), 4):   # tiny pieces -> surrogates split across deltas
            evt = {"type": "content_block_delta", "index": 0,
                   "delta": {"type": "text_delta", "text": echoed[i:i + 4]}}
            chunks.append(f"event: content_block_delta\ndata: {json.dumps(evt)}\n\n".encode())
        chunks.append(b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n')
        return httpx.Response(200, headers={"content-type": "text/event-stream"},
                              stream=_Stream(chunks))

    return httpx.Response(200, headers={"content-type": "application/json"},
                          json={"type": "message", "role": "assistant",
                                "content": [{"type": "text", "text": echoed}]})


def make_client():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "proxytest"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(mock_handler))
    app = create_app(s, client=mock)
    return TestClient(app)


def test_nonstreaming_roundtrip():
    CAPTURED.clear()
    tc = make_client()
    r = tc.post("/v1/messages", json={
        "model": "claude-3-5-sonnet",
        "messages": [{"role": "user", "content": "Scan 10.20.0.10 on dc01.acmecorp.local"}],
    }, headers={"x-api-key": "test"})
    assert r.status_code == 200
    assert "10.20.0.10" not in CAPTURED["user_text"]      # upstream saw surrogate only
    assert "acmecorp" not in CAPTURED["user_text"]
    text = r.json()["content"][0]["text"]
    assert "10.20.0.10" in text                            # client got real value back
    assert "dc01.acmecorp.local" in text


def test_streaming_roundtrip():
    CAPTURED.clear()
    tc = make_client()
    with tc.stream("POST", "/v1/messages", json={
        "model": "claude-3-5-sonnet",
        "stream": True,
        "messages": [{"role": "user", "content": "Host dc01.acmecorp.local at 10.20.0.10"}],
    }, headers={"x-api-key": "test"}) as r:
        collected = "".join(r.iter_text())

    restored = ""
    for line in collected.splitlines():
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content_block_delta":
                restored += obj["delta"].get("text", "")

    assert "10.20.0.10" in restored
    assert "dc01.acmecorp.local" in restored


def _url_capturing_mock(request: httpx.Request) -> httpx.Response:
    CAPTURED["requested_url"] = str(request.url)
    return httpx.Response(200, headers={"content-type": "application/json"},
                          json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]})


def test_openai_upstream_with_trailing_v1_does_not_double_path():
    """OpenRouter (and every other OpenAI-compatible provider) documents its
    base_url INCLUDING /v1 — that must not collide with our own hardcoded
    /v1/chat/completions route into .../v1/v1/chat/completions (a 404)."""
    CAPTURED.clear()
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.openai_upstream = "https://openrouter.ai/api/v1"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_url_capturing_mock))
    tc = TestClient(create_app(s, client=mock))

    tc.post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "hi"}],
    })
    assert CAPTURED["requested_url"] == "https://openrouter.ai/api/v1/chat/completions"


def test_openai_upstream_bare_host_still_works():
    CAPTURED.clear()
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.openai_upstream = "https://api.openai.com"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_url_capturing_mock))
    tc = TestClient(create_app(s, client=mock))

    tc.post("/v1/chat/completions", json={
        "model": "test-model", "messages": [{"role": "user", "content": "hi"}],
    })
    assert CAPTURED["requested_url"] == "https://api.openai.com/v1/chat/completions"


def _echo_mock(request: httpx.Request) -> httpx.Response:
    # doesn't assume valid JSON — used for the malformed-body tests below
    return httpx.Response(200, headers={"content-type": "text/plain"}, text="ok")


def test_malformed_body_forwards_by_default():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "malformed"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_echo_mock))
    tc = TestClient(create_app(s, client=mock))
    r = tc.post("/v1/messages", content=b"not json",
                headers={"x-api-key": "test", "content-type": "application/json"})
    assert r.status_code == 200  # forwarded as-is, logged not blocked


def test_malformed_body_blocked_in_strict_mode():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "strict"
    s.strict_mode = True
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_echo_mock))
    tc = TestClient(create_app(s, client=mock))
    r = tc.post("/v1/messages", content=b"not json",
                headers={"x-api-key": "test", "content-type": "application/json"})
    assert r.status_code == 502
