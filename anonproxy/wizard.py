"""
Interactive local setup wizard.

Walks through engagement name, local Ollama detection (detect / pull a model),
listen port and optional API token, writes a ``.env`` you can reuse, and offers
to launch the proxy.  Everything stays on this machine — there is no remote or
VPS path.
"""
from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

# Current small local models good at structured JSON extraction (June 2026).
# Qwen3 has displaced Qwen2.5 in the Ollama library; qwen3:4b is a solid default
# that fits ~8 GB. Verify whichever you pick with `python -m anonproxy verify`.
SUGGESTED_MODELS = ["qwen3:4b", "qwen3:8b", "gemma3:4b", "llama3.2:3b"]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        ans = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        ans = ""
    return ans or default


def _yesno(prompt: str, default: bool = True) -> bool:
    d = "Y/n" if default else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default
    return ans.startswith("y")


def _write_scope_file(path: Path, seed_terms: list[str]) -> None:
    """Create a starter scope file (don't clobber an existing one)."""
    if path.exists():
        return
    header = (
        "# Anonproxy engagement scope — one term per line.\n"
        "# These client identifiers are ALWAYS anonymized (incl. bare hostnames).\n"
        "# Optional explicit type:  value=TYPE  (TYPE = HOSTNAME, DOMAIN, IP_ADDRESS,\n"
        "# CIDR, ORGANIZATION, EMAIL_ADDRESS). Type is inferred if omitted.\n"
        "# Lines starting with # and blank lines are ignored. Edit freely.\n"
        "#\n"
        "# Examples:\n"
        "#   acme.com\n"
        "#   portal.acme.com\n"
        "#   DC01=HOSTNAME\n"
        "#   10.20.0.0/16=CIDR\n"
        "#   Acme Corp=ORGANIZATION\n"
        "\n"
    )
    body = "\n".join(seed_terms)
    path.write_text(header + (body + "\n" if body else ""))


def _gliner2_installed() -> bool:
    import importlib.util
    return importlib.util.find_spec("gliner2") is not None


def _pip_install(extra: str) -> bool:
    """Best-effort install of an optional extra; never fatal."""
    import subprocess
    print(f"  Installing anonproxy[{extra}] (this can take a while)…")
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", f"anonproxy[{extra}]"],
                       check=True)
        return True
    except Exception as e:
        print(f"  install failed: {e}\n  run manually:  pip install 'anonproxy[{extra}]'")
        return False


def _ollama_tags(host: str) -> list[str] | None:
    if httpx is None:
        return None
    try:
        r = httpx.get(f"{host}/api/tags", timeout=3.0)
        r.raise_for_status()
        return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        return None


def _pull(host: str, model: str) -> bool:
    print(f"Pulling {model} (this can take a while)…")
    try:
        with httpx.stream("POST", f"{host}/api/pull",
                          json={"name": model}, timeout=None) as r:
            for line in r.iter_lines():
                if line:
                    # show coarse status without spamming
                    if '"status"' in line:
                        import json
                        st = json.loads(line).get("status", "")
                        sys.stdout.write(f"\r  {st[:70]:<70}")
                        sys.stdout.flush()
        print("\r  done" + " " * 70)
        return True
    except Exception as e:
        print(f"\n  pull failed: {e}")
        return False


