"""
True streaming deanonymization of SSE responses.

Unlike the original (which buffered the whole response before restoring), this
restores surrogates *as they stream* using a per-content-block hold-back buffer
(`StreamRestorer`).  A surrogate split across two SSE deltas is reassembled
before it is restored, so the client still sees real values with low latency.
"""
from __future__ import annotations

import codecs
import json
import logging
from typing import AsyncIterator

log = logging.getLogger("anonproxy.stream")


async def _lines(aiter_bytes: AsyncIterator[bytes]) -> AsyncIterator[str]:
    """Yield complete text lines (without trailing newline) from a byte stream."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buf = ""
    async for chunk in aiter_bytes:
        buf += decoder.decode(chunk)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line
    buf += decoder.decode(b"", final=True)
    if buf:
        yield buf


def _data_event(event: str, payload: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n"


async def anthropic_stream(engine, aiter_bytes) -> AsyncIterator[str]:
    restorers: dict[int, object] = {}
    async for line in _lines(aiter_bytes):
        if not line.startswith("data:"):
            # event: / blank / comment lines pass through verbatim
            yield line + "\n"
            continue
        raw = line[len("data:"):].strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            yield line + "\n"
            continue

        etype = obj.get("type")
        if etype == "content_block_delta" and obj.get("delta", {}).get("type") == "text_delta":
            idx = obj.get("index", 0)
            sr = restorers.get(idx)
            if sr is None:
                sr = restorers[idx] = engine.stream_restorer()
            obj["delta"]["text"] = sr.push(obj["delta"].get("text", ""))
            yield f"data: {json.dumps(obj)}\n\n"
        elif etype == "content_block_stop":
            idx = obj.get("index", 0)
            sr = restorers.pop(idx, None)
            if sr is not None:
                tail = sr.flush()
                if tail:
                    yield _data_event("content_block_delta", {
                        "type": "content_block_delta", "index": idx,
                        "delta": {"type": "text_delta", "text": tail},
                    })
            yield f"data: {json.dumps(obj)}\n\n"
        else:
            yield f"data: {json.dumps(obj)}\n\n"


async def openai_stream(engine, aiter_bytes) -> AsyncIterator[str]:
    restorers: dict[int, object] = {}
    async for line in _lines(aiter_bytes):
        if not line.startswith("data:"):
            yield line + "\n"
            continue
        raw = line[len("data:"):].strip()
        if raw == "[DONE]":
            for idx, sr in restorers.items():
                tail = sr.flush()
                if tail:
                    chunk = {"choices": [{"index": idx, "delta": {"content": tail}}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            yield line + "\n"
            continue
        for choice in obj.get("choices", []):
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                idx = choice.get("index", 0)
                sr = restorers.get(idx)
                if sr is None:
                    sr = restorers[idx] = engine.stream_restorer()
                delta["content"] = sr.push(content)
        yield f"data: {json.dumps(obj)}\n\n"
