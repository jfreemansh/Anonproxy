"""Tool-call payloads must be anonymized outbound and restored inbound —
previously bypassed entirely (Anthropic tool_use.input, OpenAI
tool_calls[].function.arguments), both in plain JSON and while streaming.
"""
import json
import os
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine, Settings
from anonproxy.config import Settings as ProxySettings
from anonproxy.proxy import transform
from anonproxy.proxy.app import create_app


def fresh_engine():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    return Engine(engagement="toolcalls", settings=s)


# -- outbound anonymization ---------------------------------------------------

def test_anthropic_tool_use_input_is_anonymized():
    engine = fresh_engine()
    body = {
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "run_ssh",
                 "input": {"host": "10.20.0.10", "user": "admin"}},
            ]},
        ]
    }
    out = transform.anonymize_anthropic_request(engine, body)
    block = out["messages"][0]["content"][0]
    assert block["input"]["host"] != "10.20.0.10"
    assert "10.20.0.10" not in json.dumps(out)


def test_openai_tool_call_arguments_is_anonymized():
    engine = fresh_engine()
    body = {
        "messages": [
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "c1", "type": "function", "function": {
                    "name": "run_ssh",
                    "arguments": json.dumps({"host": "10.20.0.10", "user": "admin"}),
                }},
            ]},
        ]
    }
    out = transform.anonymize_openai_request(engine, body)
    args = json.loads(out["messages"][0]["tool_calls"][0]["function"]["arguments"])
    assert args["host"] != "10.20.0.10"
    assert "10.20.0.10" not in json.dumps(out)


def test_verify_probe_catches_the_regression_if_reintroduced(monkeypatch):
    """The verify.py preflight probe exists specifically to catch this bug
    class again. Prove it's sensitive, not just quiet when things are fine —
    without this, the probe could tautologically report "ok" and nobody would
    notice it stopped checking anything.
    """
    from anonproxy.verify import _tool_call_probe

    engine = fresh_engine()

    def old_anon_content(engine, content):  # pre-fix behavior: no tool_use.input handling
        if isinstance(content, str):
            return transform._anon_str(engine, content)
        if isinstance(content, list):
            return [dict(b) if isinstance(b, dict) else b for b in content]
        return content

    monkeypatch.setattr(transform, "_anon_content", old_anon_content)
    assert _tool_call_probe(engine)["anthropic_tool_use_leak"] is True

    monkeypatch.setattr(transform, "_anon_tool_call", lambda engine, tc: tc)  # pre-fix: no-op
    assert _tool_call_probe(engine)["openai_tool_call_leak"] is True


# -- streaming restoration, end to end through the proxy ---------------------

CAPTURED = {}


def _anthropic_tool_use_mock(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    text = body["messages"][0]["content"]
    CAPTURED["outbound"] = text
    surrogate = "".join(t for t in text.split() if "." in t or any(c.isdigit() for c in t))

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            events = [
                {"type": "content_block_start", "index": 0,
                 "content_block": {"type": "tool_use", "id": "t1", "name": "run_ssh", "input": {}}},
            ]
            partial = json.dumps({"host": surrogate})
            for i in range(0, len(partial), 5):  # split mid-token, like a real stream
                events.append({"type": "content_block_delta", "index": 0,
                                "delta": {"type": "input_json_delta", "partial_json": partial[i:i + 5]}})
            events.append({"type": "content_block_stop", "index": 0})
            for e in events:
                yield f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode()

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream())


def test_anthropic_streaming_tool_use_is_restored():
    CAPTURED.clear()
    s = ProxySettings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "toolstream"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_anthropic_tool_use_mock))
    tc = TestClient(create_app(s, client=mock))

    with tc.stream("POST", "/v1/messages", json={
        "model": "claude-3-5-sonnet", "stream": True,
        "messages": [{"role": "user", "content": "Scan 10.20.0.10"}],
    }, headers={"x-api-key": "test"}) as r:
        collected = "".join(r.iter_text())

    assert "10.20.0.10" not in CAPTURED["outbound"]  # upstream never saw the real IP

    partial_json = ""
    for line in collected.splitlines():
        if line.startswith("data:"):
            try:
                obj = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "content_block_delta":
                partial_json += obj["delta"].get("partial_json", "")
    assert json.loads(partial_json) == {"host": "10.20.0.10"}


def _openai_tool_call_mock(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    text = body["messages"][0]["content"]
    surrogate = "".join(t for t in text.split() if "." in t or any(c.isdigit() for c in t))

    class _Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            partial = json.dumps({"host": surrogate})
            for i in range(0, len(partial), 5):
                chunk = {"choices": [{"index": 0, "delta": {"tool_calls": [
                    {"index": 0, "function": {"arguments": partial[i:i + 5]}}
                ]}}]}
                yield f"data: {json.dumps(chunk)}\n\n".encode()
            yield b"data: [DONE]\n\n"

    return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=_Stream())


def test_openai_streaming_tool_call_arguments_is_restored():
    s = ProxySettings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "toolstream-openai"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_openai_tool_call_mock))
    tc = TestClient(create_app(s, client=mock))

    with tc.stream("POST", "/v1/chat/completions", json={
        "model": "gpt-4", "stream": True,
        "messages": [{"role": "user", "content": "Scan 10.20.0.10"}],
    }, headers={"authorization": "Bearer test"}) as r:
        collected = "".join(r.iter_text())

    args = ""
    for line in collected.splitlines():
        if line.startswith("data:") and "[DONE]" not in line:
            obj = json.loads(line[5:].strip())
            for choice in obj["choices"]:
                for tc_ in choice["delta"].get("tool_calls", []):
                    args += tc_["function"]["arguments"]
    assert json.loads(args) == {"host": "10.20.0.10"}


# -- streaming error status propagation --------------------------------------

def _error_mock(request: httpx.Request) -> httpx.Response:
    return httpx.Response(429, json={"error": "rate limited"})


def test_streaming_upstream_error_status_is_propagated():
    s = ProxySettings()
    s.ephemeral = True
    s.llm_enabled = False
    mock = httpx.AsyncClient(transport=httpx.MockTransport(_error_mock))
    tc = TestClient(create_app(s, client=mock))

    r = tc.post("/v1/messages", json={
        "model": "claude-3-5-sonnet", "stream": True,
        "messages": [{"role": "user", "content": "hi"}],
    }, headers={"x-api-key": "test"})
    assert r.status_code == 429
