"""Gemini (generativelanguage) route: anonymized out, restored back."""
import json
import os
import sys

import httpx
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.proxy.app import create_app

CAPTURED = {}


def gemini_handler(request: httpx.Request) -> httpx.Response:
    CAPTURED["url"] = str(request.url)
    body = json.loads(request.content)
    texts = []
    for c in body.get("contents", []):
        for part in c.get("parts", []):
            if "text" in part:
                texts.append(part["text"])
    CAPTURED["user_text"] = " ".join(texts)
    # model echoes the surrogate, bolded + uppercased (worst case for restore)
    echoed = " ".join(
        f"**{t.strip('.,').upper()}**" for t in CAPTURED["user_text"].split()
        if any(ch.isdigit() for ch in t) or "." in t)
    return httpx.Response(200, headers={"content-type": "application/json"},
                          json={"candidates": [{"content": {"role": "model", "parts": [
                              {"text": f"Analysis: {echoed}"}]}}]})


def make_client():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "gemtest"
    mock = httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler))
    return TestClient(create_app(s, client=mock))


def test_generate_content_roundtrip():
    CAPTURED.clear()
    tc = make_client()
    r = tc.post("/v1beta/models/gemini-2.5-flash:generateContent?key=k",
                json={"contents": [{"role": "user", "parts": [
                    {"text": "Scan 10.20.0.10 on dc01.acmecorp.local"}]}]})
    assert r.status_code == 200
    assert "10.20.0.10" not in CAPTURED["user_text"], "upstream saw real data"
    assert "acmecorp" not in CAPTURED["user_text"]
    assert CAPTURED["url"].startswith("https://generativelanguage.googleapis.com/v1beta/models/")
    text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    assert "10.20.0.10" in text, "client got surrogate back instead of real value"
    assert "dc01.acmecorp.local" in text


def test_count_tokens_is_anonymized_too():
    CAPTURED.clear()
    tc = make_client()
    r = tc.post("/v1beta/models/gemini-2.5-flash:countTokens",
                json={"contents": [{"parts": [{"text": "host 10.20.0.10"}]}]})
    assert r.status_code == 200
    assert "10.20.0.10" not in CAPTURED["user_text"]


def test_unknown_gemini_action_refused():
    tc = make_client()
    r = tc.post("/v1beta/models/m:makeSandwich", json={"contents": []})
    assert r.status_code == 404
