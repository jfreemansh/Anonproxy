"""
MCP stdio wrapper — run any MCP server under the anonymizer.

    anonproxy mcp -- <server-command> [args...]

Point your MCP host (Claude Code, Claude Desktop, ...) at this instead of the
server binary. Host -> server traffic passes through untouched (tool-call
arguments originate locally); server -> host traffic has every detected
entity replaced before it enters model context, so tool RESULTS from client
infrastructure never leak upstream. Restoration happens on the way back out
via the normal engine vault, so the host still reads real values.

Framing: MCP stdio is newline-delimited JSON-RPC. Lines that fail to parse as
JSON are relayed verbatim (defensive; spec says they shouldn't occur).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from .config import Settings
from .engine import Engine

log = logging.getLogger("anonproxy.mcp")


def anonymize_mcp_message(engine: Engine, obj):
    """Rewrite text-bearing leaves of an outbound MCP message.

    Covered shapes: result.content[] entries ({type:'text', text}), legacy
    string `content`, resource contents[].text, and any other 'text' leaf.
    Everything else (ids, methods, params, metadata) is left intact.
    """
    return _walk(engine, obj)


def _walk(engine, val):
    if isinstance(val, list):
        return [_walk(engine, v) for v in val]
    if isinstance(val, dict):
        out = {}
        for k, v in val.items():
            if isinstance(v, str) and k in ("text", "content"):
                out[k] = engine.anonymize(v, is_tool_output=True)
            else:
                out[k] = _walk(engine, v)
        return out
    return val


async def wrap(argv: list[str], engine: Engine) -> int:
    if not argv:
        print("mcp: no server command given "
              "(usage: anonproxy mcp -- <command> [args...])", file=sys.stderr)
        return 2

    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,                       # server logs flow straight through
    )

    async def host_to_server():
        loop = asyncio.get_event_loop()
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            proc.stdin.write(line.encode())
            await proc.stdin.drain()
        try:
            proc.stdin.close()
        except (BrokenPipeError, ProcessLookupError):
            pass

    async def server_to_host():
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            line = raw.decode(errors="replace").rstrip("\n")
            if line.lstrip().startswith(("{", "[")):
                try:
                    msg = json.loads(line)
                    msg = anonymize_mcp_message(engine, msg)
                    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
                    sys.stdout.flush()
                    continue
                except json.JSONDecodeError:
                    pass                      # defensive: relay non-JSON verbatim
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    host_task = asyncio.create_task(host_to_server())
    server_task = asyncio.create_task(server_to_host())

    # Drain stdin first; keep reading child output until it EOFs (well-behaved
    # MCP servers exit on stdin EOF). A lingering server gets a short grace
    # period, then termination — cancelling the reader outright would discard
    # buffered results (the exact bug this wrapper's tests exist to prevent).
    await host_task
    try:
        await asyncio.wait_for(server_task, timeout=10)
    except asyncio.TimeoutError:
        log.warning("MCP server did not exit after stdin EOF — terminating")
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(server_task, timeout=5)
        except asyncio.TimeoutError:
            pass
    rc = await proc.wait()
    return rc if rc >= 0 else 0


def main(argv: list[str], settings: Settings | None = None) -> int:
    logging.basicConfig(level=logging.WARNING,
                        format="%(name)s %(levelname)s %(message)s")
    engine = Engine(settings=settings or Settings())
    return asyncio.run(wrap(argv, engine))
