"""anonproxy mcp: server->host redacted, host->server untouched.

In-process harness: swap sys.stdin/stdout so wrap() talks to buffers — no
nested interpreters, hard timeout so a regression fails instead of hanging.
"""
import asyncio
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import mcp_wrapper  # noqa: E402
from anonproxy.config import Settings  # noqa: E402

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                     "fixtures", "mcp_fake_child.py")


def _settings():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    return s


def _run(lines, child=None):
    """Returns (raw_stdout_lines, parsed_json_msgs)."""
    async def go():
        engine = mcp_wrapper.Engine(settings=_settings())
        argv = child or [sys.executable, CHILD]
        return await asyncio.wait_for(
            mcp_wrapper.wrap(argv, engine), timeout=20)

    in_buf = io.StringIO("\n".join(lines) + "\n")
    out_buf = io.StringIO()
    real_in, real_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = in_buf, out_buf
    try:
        rc = asyncio.run(go())
    finally:
        sys.stdin, sys.stdout = real_in, real_out
    assert rc == 0
    raw = out_buf.getvalue().splitlines()
    return raw, [json.loads(line) for line in raw if line.strip().startswith("{")]


def test_tool_results_redacted_host_traffic_untouched():
    _, msgs = _run([
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                    "params": {"name": "nmap",
                               "arguments": {"h": "10.20.0.10"}}}),
        json.dumps({"jsonrpc": "2.0", "id": 8, "method": "ping",
                    "params": {"h": "10.20.0.10"}}),
    ])
    tool_resp = next(m for m in msgs if m.get("id") == 7)
    text = tool_resp["result"]["content"][0]["text"]
    assert "10.20.0.10" not in text and "dc01.acme.local" not in text
    assert "up" in text                                    # content preserved

    echoed = next(m for m in msgs if "echo" in m)
    assert echoed["echo"]["params"]["h"] == "10.20.0.10", (
        "host->server traffic must NOT be rewritten")


def test_non_json_lines_relayed_verbatim():
    child = [sys.executable, "-c",
             f"print('not-json-noise')\nexec(open({CHILD!r}).read())"]
    raw, msgs = _run([json.dumps({"jsonrpc": "2.0", "id": 1,
                                  "method": "tools/list"})], child=child)
    assert raw[0] == "not-json-noise", "non-JSON output must pass verbatim"
    assert any("tools/list" in json.dumps(m) for m in msgs)
