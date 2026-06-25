"""Anonymize outbound request payloads; deanonymize inbound responses.

Handles both the Anthropic Messages shape and the OpenAI chat-completions shape.
Only natural-language / tool-output fields are anonymized — model names, roles,
and control fields are left untouched.
"""
from __future__ import annotations

from typing import Any


def _anon_str(engine, s: str) -> str:
    if isinstance(s, str) and s.strip():
        return engine.anonymize(s, is_tool_output=True)
    return s


def _anon_content(engine, content: Any) -> Any:
    """Content may be a plain string or a list of typed blocks."""
    if isinstance(content, str):
        return _anon_str(engine, content)
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, dict):
                b = dict(block)
                if isinstance(b.get("text"), str):
                    b["text"] = _anon_str(engine, b["text"])
                # Anthropic tool_result: content is str or list of text blocks
                if "content" in b:
                    b["content"] = _anon_content(engine, b["content"])
                out.append(b)
            else:
                out.append(block)
        return out
    return content


def anonymize_anthropic_request(engine, body: dict) -> dict:
    body = dict(body)
    if "system" in body:
        body["system"] = _anon_content(engine, body["system"])
    if isinstance(body.get("messages"), list):
        body["messages"] = [
            {**m, "content": _anon_content(engine, m.get("content"))}
            if isinstance(m, dict) else m
            for m in body["messages"]
        ]
    return body


def anonymize_openai_request(engine, body: dict) -> dict:
    body = dict(body)
    if isinstance(body.get("messages"), list):
        new = []
        for m in body["messages"]:
            if isinstance(m, dict) and "content" in m:
                new.append({**m, "content": _anon_content(engine, m["content"])})
            else:
                new.append(m)
        body["messages"] = new
    return body


def deanonymize_json(engine, obj: Any) -> Any:
    """Recursively restore real values in any JSON-like response structure."""
    if isinstance(obj, str):
        return engine.deanonymize(obj)
    if isinstance(obj, list):
        return [deanonymize_json(engine, x) for x in obj]
    if isinstance(obj, dict):
        return {k: deanonymize_json(engine, v) for k, v in obj.items()}
    return obj
