"""
Pre-engagement coverage check.

Runs realistic pentest tool outputs (nmap, secretsdump, netexec, configs, HTTP)
through the full pipeline and reports:

* **Leaks** — sensitive strings that survived anonymization (would reach the LLM).
* **Round-trip** — whether deanonymizing the result reproduces the original.

If a local Ollama model is up it is used automatically, so this doubles as a
"does my local model actually work" check. Secrets that only the LLM layer can
catch (bare hostnames, unlabelled creds, person names) are marked accordingly:
in regex-only mode they are reported as *needs-LLM* rather than counted as leaks.

Exit code is non-zero if any real leak is found.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .config import Settings
from .engine import Engine
from .proxy import transform


@dataclass
class Fixture:
    name: str
    text: str
    # (sensitive_string, layer) where layer is "regex" (must be caught even
    # without Ollama) or "llm" (needs the local model).
    secrets: list[tuple[str, str]] = field(default_factory=list)


FIXTURES: list[Fixture] = [
    Fixture(
        "nmap -sV",
        "Nmap scan report for dc01.acmecorp.local (10.10.10.5)\n"
        "Host is up (0.0012s latency).\n"
        "PORT     STATE SERVICE       VERSION\n"
        "445/tcp  open  microsoft-ds  Microsoft Windows Server 2019\n"
        "3389/tcp open  ms-wbt-server Microsoft Terminal Services\n"
        "Service Info: Host: DC01; OS: Windows",
        [("dc01.acmecorp.local", "regex"), ("10.10.10.5", "regex"), ("DC01", "llm")],
    ),
    Fixture(
        "secretsdump (NTLM)",
        "[*] Dumping Domain Credentials (domain\\uid:rid:lmhash:nthash)\n"
        "acmecorp.local\\Administrator:500:aad3b435b51404eeaad3b435b51404ee:"
        "8846f7eaee8fb117ad06bdd830b7586c:::\n"
        "acmecorp.local\\jsmith:1103:aad3b435b51404eeaad3b435b51404ee:"
        "5835048ce94ad0564e29a924a03510ef:::",
        [("acmecorp.local", "regex"),
         ("8846f7eaee8fb117ad06bdd830b7586c", "regex"),
         ("5835048ce94ad0564e29a924a03510ef", "regex")],
    ),
    Fixture(
        "netexec / CME",
        "SMB  10.10.10.5  445  DC01  [+] acmecorp.local\\jsmith:Summer2024! (Pwn3d!)",
        [("10.10.10.5", "regex"), ("acmecorp.local", "regex"),
         ("DC01", "llm"), ("Summer2024!", "llm")],
    ),
    Fixture(
        "app config",
        "database:\n"
        "  host: prod-db-01.acmecorp.local\n"
        "  password: P@ssw0rd!2024\n"
        "api_key: AKIAIOSFODNN7EXAMPLE\n"
        "aws_secret: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        [("prod-db-01.acmecorp.local", "regex"), ("P@ssw0rd!2024", "regex"),
         ("AKIAIOSFODNN7EXAMPLE", "regex"),
         ("wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "regex")],
    ),
    Fixture(
        "HTTP request (web app)",
        "POST /checkout HTTP/1.1\n"
        "Host: shop.acmecorp.com\n"
        "Cookie: PHPSESSID=9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c\n"
        "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiYWRtaW4ifQ."
        "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789\n"
        '{"email":"john@acmecorp.com","card":"4111 1111 1111 1111"}',
        [("shop.acmecorp.com", "regex"),
         ("9f8a7b6c5d4e3f2a1b0c1d2e3f4a5b6c", "regex"),
         ("john@acmecorp.com", "regex"),
         ("4111 1111 1111 1111", "regex")],
    ),
]


def _temp_engine(base: Settings, use_llm: bool) -> Engine:
    """A throwaway in-memory engine so verify never pollutes a real vault."""
    s = Settings()
    s.__dict__.update(base.__dict__)
    s.ephemeral = True
    s.engagement_id = "__verify__"
    if not use_llm:
        s.detectors = ["regex"]   # regex floor only
    return Engine(settings=s)


def run(settings: Settings | None = None, use_llm: bool = True) -> dict:
    settings = settings or Settings()
    engine = _temp_engine(settings, use_llm)
    contextual = engine.contextual_available()
    det_status = engine.detector_status()

    results = []
    total_leaks = 0
    total_needs_ctx = 0
    rt_failures = 0

    for fx in FIXTURES:
        anon = engine.anonymize(fx.text, is_tool_output=True, use_llm=contextual)
        leaks, needs_ctx = [], []
        for secret, layer in fx.secrets:
            if secret in anon:
                if layer == "llm" and not contextual:
                    needs_ctx.append(secret)
                else:
                    leaks.append(secret)
        restored = engine.deanonymize(anon)
        rt_ok = restored == fx.text
        if not rt_ok:
            rt_failures += 1
        total_leaks += len(leaks)
        total_needs_ctx += len(needs_ctx)
        results.append({"fixture": fx.name, "leaks": leaks,
                        "needs_llm": needs_ctx, "roundtrip_ok": rt_ok,
                        "anonymized": anon})

    probe = _adversarial_probe(engine, contextual)
    tool_probe = _tool_call_probe(engine)

    return {"contextual_active": contextual, "detectors": det_status,
            "results": results, "total_leaks": total_leaks,
            "needs_llm": total_needs_ctx, "roundtrip_failures": rt_failures,
            "adversarial": probe, "tool_call_probe": tool_probe,
            "mappings": engine.export()}


def _adversarial_probe(engine: Engine, contextual: bool) -> dict:
    """Simulate a prompt-injected model that echoes its whole context back
    verbatim ("repeat everything above").  Since only the anonymized payload
    ever left the machine, the echo can only contain surrogates — we assert no
    regex-layer secret is present in what was sent upstream.
    """
    blob = "\n".join(fx.text for fx in FIXTURES)
    outbound = engine.anonymize(blob, is_tool_output=True, use_llm=contextual)
    must_never_leak = [s for fx in FIXTURES for s, layer in fx.secrets
                       if layer == "regex"]
    leaked = sorted({s for s in must_never_leak if s in outbound})
    return {"leaked": leaked, "checked": len(set(must_never_leak))}


def _tool_call_probe(engine: Engine) -> dict:
    """Tool-call payloads must not leak real values either — a real engagement
    regression: a prior assistant turn's tool call (host/creds used in a
    command) gets echoed back in conversation history on the next request, and
    that content bypassed anonymization entirely until this check existed.
    """
    real_host = "10.10.10.5"

    anthropic_body = {"messages": [{"role": "assistant", "content": [
        {"type": "tool_use", "id": "t1", "name": "run_ssh",
         "input": {"host": real_host, "user": "admin"}},
    ]}]}
    anthropic_out = transform.anonymize_anthropic_request(engine, anthropic_body)
    anthropic_leak = real_host in json.dumps(anthropic_out)

    openai_body = {"messages": [{"role": "assistant", "content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {
            "name": "run_ssh", "arguments": json.dumps({"host": real_host, "user": "admin"})}},
    ]}]}
    openai_out = transform.anonymize_openai_request(engine, openai_body)
    openai_leak = real_host in json.dumps(openai_out)

    return {"anthropic_tool_use_leak": anthropic_leak, "openai_tool_call_leak": openai_leak}


def print_report(report: dict, show_mappings: bool = False) -> None:
    print("\nAnonproxy coverage verification")
    print("=" * 60)
    print("Detector chain:")
    for d in report["detectors"]:
        flag = "on " if d.get("available") else "off"
        print(f"  [{flag}] {d['name']:<14} {d.get('detail', '')}")
    if not report["contextual_active"]:
        print("  (regex floor only; bare hostnames / unlabelled creds need a "
              "contextual backend)")
    print("-" * 60)

    for r in report["results"]:
        if r["leaks"]:
            mark = "LEAK ✗"
        elif not r["roundtrip_ok"]:
            mark = "RT   ✗"
        elif r["needs_llm"]:
            mark = "ok ~ "
        else:
            mark = "ok ✓ "
        print(f"  [{mark}] {r['fixture']}")
        for s in r["leaks"]:
            print(f"          LEAKED: {s!r}")
        for s in r["needs_llm"]:
            print(f"          needs contextual backend (not caught regex-only): {s!r}")
        if not r["roundtrip_ok"]:
            print("          round-trip did NOT reproduce the original")

    adv = report["adversarial"]
    print("-" * 60)
    print(f"  adversarial 'repeat context verbatim' probe: "
          f"{len(adv['leaked'])} leak(s) of {adv['checked']} must-never-leak values")
    for s in adv["leaked"]:
        print(f"          OUTBOUND LEAK: {s!r}")

    tcp = report["tool_call_probe"]
    tool_leak = tcp["anthropic_tool_use_leak"] or tcp["openai_tool_call_leak"]
    print(f"  tool-call payload probe: {'LEAK ✗' if tool_leak else 'ok ✓'}")
    if tcp["anthropic_tool_use_leak"]:
        print("          Anthropic tool_use.input leaked a real value")
    if tcp["openai_tool_call_leak"]:
        print("          OpenAI tool_calls[].function.arguments leaked a real value")

    if show_mappings:
        print("-" * 60)
        print("  Anonymized fixture output (what the LLM would receive):")
        for r in report["results"]:
            print(f"\n  • {r['fixture']}")
            for line in r["anonymized"].splitlines():
                print(f"      {line}")
        print("\n  Mappings  (original  →  surrogate):")
        for m in report.get("mappings", []):
            print(f"      [{m['entity_type']:<13}] {m['original']!r}  →  {m['surrogate']!r}")

    print("-" * 60)
    print(f"  leaks: {report['total_leaks']}   "
          f"round-trip failures: {report['roundtrip_failures']}   "
          f"needs-contextual (regex-only): {report['needs_llm']}")
    hard_fail = report["total_leaks"] or report["roundtrip_failures"] or adv["leaked"] or tool_leak
    if not hard_fail:
        if report["needs_llm"] and not report["contextual_active"]:
            print("  RESULT: regex floor holds. Enable a contextual backend for "
                  "full coverage.")
        else:
            print("  RESULT: PASS — no leaks, round-trip intact.")
    else:
        print("  RESULT: FAIL — see leaks above.")
    print()
