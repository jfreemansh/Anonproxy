"""Streaming tool_use arguments stay syntactically valid while restoring.

Clients (Claude Code) concatenate input_json_delta.partial_json chunks and
parse the accumulated STRING as JSON — so any original value we inject in
place of a surrogate must be JSON-escaped for that context, even though
plain text_delta content must NOT be.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine
from anonproxy.config import Settings
from anonproxy.proxy.streaming import anthropic_stream


def _engine_with_host():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    eng = Engine(settings=s)
    # CREDENTIAL whose ORIGINAL contains JSON-structural characters: if the
    # streamed restoration injects them raw into partial_json, clients that
    # parse the accumulated string (Claude Code) explode.
    eng.anonymize('secret: P@ss"word')
    eng.anonymize("connect to dc01.acmecorp.local")
    return eng


async def _collect(engine, events):
    async def gen():
        for e in events:
            yield e.encode()
    out = []
    async for piece in anthropic_stream(engine, gen()):
        out.append(piece)
    return "".join(out)


def test_partial_json_surrogate_split_and_hostile_original():
    engine = _engine_with_host()

    # surrogate for dc01.acmecorp.local, discovered from the seeded vault
    surr = engine.vault.surrogate_for("dc01.acmecorp.local")
    assert surr

    cred_surr = None
    for surrogate, original in engine.vault.all_mappings():
        if "P@ss" in original:
            cred_surr = surrogate
    inner = (f'{{"command":"ssh admin@{surr}",'
             f'"secret":"{cred_surr}","ok":true}}')
    # three deltas cutting INSIDE the credential surrogate
    cut1 = inner.index(cred_surr) + len(cred_surr)//2
    cut2 = cut1 + 3
    deltas = [inner[:cut1], inner[cut1:cut2], inner[cut2:]]

    events = [
        'event: message_start\ndata: {"type":"message_start"}\n\n',
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","name":"bash","id":"t1"}}\n\n',
    ] + [
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta",
                                         "partial_json": d}}) + '\n\n'
        for d in deltas
    ] + [
        'data: {"type":"content_block_stop","index":0}\n\n',
    ]

    collected = asyncio_run_collect(engine, events)

    # reconstruct exactly like Claude Code does
    partial = ""
    for line in collected.splitlines():
        if line.startswith("data:"):
            obj = json.loads(line[5:])
            if obj.get("type") == "content_block_delta":
                partial += obj["delta"].get("partial_json", "")
    parsed = json.loads(partial)          # MUST be valid JSON
    assert parsed["command"] == "ssh admin@dc01.acmecorp.local"
    assert parsed["secret"] == 'P@ss"word'


def asyncio_run_collect(engine, events):
    import asyncio
    return asyncio.run(_collect(engine, events))


def test_push_path_emission_escapes_hostile_original():
    """The credential surrogate COMPLETES mid-delta so restoration happens in
    push() (not flush()) — the emitted fragment must still be valid JSON."""
    engine = _engine_with_host()
    cred_surr = None
    for surrogate, original in engine.vault.all_mappings():
        if "P@ss" in original:
            cred_surr = surrogate
    assert cred_surr

    inner = f'{{"secret":"{cred_surr}","pad":"{("x" * 80)}"}}'
    # cut AFTER the completed surrogate + margin → push() emits it restored
    cut = inner.index(cred_surr) + len(cred_surr) + 40
    events = [
        'data: {"type":"content_block_start","index":0,"content_block":'
        '{"type":"tool_use","name":"bash","id":"t1"}}\n\n',
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta",
                                         "partial_json": inner[:cut]}}) + '\n\n',
        'data: ' + json.dumps({"type": "content_block_delta", "index": 0,
                               "delta": {"type": "input_json_delta",
                                         "partial_json": inner[cut:]}}) + '\n\n',
        'data: {"type":"content_block_stop","index":0}\n\n',
    ]
    collected = asyncio_run_collect(engine, events)

    partial = ""
    for line in collected.splitlines():
        if line.startswith("data:") and "partial_json" in line:
            obj = json.loads(line[5:])
            if obj.get("type") == "content_block_delta":
                partial += obj["delta"].get("partial_json", "")
    assert '"secret":"' in partial          # surrogate already restored via push()
    parsed = json.loads(partial)            # MUST be valid JSON
    assert parsed["secret"] == 'P@ss"word'