def run() -> int:
    print("\n🛡️  Anonproxy — local setup\n" + "-" * 32)

    engagement = _ask("Engagement id (change per client)", "default")

    # Engagement scope seed — the reliable way to catch BARE hostnames/org names.
    print("\nScope seed (recommended): list the client's domains, hostnames and")
    print("org names so they're ALWAYS anonymized — even bare names like DC01 that")
    print("patterns can't infer. Comma-separated, e.g. acme.com,portal.acme.com,DC01,Acme Corp")
    scope = _ask("Scope terms (blank to skip)", "")
    # Always write a starter scope file the operator keeps editing during the
    # engagement; seed it with anything entered above.
    safe_eng = "".join(c if c.isalnum() or c in "-_." else "_" for c in engagement)
    scope_path = Path(f"{safe_eng}-scope.txt")
    _write_scope_file(scope_path, [t.strip() for t in scope.split(",") if t.strip()])
    print(f"Wrote starter scope file: {scope_path.resolve()}  (edit it any time)")

    ollama_host = "http://localhost:11434"
    model = SUGGESTED_MODELS[0]
    llm_enabled = False

    # detector chain: regex floor is always first.
    detectors = ["regex"]

    # Recommended contextual backend: GLiNER2-PII (best recall, CPU-friendly).
    print("\nContextual detection catches what patterns can't — bare hostnames,")
    print("names, and unlabelled credentials in prose.")
    print("Recommended: GLiNER2-PII (top of the SPY PII benchmark, runs on CPU,")
    print("no Ollama needed).")
    if _yesno("Enable GLiNER2-PII (recommended)?", True):
        detectors.append("gliner2")
        if _gliner2_installed():
            print("  GLiNER2 already installed.")
        elif _yesno("  Install it now? (pip install 'anonproxy[gliner2]')", True):
            _pip_install("gliner2")

    # Optional: also use a local Ollama model (good for prose / org-project names).
    # If Ollama is already running, recommend stacking it (best of both); otherwise
    # don't push it — gliner2 alone is a strong default.
    running_tags = _ollama_tags(ollama_host)
    ollama_running = running_tags is not None
    if ollama_running:
        prompt = ("\nOllama is running — also stack it for extra recall on prose / "
                  "org-project names? (recommended: regex,gliner2,ollama)")
    else:
        prompt = "\nAlso use a local Ollama model? (optional; Ollama not detected)"
    if _yesno(prompt, ollama_running):
        llm_enabled = True
        detectors.append("ollama")
        ollama_host = _ask("Ollama host", ollama_host)
        tags = running_tags if ollama_running else _ollama_tags(ollama_host)
        if tags is None:
            print("  ⚠ couldn't reach Ollama (install from https://ollama.com).")
        else:
            if tags:
                print("  Installed models: " + ", ".join(tags))
            model = _ask("Model to use", tags[0] if tags else SUGGESTED_MODELS[0])
            if model not in (tags or []) and _yesno(
                    f"  {model} isn't installed — pull it now?", True):
                if httpx is not None:
                    _pull(ollama_host, model)

    print("\nOther optional backends (advanced):")
    print("  openai-privacy-filter  pip install 'anonproxy[openai-pii]'")
    print("  piiranha               pip install 'anonproxy[piiranha]'")
    print("  gliner                 older GLiNER, pip install 'anonproxy[gliner]'")
    print("  anonymizer-slm         Eternis SLM via Ollama (models/anonymizer-slm.Modelfile)")
    extra = _ask("Add any? (comma-separated, blank for none)", "")
    for d in (x.strip() for x in extra.split(",")):
        if d and d not in detectors:
            detectors.append(d)

    port = _ask("Proxy port", "8080")
    use_token = _yesno("Protect the local API/audit with a token?", False)
    token = secrets.token_urlsafe(18) if use_token else ""

    env_path = Path(".env")
    lines = [
        f"ENGAGEMENT_ID={engagement}",
        f"ANONPROXY_SCOPE_FILE={scope_path}",
        f"ANONPROXY_DETECTORS={','.join(detectors)}",
        f"LLM_ENABLED={'true' if llm_enabled else 'false'}",
        f"OLLAMA_HOST={ollama_host}",
        f"OLLAMA_MODEL={model}",
        "HOST=127.0.0.1",
        f"PORT={port}",
        f"ANONPROXY_API_TOKEN={token}",
        "ANONPROXY_AUDIT=true",
    ]
    env_path.write_text("\n".join(lines) + "\n")
    print(f"\nWrote {env_path.resolve()}")

    base = f"http://127.0.0.1:{port}"
    print("\nNext steps")
    print("-" * 32)
    print(f"  Claude Code:   export ANTHROPIC_BASE_URL={base}")
    print(f"  OpenAI SDK:    base_url = \"{base}/v1\"")
    print(f"  Audit page:    {base}/audit" + (f"?token={token}" if token else ""))
    print()

    if _yesno("Launch the proxy now?", True):
        for line in lines:
            k, _, v = line.partition("=")
            os.environ[k] = v
        from .config import Settings
        from .cli import main as cli_main
        # re-resolve settings from the env we just set
        return cli_main(["serve"])
    print("Start later with:  python -m anonproxy serve")
    return 0
