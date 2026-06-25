"""Command line entrypoint.

    python -m anonproxy serve                 # start the proxy
    python -m anonproxy anon  < file.txt      # anonymize stdin
    python -m anonproxy deanon < file.txt     # restore stdin
    python -m anonproxy stats                 # vault stats
    python -m anonproxy export                # dump mappings as JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .config import Settings
from .engine import Engine


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs from a .env in the cwd without overriding real env."""
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


def main(argv=None) -> int:
    _load_dotenv()
    p = argparse.ArgumentParser(prog="anonproxy", description="Reversible LLM anonymization proxy")
    p.add_argument("--engagement", help="engagement id (overrides $ENGAGEMENT_ID)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the reverse proxy")
    s.add_argument("--host", default=None)
    s.add_argument("--port", type=int, default=None)
    s.add_argument("--model", default=None,
                   help="Ollama model to use (overrides OLLAMA_MODEL), e.g. qwen3:4b, qwen3.6:27b")
    s.add_argument("--scope", default=None,
                   help="comma list of client domains/hostnames/orgs to always anonymize")
    s.add_argument("--scope-file", default=None, dest="scope_file",
                   help="path to a scope file (one term per line, optional value=TYPE)")

    sub.add_parser("wizard", help="interactive local setup")
    sub.add_parser("audit", help="open the audit dashboard in a browser")
    v = sub.add_parser("verify", help="run tool-output fixtures and report leaks")
    v.add_argument("--no-llm", action="store_true", help="regex-only (skip Ollama)")
    v.add_argument("--model", default=None, help="Ollama model to verify with")
    v.add_argument("--scope", default=None, help="comma list of scope terms")
    v.add_argument("--scope-file", default=None, dest="scope_file", help="path to a scope file")
    v.add_argument("--show-mappings", "--audit", action="store_true", dest="show_mappings",
                   help="print the anonymized output + original→surrogate table")
    a = sub.add_parser("anon", help="anonymize stdin")
    a.add_argument("--scope", default=None, help="comma list of scope terms")
    a.add_argument("--scope-file", default=None, dest="scope_file", help="path to a scope file")
    sub.add_parser("deanon", help="deanonymize stdin")
    sub.add_parser("stats", help="show vault stats")
    sub.add_parser("export", help="dump mappings")

    args = p.parse_args(argv)

    if args.cmd == "wizard":
        from .wizard import run as wizard_run
        return wizard_run()

    settings = Settings()
    if args.engagement:
        settings.engagement_id = args.engagement
    if getattr(args, "model", None):
        settings.ollama_model = args.model
    if getattr(args, "scope", None):
        settings.scope_terms = [x.strip() for x in args.scope.split(",") if x.strip()]
    if getattr(args, "scope_file", None):
        settings.scope_file = args.scope_file

    if args.cmd == "verify":
        from . import verify
        report = verify.run(settings, use_llm=not args.no_llm)
        verify.print_report(report, show_mappings=args.show_mappings)
        hard_fail = (report["total_leaks"] or report["roundtrip_failures"]
                     or report["adversarial"]["leaked"])
        return 1 if hard_fail else 0

    if args.cmd == "audit":
        import webbrowser
        url = f"http://{settings.host}:{settings.port}/audit"
        if settings.engine_api_token:
            url += f"?token={settings.engine_api_token}"
        print(f"Opening {url}")
        webbrowser.open(url)
        return 0

    if args.cmd == "serve":
        import uvicorn
        from .proxy.app import create_app
        if args.host:
            settings.host = args.host
        if args.port:
            settings.port = args.port
        app = create_app(settings)
        print(f"Anonproxy listening on http://{settings.host}:{settings.port} "
              f"(engagement={settings.engagement_id})", file=sys.stderr)
        print("  Claude Code:  export ANTHROPIC_BASE_URL=http://"
              f"{settings.host}:{settings.port}", file=sys.stderr)
        print("  OpenAI SDK:   base_url=http://"
              f"{settings.host}:{settings.port}/v1", file=sys.stderr)
        uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")
        return 0

    engine = Engine(settings=settings)
    if args.cmd == "anon":
        sys.stdout.write(engine.anonymize(sys.stdin.read()))
    elif args.cmd == "deanon":
        sys.stdout.write(engine.deanonymize(sys.stdin.read()))
    elif args.cmd == "stats":
        print(json.dumps(engine.stats(), indent=2))
    elif args.cmd == "export":
        print(json.dumps(engine.export(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
