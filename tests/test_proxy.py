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
from anonproxy.proxy.app import create_app

CAPTURED = {}


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
