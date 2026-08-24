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
import time
from dataclasses import asdict
from pathlib import Path

from .config import Settings
from .engine import Engine


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE pairs without overriding real env vars.

    Searched in order: ``path`` (the cwd convention), then the repo root
    next to the installed package — so the CLI picks up your settings from
    any working directory, and after moving/renaming the repo.
    """
    candidates = [Path(path)]
    pkg_root_env = Path(__file__).resolve().parent.parent / ".env"
    if pkg_root_env.resolve() != Path(path).resolve():
        candidates.append(pkg_root_env)
    for p in candidates:
        if not p.exists():
            continue
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

    # --- profiles: per-test context, one JSON each ---------------------------
    prof = sub.add_parser("profile", help="manage engagement profiles")
    pp = prof.add_subparsers(dest="pcmd", required=True)
    pnew = pp.add_parser("new", help="create a profile")
    pnew.add_argument("name", help="profile/engagement name (e.g. acme-web)")
    pnew.add_argument("--scope", default="", help="comma list of scope terms")
    pnew.add_argument("--scope-file", default=None, dest="scope_file")
    pnew.add_argument("--detectors", default=None, help="comma chain (default regex,ollama)")
    pnew.add_argument("--model", default=None, help="Ollama model")
    pnew.add_argument("--ephemeral", action="store_true",
                      help="in-memory vault for this engagement (nothing on disk)")
    pnew.add_argument("--port", type=int, default=8099)
    pnew.add_argument("--notes", default="")
    pp.add_parser("list", help="list profiles")
    pshow = pp.add_parser("show", help="print one profile as JSON")
    pshow.add_argument("name")
    ped = pp.add_parser("edit", help="open a profile's JSON in your editor")
    ped.add_argument("name")
    prm = pp.add_parser("rm", help="delete a profile (vault/exports untouched)")
    prm.add_argument("name")

    up = sub.add_parser("up", help="start the proxy from a profile (default: most recent)")
    up.add_argument("profile", nargs="?", default=None)
    up.add_argument("--daemon", action="store_true",
                    help="run detached; manage with `anonproxy stop`")
    up.add_argument("--host", default=None)
    sub.add_parser("stop", help="stop daemonized `up` instance(s)")
    envp = sub.add_parser("env", help="print/copy client env lines for a profile")
    envp.add_argument("profile", nargs="?", default=None)
    envp.add_argument("--copy", action="store_true", dest="do_copy")
    mcpp = sub.add_parser("mcp", help="wrap an MCP stdio server (host -- server)")
    mcpp.add_argument("cmd", nargs=argparse.REMAINDER, metavar="COMMAND")

    closep = sub.add_parser("close", help="export mappings to evidence files + wipe vault")
    closep.add_argument("profile", nargs="?", default=None)
    closep.add_argument("--keep-vault", action="store_true", dest="keep_vault")

    args = p.parse_args(argv)

    if args.cmd == "wizard":
        from .wizard import run as wizard_run
        return wizard_run()

    # --- profiles: pure file management, no Settings needed ------------------
    from .profiles import (
        Profile,
        ProfileStore,
        client_env_lines,
        close_engagement,
        copy_to_clipboard,
        open_in_editor,
        sanitize_name,
    )
    store = ProfileStore()

    def _resolve_profile(name: str | None) -> Profile:
        prof = store.get(name) if name else store.most_recent()
        if prof is None and name is None:
            prof = Profile(name="default")   # first run: sensible starter
            store.save(prof)
        if prof is None:
            known = ", ".join(pr.name for pr in store.list()) or "(none yet)"
            raise SystemExit(f"no profile {name!r} — known: {known}\n"
                             f"create one: anonproxy profile new {name}")
        return prof

    if args.cmd == "profile":
        if args.pcmd == "new":
            name = sanitize_name(args.name)
            if store.exists(name):
                raise SystemExit(f"profile {name!r} already exists "
                                 f"({store.path(name)})")
            prof = Profile(name=name, notes=args.notes, port=args.port,
                           ephemeral=args.ephemeral,
                           scope_file=args.scope_file or "")
            if args.scope:
                prof.scope_terms = [x.strip() for x in args.scope.split(",") if x.strip()]
            if args.detectors:
                prof.detectors = [x.strip() for x in args.detectors.split(",") if x.strip()]
            if args.model:
                prof.ollama_model = args.model
            path = store.save(prof)
            print(f"saved {path}")
            print(f"start it:   anonproxy up {name}"
                  + ("  (--daemon to detach)" if not args.ephemeral else ""))
            print(f"client env: anonproxy env {name}")
            return 0
        if args.pcmd == "list":
            rows = store.list()
            if not rows:
                print("no profiles yet — create one: anonproxy profile new <name>")
                return 0
            print(f"{'NAME':<24} {'PORT':<6} {'DETECTORS':<28} EPHEMERAL  NOTES")
            for pr in rows:
                last = time.strftime("%Y-%m-%d", time.localtime(pr.last_used_at)) \
                    if pr.last_used_at else "never"
                notes = f"{pr.notes} (last used {last})" if pr.notes else \
                    (f"(last used {last})" if pr.last_used_at else "")
                print(f"{pr.name:<24} {pr.port:<6} "
                      f"{','.join(pr.detectors):<28} "
                      f"{'yes' if pr.ephemeral else 'no':<9}  {notes}")
            return 0
        if args.pcmd == "show":
            prof = _resolve_profile(args.name)
            print(json.dumps(asdict(prof), indent=2))
            return 0
        if args.pcmd == "edit":
            prof = _resolve_profile(args.name)
            open_in_editor(store.path(prof.name))
            print(f"opened {store.path(prof.name)}")
            return 0
        if args.pcmd == "rm":
            if store.delete(args.name):
                print(f"deleted {args.name} (vault/exports untouched)")
                return 0
            raise SystemExit(f"no profile {args.name!r}")
        raise SystemExit(f"unknown profile command {args.pcmd!r}")

    settings = Settings()
    if args.engagement:
        settings.engagement_id = args.engagement
    if getattr(args, "model", None):
        settings.ollama_model = args.model
    if getattr(args, "scope", None):
        settings.scope_terms = [x.strip() for x in args.scope.split(",") if x.strip()]
    if getattr(args, "scope_file", None):
        settings.scope_file = args.scope_file

    if args.cmd == "up":
        prof = _resolve_profile(args.profile)
        store.touch(prof.name)
        prof.apply(settings)
        if getattr(args, "host", None):
            settings.host = args.host
        if args.daemon:
            return _spawn_daemon(["up", prof.name], settings)
        _print_banner(settings)
        _serve(settings)
        return 0

    if args.cmd == "stop":
        return _stop_daemons()

    if args.cmd == "env":
        prof = _resolve_profile(args.profile)
        lines = client_env_lines(prof, host=settings.host)
        text = "\n".join(lines)
        if args.do_copy:
            ok = copy_to_clipboard(text + "\n")
            print(("copied to clipboard:\n" if ok else "clipboard unavailable, print only:\n")
                  + text)
        else:
            print(text)
        audit = f"http://{settings.host}:{prof.port}/audit"
        print(f"# audit: {audit}" +
              ("  (token-gated: see ANONPROXY_API_TOKEN)" if settings.engine_api_token else ""))
        return 0

    if args.cmd == "mcp":
        from . import mcp_wrapper
        sys.exit(mcp_wrapper.main(args.cmd, settings))

    if args.cmd == "close":
        prof = _resolve_profile(args.profile)
        prof.apply(settings)
        result = close_engagement(settings, keep_vault=args.keep_vault)
        if not result["count"]:
            print("no mappings on disk for this engagement (ephemeral session? "
                  "close while the proxy still holds them)")
        print(f"mappings exported: {result['count']}")
        print(f"  json: {result['json']}")
        print(f"  csv : {result['csv']}")
        print(f"vault removed: {'yes' if result['vault_removed'] else 'no'}"
              + (" (--keep-vault)" if args.keep_vault else ""))
        return 0

    if args.cmd == "verify":
        from . import verify
        report = verify.run(settings, use_llm=not args.no_llm)
        verify.print_report(report, show_mappings=args.show_mappings)
        tcp = report["tool_call_probe"]
        hard_fail = (report["total_leaks"] or report["roundtrip_failures"]
                     or report["preserved_failures"]
                     or report["adversarial"]["leaked"]
                     or tcp["anthropic_tool_use_leak"] or tcp["openai_tool_call_leak"])
        return 1 if hard_fail else 0

    if args.cmd == "audit":
        import webbrowser
        from urllib.parse import quote
        url = f"http://{settings.host}:{settings.port}/audit"
        if settings.engine_api_token:
            # fragment, not query: #fragments are never sent to the server, so
            # the token stays out of access logs; the page reads location.hash.
            url += "#token=" + quote(settings.engine_api_token, safe="")
        print(f"Opening {url}")
        webbrowser.open(url)
        return 0

    if args.cmd == "serve":
        if args.host:
            settings.host = args.host
        if args.port:
            settings.port = args.port
        _print_banner(settings)
        _serve(settings)
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


def _print_banner(settings) -> None:
    print(f"Anonproxy listening on http://{settings.host}:{settings.port} "
          f"(engagement={settings.engagement_id})", file=sys.stderr)
    print("  Claude Code:  export ANTHROPIC_BASE_URL=http://"
          f"{settings.host}:{settings.port}", file=sys.stderr)
    print("  OpenAI SDK:   base_url=http://"
          f"{settings.host}:{settings.port}/v1", file=sys.stderr)


def _serve(settings) -> None:
    import uvicorn

    from .proxy.app import create_app
    uvicorn.run(create_app(settings), host=settings.host,
                port=settings.port, log_level="info")


def _run_dir() -> Path:
    d = Path.home() / ".anonproxy" / "run"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _spawn_daemon(child_argv: list[str], settings) -> int:
    """Detach `up` so the terminal is free; `anonproxy stop` reaps it later."""
    import subprocess
    log_dir = Path.home() / ".anonproxy" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{settings.engagement_id}.log"
    with open(log_path, "ab") as logf:
        proc = subprocess.Popen(
            [sys.executable, "-m", "anonproxy", *child_argv],
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, cwd=os.getcwd(),
        )
    pidfile = _run_dir() / f"up-{settings.port}.pid"
    pidfile.write_text(str(proc.pid))
    print(f"started pid {proc.pid} (engagement={settings.engagement_id}, "
          f"port={settings.port})")
    print(f"  logs: {log_path}")
    print("  stop: anonproxy stop")
    return 0


def _stop_daemons() -> int:
    import signal
    import time as _time
    stopped = 0
    for pidfile in sorted(_run_dir().glob("*.pid")):
        try:
            pid = int(pidfile.read_text().strip())
        except ValueError:
            pidfile.unlink()
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(20):          # up to ~2s for a clean shutdown
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    break
                _time.sleep(0.1)
            else:
                os.kill(pid, signal.SIGKILL)
            stopped += 1
            print(f"stopped pid {pid} ({pidfile.name})")
        except (ProcessLookupError, PermissionError):
            print(f"pid {pid} not running — clearing stale pidfile")
        finally:
            try:
                pidfile.unlink()
            except FileNotFoundError:
                pass
    if not stopped:
        print("no daemonized instances found "
              "(menubar-managed proxies stop from the menu)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
