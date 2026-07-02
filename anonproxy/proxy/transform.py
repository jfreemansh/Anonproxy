"""Anonymize outbound request payloads; deanonymize inbound responses.

Handles both the Anthropic Messages shape and the OpenAI chat-completions shape.
Only natural-language / tool-output fields are anonymized — model names, roles,
and control fields are left untouched. Tool-call payloads (Anthropic
``tool_use.input``, OpenAI ``tool_calls[].function.arguments``) ARE anonymized:
once the client echoes a prior assistant turn back in conversation history, any
real values used in a tool call (hosts, creds, paths) would otherwise reach the
LLM provider unredacted.
"""
from __future__ import annotations

import json
from typing import Any


def _anon_str(engine, s: str) -> str:
    if isinstance(s, str) and s.strip():
        return engine.anonymize(s, is_tool_output=True)
    return s


def _anon_value(engine, val: Any) -> Any:
    """Recursively anonymize every string leaf in an arbitrary JSON value."""
    if isinstance(val, str):
        return _anon_str(engine, val)
    if isinstance(val, list):
        return [_anon_value(engine, v) for v in val]
    if isinstance(val, dict):
        return {k: _anon_value(engine, v) for k, v in val.items()}
    return val


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
                # Anthropic tool_use: input is the real tool-call arguments
                if b.get("type") == "tool_use" and isinstance(b.get("input"), dict):
                    b["input"] = _anon_value(engine, b["input"])
                out.append(b)
            else:
                out.append(block)
        return out
    return content


def _anon_tool_call(engine, tc: Any) -> Any:
    """OpenAI tool_calls[]: function.arguments is a JSON-encoded string."""
    if not isinstance(tc, dict):
        return tc
    tc = dict(tc)
    fn = tc.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("arguments"), str):
        try:
            args = json.loads(fn["arguments"])
        except json.JSONDecodeError:
            args = None
        if args is not None:
            fn = dict(fn)
            fn["arguments"] = json.dumps(_anon_value(engine, args))
            tc["function"] = fn
    return tc


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
            if not isinstance(m, dict):
                new.append(m)
                continue
            m = dict(m)
            if "content" in m:
                m["content"] = _anon_content(engine, m["content"])
            if isinstance(m.get("tool_calls"), list):
                m["tool_calls"] = [_anon_tool_call(engine, tc) for tc in m["tool_calls"]]
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
