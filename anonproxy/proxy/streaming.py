"""
True streaming deanonymization of SSE responses.

Unlike the original (which buffered the whole response before restoring), this
restores surrogates *as they stream* using a per-content-block hold-back buffer
(`StreamRestorer`).  A surrogate split across two SSE deltas is reassembled
before it is restored, so the client still sees real values with low latency.

Two correctness contracts:

* Tool-argument fragments (Anthropic input_json_delta, OpenAI function
  arguments) are a JSON document the client assembles incrementally, so
  restored originals are JSON-string-escaped at substitution time
  (StreamRestorer(escape_originals=True), selected here via json_args=True).
  Plain text deltas are restored verbatim.
* A stream can end without its closing events (upstream 429/overload, cut
  connection). Both stream functions flush every outstanding hold-back
  buffer when the upstream ends, whatever way it ends — otherwise the held
  tail (for short blocks, the entire text) is silently swallowed.
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


# Anthropic content_block_delta.delta.type -> the field that carries the text
# to restore. text_delta covers plain text blocks; input_json_delta covers a
# streamed tool_use call's arguments (partial_json) and must be restored too,
# or a client executing the tool call acts on the surrogate instead of the
# real value.
_DELTA_FIELD = {"text_delta": "text", "input_json_delta": "partial_json"}


async def anthropic_stream(engine, aiter_bytes) -> AsyncIterator[str]:
    restorers: dict[int, tuple[object, str]] = {}  # idx -> (StreamRestorer, delta type)
    try:
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
            dtype = (obj.get("delta") or {}).get("type")
            field = _DELTA_FIELD.get(dtype)
            if etype == "content_block_delta" and field:
                idx = obj.get("index", 0)
                entry = restorers.get(idx)
                if entry is None:
                    entry = restorers[idx] = (
                        engine.stream_restorer(json_args=(dtype == "input_json_delta")),
                        dtype,
                    )
                sr, _ = entry
                obj["delta"][field] = sr.push(obj["delta"].get(field, ""))
                yield f"data: {json.dumps(obj)}\n\n"
            elif etype == "content_block_stop":
                idx = obj.get("index", 0)
                entry = restorers.pop(idx, None)
                if entry is not None:
                    sr, dtype = entry
                    tail = sr.flush()
                    if tail:
                        field = _DELTA_FIELD[dtype]
                        yield _data_event("content_block_delta", {
                            "type": "content_block_delta", "index": idx,
                            "delta": {"type": dtype, field: tail},
                        })
                yield f"data: {json.dumps(obj)}\n\n"
            else:
                yield f"data: {json.dumps(obj)}\n\n"
    finally:
        # Upstream ended without content_block_stop events (429/overload,
        # cut connection, error event): flush every outstanding hold-back
        # buffer so the client still receives the restored tail.
        for idx, (sr, dtype) in list(restorers.items()):
            tail = sr.flush()
            if tail:
                field = _DELTA_FIELD[dtype]
                yield _data_event("content_block_delta", {
                    "type": "content_block_delta", "index": idx,
                    "delta": {"type": dtype, field: tail},
                })
            restorers.pop(idx, None)


async def openai_stream(engine, aiter_bytes) -> AsyncIterator[str]:
    restorers: dict[int, object] = {}                     # choice index -> content restorer
    tool_restorers: dict[tuple[int, int], object] = {}    # (choice index, call index) -> restorer

    def _flush_all() -> AsyncIterator[str]:
        async def _gen():
            for idx, sr in restorers.items():
                tail = sr.flush()
                if tail:
                    chunk = {"choices": [{"index": idx, "delta": {"content": tail}}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
            for (cidx, tidx), sr in tool_restorers.items():
                tail = sr.flush()
                if tail:
                    chunk = {"choices": [{"index": cidx, "delta": {"tool_calls": [
                        {"index": tidx, "function": {"arguments": tail}}
                    ]}}]}
                    yield f"data: {json.dumps(chunk)}\n\n"
            restorers.clear()
            tool_restorers.clear()
        return _gen()

    try:
        async for line in _lines(aiter_bytes):
            if not line.startswith("data:"):
                yield line + "\n"
                continue
            raw = line[len("data:"):].strip()
            if raw == "[DONE]":
                async for piece in _flush_all():
                    yield piece
                yield "data: [DONE]\n\n"
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                yield line + "\n"
                continue
            for choice in obj.get("choices", []):
                delta = choice.get("delta") or {}
                cidx = choice.get("index", 0)
                content = delta.get("content")
                if isinstance(content, str) and content:
                    sr = restorers.get(cidx)
                    if sr is None:
                        sr = restorers[cidx] = engine.stream_restorer()
                    delta["content"] = sr.push(content)
                # streamed function-call arguments — same leak risk as content
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str) and args:
                        tidx = tc.get("index", 0)
                        key = (cidx, tidx)
                        sr = tool_restorers.get(key)
                        if sr is None:
                            sr = tool_restorers[key] = engine.stream_restorer(json_args=True)
                        fn["arguments"] = sr.push(args)
            yield f"data: {json.dumps(obj)}\n\n"
    finally:
        # Upstream ended without [DONE]: same contract as anthropic_stream.
        async for piece in _flush_all():
            yield piece
