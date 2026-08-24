"""Fake MCP server: echoes requests; tools/call returns real client data."""
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue
    if req.get("method") == "tools/call":
        out = {"jsonrpc": "2.0", "id": req.get("id"), "result": {"content": [
            {"type": "text", "text": "nmap done: host 10.20.0.10 (dc01.acme.local) up"}]}}
    else:
        # verbatim echo proves host->server traffic is untouched
        out = {"echo": req}
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()
