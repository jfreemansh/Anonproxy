# Anonproxy

[![CI](https://github.com/jfreemansh/Anonproxy/actions/workflows/ci.yml/badge.svg)](https://github.com/jfreemansh/Anonproxy/actions/workflows/ci.yml)

**A reversible anonymization layer for sending pentest data to LLMs — built to be
more reliable than match/replace.**

Anonproxy sits between your tools (Claude Code, the OpenAI SDK, Burp Suite) and
the LLM API. It strips IPs, hostnames, credentials, hashes, tokens, org names and
PII out of everything *before* it leaves the machine, and restores the real
values in the reply. The model reasons over realistic surrogates; you read real
data.

It is a ground-up reimplementation inspired by
[`DontFeedTheAI`](https://github.com/zeroc00I/DontFeedTheAI), focused on the part
that was only working ~75% of the time: **the round trip**.

> **In plain terms:** you run a small program on your laptop. Your AI tools talk
> to it instead of talking to the cloud directly. It swaps client data for
> realistic fakes before anything leaves your machine, and swaps the real values
> back into the answer. The cloud AI never sees real client data; you never see
> the fakes.
>
> **New here? Read [QUICKSTART.md](QUICKSTART.md) first** — three steps, no jargon.

---

## Why the old approach hit ~75%

Match/replace (and the original's exact-substring restore) breaks the moment the
model touches a surrogate. Given a surrogate `host-ab12cd9`, models routinely
write it back as:

| What the model writes | Exact `str.replace` restores? |
|---|:---:|
| `host-ab12cd9` | ✅ |
| `**host-ab12cd9**` (bold) | ✅ (wrapper is outside) |
| `` `host-ab12cd9` `` (inline code) | ✅ |
| `HOST-AB12CD9` (case changed) | ❌ |
| `` `host-`ab12cd9 `` (emphasis *inside* the token) | ❌ |
| `host‑ab12cd9` (non-breaking hyphen / line wrap) | ❌ |
| surrogate split across two streaming chunks | ❌ |

Add detection gaps (regex can't see a bare hostname) and a small local model's
inconsistency, and you land around three-quarters. Our reproducible benchmark
puts naive exact-replace at **78%** and Anonproxy's restorer at **100%** on the
same mangling patterns:

```
$ python3 scripts/benchmark_roundtrip.py

mangling         naive str.replace   tolerant restorer
------------------------------------------------------
verbatim      7/    7  (  100%)       7/7  (  100%)
uppercase     2/    7  (   29%)       7/7  (  100%)
lowercase     6/    7  (   86%)       7/7  (  100%)
intra_code    0/    7  (    0%)       7/7  (  100%)
bold_segment  3/    7  (   43%)       7/7  (  100%)
...
OVERALL       60/   77  (   78%)      77/77  (  100%)
```

## What makes it more reliable

**1. Tolerant restoration.** Restoration matches a surrogate against a normalized
projection of the reply (markdown noise *inside* the token removed, whitespace
collapsed, case folded, unicode hyphens normalized) while keeping an index map
back to the original text, so it recovers the value even when the model
reformats it. It deliberately does **not** expand outward to eat markdown
*around* a matched span (e.g. `**`/backticks hugging it) — those may be genuine
original content rather than model-added formatting, and swallowing them
corrupted real pages that happened to use `*emphasis*` near a redacted value.
(`anonproxy/restorer.py`)

**2. Streaming-safe.** A per-content-block hold-back buffer reassembles a
surrogate split across SSE deltas before restoring it, so you get real values
*as the response streams*, not after a full buffer. (`anonproxy/proxy/streaming.py`)

**3. Consistency by construction.** Surrogates are deterministic (HMAC keyed on
the engagement id) *and* vault-backed, so the same original always maps to the
same surrogate on an exact re-sighting — even across restarts or a lost vault.
A consistency rescan re-detects anything the vault has ever seen, so an entity
caught once is caught every time. (Two *different* casings of the same
real-world value, e.g. `WordPress.org` and `wordpress.org` both appearing in
one page, are treated as distinct sightings with their own surrogates — the
alternative, collapsing them, can only remember one original spelling and gets
the other one's restoration wrong.) (`anonproxy/surrogates.py`, `anonproxy/vault.py`)

**4. Format-preserving surrogates.** A hash surrogate is hex of the same length;
an AWS key keeps its `AKIA` prefix; an IP is a valid RFC 5737 TEST-NET address; a
payment card stays 16 Luhn-valid digits with the same grouping. The model treats
them as the real thing and has no reason to "correct" them — which also *prevents*
mangling in the first place. Precise floor spans also win over a broader model
span that merely wraps them, so structure like a `PHPSESSID=` cookie name is kept
while the value is swapped.

**5. Layered, pluggable detection.** A deterministic regex floor (IPs, hashes,
JWTs, cloud keys, FQDNs, MACs) plus a *configurable chain* of contextual backends
and the consistency rescan. Contextual backends are additive: if one is down,
regex + rescan carry on. See [Detection backends](#detection-backends).
(`anonproxy/detectors/`)

## Architecture

```
            real data                      surrogates only
 client  ───────────────▶  Anonproxy  ───────────────────▶  LLM API
 (Claude Code /            (engine +                         (Anthropic /
  OpenAI SDK / Burp)        proxy)    ◀───────────────────   OpenAI-compatible)
                              ▲          response w/ surrogates
                              │ restored (tolerant, streaming)
                              ▼
                    per-engagement vault (SQLite, isolated)
```

One engine, three front ends:

* **Library** — `from anonproxy import Engine`
* **Proxy** — `python -m anonproxy serve` (Anthropic + OpenAI shapes)
* **Burp extension** — `burp/` delegates to the engine's local API, so Burp
  shares the same vault and tolerant restorer (see `burp/README.md`)

## Quick start (all local)

Everything runs on your machine — no VPS, no remote, nothing sensitive leaves
the host. Pick whichever setup you prefer.

**A. macOS app (recommended)**

> **You do NOT need to clone this repo or run any pip command for this path.**
> The app ships the entire Anonproxy engine inside itself — this download is
> the whole product.

*1 · Install* — from the project's [Releases](../../releases) page grab the
zip for your Mac: **`Anonbar-macos-arm64.zip`** (Apple Silicon M1–M4) or
**`Anonbar-macos-intel.zip`** (Intel Macs). Unzip, drag **Anonbar.app** to
Applications, open it (unsigned build: right-click → *Open* the first time,
or `xattr -cr /Applications/Anonbar.app`). A 🛡️ icon appears in your menu bar.

> **The app is macOS-only.** It does not run on Windows or Linux — those
> platforms use the terminal CLI instead (options **B**/**C** below).

*2 · Requirements* — none. The app embeds its own Python interpreter and
every dependency (v0.1.2+): no Homebrew, no pip, no network on first run,
works even on machines without any Python installed.

*3 · Daily loop* —

```
🛡️ menu ▸ Engagements ▸ ＋ New engagement…      name it, add scope terms
🛡️ menu ▸ Start
🛡️ menu ▸ Copy client env                        paste into your shell:
    export ANTHROPIC_BASE_URL=http://127.0.0.1:8099   # then run claude / your SDK
🛡️ menu ▸ Open audit dashboard                   watch every redaction live
🛡️ menu ▸ Export & archive vault…                at close-out: evidence CSV+JSON,
                                                 vault wiped
```

Switching clients = pick another engagement from the same menu (vaults stay
isolated per client).

*4 · Recommended extra* — install [Ollama](https://ollama.com) and pull a
model (`ollama pull qwen3:4b`); detection then also catches bare hostnames,
org names and unlabeled passwords, not just regex-shaped secrets. Without it
everything still works — regex floor only.

*5 · Optional* — want the terminal CLI too (`anonproxy …`, `scripts/anon`
profiles)? That's what options **B**/**C** below are for; completely separate
from the app, sharing the same engagements.

Building the app yourself instead of downloading?
[Fast workflow](#fast-workflow-profiles-scripts_anon-macos-menubar).

**B. Guided wizard**

```bash
# one-time install (Python 3.10+): gives you the `anonproxy` command
pipx install anonproxy        # or: pip install anonproxy  /  pip install .
anonproxy wizard
```

From source instead?

```bash
git clone https://github.com/jfreemansh/Anonproxy && cd Anonproxy
pip install .                 # or: pip install -r requirements.txt + python -m anonproxy
python -m anonproxy wizard
```

It asks for the engagement name, finds/pulls an Ollama model, writes a `.env`,
and offers to launch the proxy.

**C. Manual**

```bash
pip install -r requirements.txt
ollama pull qwen3:4b                         # optional, for best recall
python -m anonproxy serve --engagement acme-2026
```

**D. Docker (bundles Ollama)**

```bash
ENGAGEMENT_ID=acme-2026 docker compose up -d
docker compose exec ollama ollama pull qwen3:4b     # one-time
```

Then point your client at it:

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8099
claude

# OpenAI SDK / OpenRouter
#   base_url = "http://127.0.0.1:8099/v1"
```

### Fast workflow: profiles (`scripts/anon`, macOS menubar)

> **The proxy listens on 8099, not 8080** — Burp's upstream proxy owns 8080,
> and this tool ships a Burp extension that talks to a *running* Anonproxy.
> Keeping the two apart means both are up at once with zero configuration.

One JSON profile per client/test carries the whole context — engagement id
(→ vault isolation), scope seed, detector chain, model, port. Spin-up between
tests is one command instead of re-typed flags:

```bash
scripts/anon profile new acme-web --scope "acme.com,portal.acme.com,DC01" --notes "web app test"
scripts/anon up acme-web --daemon      # detached; logs under ~/.anonproxy/logs/
scripts/anon env acme-web --copy       # client export lines → clipboard
scripts/anon stop                      # reap the daemon
scripts/anon close acme-web            # export JSON+CSV evidence + wipe vault
```

`profile list|show|edit|rm` round it out (profiles live in
`~/.anonproxy/profiles/`; `ANONPROXY_PROFILE_DIR` overrides). `up <profile>`
without `--daemon` behaves like `serve` with the profile applied.

On macOS there's also a **native status-bar app** — pick an engagement,
Start/Stop, ＋ **New engagement…** (a real form: name, scope terms, port,
notes, ephemeral toggle), copy client env, open `/audit`, run `verify`, and
one-click **Export & archive vault…** close-out. Prebuilt `Anonbar-macos-arm64.zip`
and `Anonbar-macos-intel.zip` (Python runtime and dependencies *embedded* — the
machine needs nothing installed) live on the
[Releases](../../releases) page; see [Quick start A](#a-macos-app-recommended).

Build it yourself instead:

```bash
scripts/install_anonbar.sh      # builds + installs /Applications/Anonbar.app
open -a Anonbar                 # or Spotlight: ⌘Space → "Anonbar"
```

Rebuild/reinstall when `anonbar.swift` changes. Auto-start at login:
System Settings → General → Login Items → add Anonbar.

It shells out to the same CLI, so profiles created in either place just show
up in the other. Env overrides: `ANONPROXY_HOME`, `PYTHON_BIN`.

> Ephemeral profiles (`--ephemeral`) leave nothing on disk — but that also
> means a close-out *after* stopping finds no mappings. Run close-out while
> the proxy still holds them, or keep persistence on when evidence matters.

Quick offline check without a client:

```bash
echo 'Host dc01.acmecorp.local at 10.20.0.10, NTLM 8846f7eaee8fb117ad06bdd830b7586c' \
  | python -m anonproxy anon --engagement acme-2026
```

## End-to-end example (no Burp)

Send real tool output to a real LLM through the proxy and get a useful answer
back — while the API only ever sees fake data.

```bash
# 1. start the proxy
python -m anonproxy serve --engagement acme-2026 &

# 2. run a tool, then ask Claude about it THROUGH the proxy
SCAN=$(nmap -sV dc01.acmecorp.local)

curl -s http://127.0.0.1:8099/v1/messages \
  -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "content-type: application/json" \
  -d "$(jq -n --arg scan "$SCAN" '{
        model: "claude-sonnet-4-6",
        max_tokens: 1024,
        messages: [{role:"user", content:("Analyse this scan and suggest next steps:\n" + $scan)}]
      }')"
```

What just happened:

- **Anthropic only saw surrogates** — e.g. `203.0.113.47` and
  `host-ab12cd9.pentest.local`, never `10.20.0.10` or `dc01.acmecorp.local`.
- **Your reply has the real values back** — even if the model wrote them in bold
  or changed their case.
- Open `http://127.0.0.1:8099/audit` to see exactly what was swapped.

The same works for OpenAI-style clients — just call
`http://127.0.0.1:8099/v1/chat/completions` with your usual OpenAI payload.
And for **Google Gemini** (REST shape is handled natively, including
`countTokens`; auth headers/query keys pass through untouched):

```bash
curl -s "http://127.0.0.1:8099/v1beta/models/gemini-2.5-flash:generateContent?key=$GEMINI_API_KEY" \
  -H 'content-type: application/json' \
  -d '{"contents":[{"role":"user","parts":[{"text":"Analyse this scan:\n'"$SCAN"'"}]}]}'
```

Upstream defaults to `generativelanguage.googleapis.com` (override with
`GOOGLE_UPSTREAM`).

## MCP servers (`anonproxy mcp`)

Wrap **any stdio MCP server** so its tool *results* are anonymized before
they enter model context — host→server traffic passes through untouched:

```jsonc
// .mcp.json — point the host at the wrapper instead of the server binary
{ "mcpServers": { "nmap": {
    "command": "anonproxy",
    "args": ["mcp", "--", "nmap-mcp-server", "--some-flag"] } } }
```

Server→host messages have every detected entity replaced inside
`result.content[].text`, resource `contents[].text`, and legacy string
`content`; ids/methods/params stay verbatim. Restoration back to real values
uses the same engagement vault as everything else.

## Audit dashboard

Open `http://127.0.0.1:8099/audit` (or `python -m anonproxy audit`) to review
every `original → surrogate` mapping live during an engagement — filterable by
type, with counts and CSV export. It binds to localhost and honours
`ANONPROXY_API_TOKEN` if set; disable it with `ANONPROXY_AUDIT=false`. It exposes
the reverse lookup, so treat it as an operator-only debug view.

If a detector backend errors mid-engagement (a floor-detector crash, a contextual
backend throwing), the stats bar shows a red `⚠ <name> failed ×N` pill instead of
failing silently into a log line — check it if coverage looks off partway through
a session.

## Engagement workflow

1. **One engagement id per client** (`--engagement acme-2026`). This isolates the
   vault so surrogates never cross between clients.
2. Run the proxy locally on your testing machine. Everything — proxy, vault, and
   the Ollama detector — stays on the host.
3. Work normally — every request is anonymized, every reply restored. Watch
   coverage live at `/audit`.
4. At session close, `python -m anonproxy export --engagement acme-2026` (or the
   audit page's CSV export) dumps the full `original → surrogate` map for your
   evidence/audit trail, then archive or delete the vault. Use
   `ANONPROXY_EPHEMERAL=1` for in-memory-only (no disk persistence), or set
   `ANONPROXY_VAULT_PASSPHRASE` to keep an AES-GCM-encrypted vault at rest
   instead of plaintext sqlite — see [Encrypted vaults](#encrypted-vaults-at-rest).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ENGAGEMENT_ID` | `default` | **Change per client.** Isolates the vault. |
| `ANONPROXY_SCOPE` | *(empty)* | Comma list of client domains/hostnames/orgs always anonymized. |
| `ANONPROXY_SCOPE_FILE` | *(empty)* | File of scope terms (one per line, optional `value=TYPE`). |
| `ANONPROXY_CONTEXTUAL_MIN_LEN` | `4` | Contextual single-token findings shorter than this are dropped as noise (`db`, `sql`). Scope terms and regex matches bypass it. |
| `ANONPROXY_DETECTORS` | `regex,ollama` | Backend chain (see [Detection backends](#detection-backends)). |
| `LLM_ENABLED` | `true` | `false` = drop the Ollama backend. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint. |
| `OLLAMA_MODEL` | `qwen3:4b` | Local detection model. Any Ollama model works (`--model`). |
| `GLINER_MODEL` | `urchade/gliner_multi_pii-v1` | GLiNER model (if enabled). |
| `PIIRANHA_MODEL` | `iiiorg/piiranha-v1-detect-personal-information` | Piiranha model (if enabled). |
| `ANONYMIZER_SLM_MODEL` | `anonymizer-slm` | Ollama name of the imported Anonymizer SLM. |
| `ANONPROXY_TOLERANT` | `true` | Tolerant restoration (vs. exact). |
| `ANONPROXY_EPHEMERAL` | `false` | In-memory vault, nothing on disk. |
| `ANONPROXY_VAULT_PASSPHRASE` / `_KEYFILE` | *(empty)* | Encrypt the vault at rest (AES-GCM envelope). Either var enables it. Wrong passphrase fails cleanly; existing plaintext vaults are adopted (encrypted) on first write. See [Encrypted vaults](#encrypted-vaults-at-rest). |
| `ANONPROXY_AUDIT` | `true` | Serve the `/audit` dashboard. |
| `ANONPROXY_PROFILE_DIR` | `~/.anonproxy/profiles` | Where engagement profiles (one JSON each) live. |
| `ANONPROXY_EXPORTS_DIR` | `~/.anonproxy/exports` | Close-out evidence output root. |
| `PORT` / `HOST` | `8099` / `127.0.0.1` | Proxy listen address. |
| `ANTHROPIC_UPSTREAM` | `https://api.anthropic.com` | Anthropic upstream. |
| `OPENAI_UPSTREAM` | `https://api.openai.com` | Any OpenAI-compatible endpoint — OpenRouter, Groq, Together, etc. Paste the provider's documented `base_url` as-is, with or without a trailing `/v1`; both work (`https://openrouter.ai/api/v1` and `https://openrouter.ai/api` are equivalent here). |
| `ANONPROXY_API_TOKEN` | *(empty)* | Require `X-Anonproxy-Token` on the engine API. **Empty means the engine API and `/audit` are unauthenticated** — fine on an isolated laptop bound to `127.0.0.1`, but set this if the proxy is ever reachable by anything else (a shared box, a tunneled VPS). |
| `ANONPROXY_STRICT` | `false` | Fail closed (502) instead of forwarding a request unredacted when its body can't be parsed as JSON. Off by default so odd/legacy clients keep working. |

## Detection backends

Detection is a configurable chain set by `ANONPROXY_DETECTORS` (the wizard asks
too). The regex floor is always on and always first; everything else is
optional, lazily loaded, and skipped with a warning if its dependency or model
isn't present — so the default never breaks. Pick the trade-off your colleague
wants:

| Backend | Catches | Cost / setup |
|---|---|---|
| `regex` *(always on)* | IPs, CIDRs, hashes, JWTs, cloud keys, MACs, FQDNs, emails, labelled creds, payment cards (Luhn), Bearer/Basic auth, SSNs, and **any cookie value in a `Set-Cookie:`/`Cookie:` header — by structure, not by a fixed name list**, so a CMS's own custom session-cookie name (WordPress's `wordpress_logged_in_<hash>`, say) is caught the same as `PHPSESSID`. Also resilient to URL-encoded delimiters (`%7C` etc.) that would otherwise glue an alnum byte onto the next token and defeat a boundary-anchored match — a real, previously-silent gap on form-urlencoded bodies and cookie headers. | none — deterministic floor |
| `ollama` *(default)* | bare hostnames, org/project/person names, unlabelled creds in prose | local Ollama + a model (`--model`) |
| `gliner2` ⭐ *(recommended for general PII)* | 42 PII types, best span-level F1 on SPY (beats the others), <100ms — but its fixed taxonomy has **no hostname or organization label**, so it does not replace `ollama` for pentest-specific redaction | `pip install "anonproxy[gliner2]"`, CPU-friendly |
| `openai-privacy-filter` | ~96–97% F1 PII (Apache-2.0, OpenAI) | `pip install "anonproxy[openai-pii]"` (torch) |
| `gliner` | zero-shot person/org/username/hostname/email — the older urchade model, but because it's zero-shot you can *ask* it for `hostname`/`organization`, which `gliner2` can't be asked for | `pip install "anonproxy[gliner]"`, CPU-friendly |
| `piiranha` | high-accuracy passwords/emails/usernames (6 languages) | `pip install "anonproxy[piiranha]"` (torch) |
| `anonymizer-slm` | purpose-built PII detect+replace (Eternis Qwen3 fine-tune) | import GGUF via `models/anonymizer-slm.Modelfile` |

All four transformer backends (`gliner`, `gliner2`, `piiranha`, `openai-privacy-filter`)
chunk large inputs internally — a transformer has a fixed context window, and
handing one a whole multi-hundred-KB HTTP response either truncates coverage
silently or exhausts memory. Chunking bounds memory while still scanning the
full input.

> **Best recall for general PII:** `gliner2` (Fastino's GLiNER2-PII, May 2026)
> currently tops the SPY PII benchmark, runs in-process (no external service to
> keep alive), and surfaces failures cleanly instead of swallowing them.
> **It does not catch bare hostnames or client/org names** — that's the #1
> redaction need on a pentest engagement, and nothing in its 42-label PII
> taxonomy covers it. Don't drop `ollama` (or `gliner`) in favor of `gliner2`
> alone; stack them instead: **`ANONPROXY_DETECTORS=regex,gliner2,ollama`**.
> (When pulling `openai/privacy-filter`, use exactly that org — typosquats have
> appeared.)
>
> **Web-app testing:** the regex floor now covers payment cards, session cookies,
> Bearer/Basic auth and SSNs, so a lot of HTTP traffic is handled deterministically.
> But **names and addresses in request/response bodies are not regex-detectable** —
> for those add a PII model. Always confirm on real traffic with
> `python -m anonproxy verify`.

```bash
# default, out of the box
ANONPROXY_DETECTORS=regex,ollama python -m anonproxy serve

# recommended: fast general-PII coverage (gliner2) + hostnames/orgs/names (ollama)
ANONPROXY_DETECTORS=regex,gliner2,ollama python -m anonproxy serve

# no external service at all — zero-shot gliner covers hostname/org itself
ANONPROXY_DETECTORS=regex,gliner,gliner2 python -m anonproxy serve

# stack several — order is just declaration order; regex always wins for
# structured types so a hash stays a hash
ANONPROXY_DETECTORS=regex,piiranha,ollama python -m anonproxy serve
```

The regex floor always wins type classification for structured data (so a hash
is never mis-typed as an org name), contextual backends add recall, and the
consistency rescan re-catches anything seen once. `python -m anonproxy verify`
prints the active chain and confirms coverage.

### Hostnames & the scope seed

The regex floor catches **fully-qualified** hostnames (`dc01.acme.local`,
`shop.acme.com`, `portal.acme.dev`) via a broad TLD list. It deliberately does
**not** guess **bare** hostnames (`DC01`, `WEB-PRD-03`) — there's no safe pattern
that separates them from ordinary words.

Two ways to cover bare names:

1. **Scope seed (recommended, deterministic).** Tell Anonproxy your engagement
   scope and every occurrence is anonymized, no model needed:

   ```bash
   # inline
   python -m anonproxy serve --engagement acme-2026 \
     --scope "acme.com,portal.acme.com,DC01,WEB-PRD-03,Acme Corp"
   # or a file (one term per line, optional value=TYPE)
   python -m anonproxy serve --scope-file acme-2026-scope.txt
   ```

   The wizard writes a starter `<engagement>-scope.txt` (with examples) for you
   and wires it into `.env` — just keep editing it as scope grows. The `--scope` /
   `--scope-file` flags work on `serve`, `verify`, and `anon` too; or set
   `ANONPROXY_SCOPE` / `ANONPROXY_SCOPE_FILE`.

   Scope terms run as part of the floor (always on, even regex-only), match whole
   tokens only (so `acme` won't touch `acmespeak`), and get the usual consistent,
   reversible surrogates.

2. **A contextual backend** (`ollama` or `gliner` — not `gliner2`, which doesn't
   request hostname/org labels) infers hostnames/org names it wasn't told about
   — good for catching scope you forgot to list.

> **Very short bare names** (`db`, `sql`, `dc1`): a contextual single token
> under 4 characters is dropped as probable noise, not a hostname. Put short
> names on the scope list (deterministic, always caught), or lower
> `ANONPROXY_CONTEXTUAL_MIN_LEN`.

Use both: seed what you know, let the model catch the rest.

### Encrypted vaults at rest

By default the engagement vault is a small SQLite file under
`~/.anonproxy/vaults/` containing the `original → surrogate` pairs — i.e.
your real client data. To keep that file encrypted on disk:

```bash
export ANONPROXY_VAULT_PASSPHRASE="pick something long"   # or _KEYFILE=/path/to/key
python -m anonproxy serve --engagement acme-2026
```

**Recommended one-time setup** — generate a random machine key and every
engagement (CLI *and* menubar app) is encrypted automatically from then on,
with nothing to type per session:

```bash
mkdir -p ~/.anonproxy && openssl rand -base64 32 > ~/.anonproxy/vault.key && chmod 600 ~/.anonproxy/vault.key
```

What you get: the vault becomes a single AES-GCM-encrypted envelope
(`<engagement>.sqlite.enc`); while running, the plaintext exists only in the
process memory and a private temp file that is deleted on exit. Re-opening
with the same passphrase restores everything automatically; a wrong passphrase
— or a lost keyfile — fails immediately with a clear error instead of
corrupting or silently recreating the vault. An existing
plaintext vault is adopted (encrypted) the next time something is written,
and `close-out` removes the envelope along with everything else. The `/audit`
stats bar shows a 🔒 pill whenever the live vault is encrypted.

## Verify coverage before an engagement

```bash
python -m anonproxy verify                  # uses your local Ollama if it's running
python -m anonproxy verify --no-llm          # regex floor only
python -m anonproxy verify --show-mappings   # also print anonymized output + original→surrogate table
ANONPROXY_DETECTORS=regex,gliner2 python -m anonproxy verify   # test a specific chain
```

`--show-mappings` (alias `--audit`) is the audit view for a verify run — it shows
exactly what the LLM would receive and every swap it made. (The `/audit` web
dashboard is for *live proxy* traffic; verify runs in a throwaway vault so it
won't clutter a real engagement.)

Runs realistic nmap / secretsdump / netexec / config / HTTP outputs through the
full pipeline and reports any **leaks** (sensitive strings that survived) and
**round-trip** failures. It prints the active detector chain, and whichever
contextual backends are up are used automatically — so this also confirms your
local model works. Secrets only a contextual backend can catch (bare hostnames,
unlabelled creds) are shown as *needs-contextual* in regex-only mode rather than
counted as leaks. It also runs an **adversarial "repeat the context verbatim"
probe** that asserts no regex-layer secret could appear in what was sent
upstream, and a **tool-call payload probe** that checks an Anthropic `tool_use`
block / OpenAI `tool_calls[].function.arguments` echoed back in conversation
history doesn't leak a real value either — that path bypassed anonymization
entirely until this check existed. Exit code is non-zero if anything real leaks.

Check what's actually active any time:

```bash
curl -s http://127.0.0.1:8099/anonproxy/health | python -m json.tool
# -> detectors[]: name, available, model/effective_model, detail
```

If a configured Ollama model isn't pulled, the detector auto-falls back to an
installed one and says so in `health` and `verify` (so "Ollama is running but no
model" can't silently degrade you to regex-only). If Ollama dies or times out
mid-engagement, the next request re-checks reachability instead of trusting a
stale "available" — `health` will flip to `available: false` with the reason,
rather than staying green while quietly returning zero detections.

## Tests

```bash
python3 -m pytest -q                    # 134 tests: round-trip, streaming, proxy, audit, verify, detectors, tool-calls, vault, profiles, llm_detector, webapp, scope, config, polish
python3 scripts/benchmark_roundtrip.py  # naive vs tolerant pass-rate table
```

## Techniques & prior art

Anonproxy implements the reversible-anonymization techniques that fit a pentest
LLM pipeline, and deliberately skips the ones that don't:

| Technique | In Anonproxy |
|---|---|
| Format-preserving masking | ✅ surrogates are valid instances of their type |
| Pseudonymization via consistent keyed-hash substitution | ✅ HMAC keyed on the engagement id + vault |
| Tolerant, streaming-safe restoration | ✅ the core improvement over match/replace |
| Generalization (ranges) / nulling (redaction) | ➖ not applicable to pentest infra data |
| Synthetic data generation | ➖ surrogates are synthetic but kept structure-faithful |

**One method to keep in mind — linkability.** Consistent surrogates (the same
input always maps to the same fake) make the AI's reasoning coherent across a
session, but that same consistency means a provider could, in principle,
*correlate* requests over time even without the real values (see RAT-Bench and
"localized adversarial anonymization", 2026). For pentest work the consistency is
usually worth it (you want the model to track "the same host" across turns). If
you need stronger anonymity against correlation, rotate the engagement id per
session — reversibility still works (it's vault-backed), only the surrogate
values change.

Inspired by [DontFeedTheAI](https://github.com/zeroc00I/DontFeedTheAI) and informed by:

- Fastino, [*GLiNER2-PII*](https://huggingface.co/fastino/gliner2-privacy-filter-PII-multi) — current SOTA PII span extraction; the recommended `gliner2` backend.
- OpenAI, [*Privacy Filter*](https://huggingface.co/openai/privacy-filter) — open-weight local PII classifier; the `openai-privacy-filter` backend.
- Eternis, [*Anonymizer SLM series*](https://huggingface.co/blog/pratyushrt/anonymizerslm) — purpose-built Qwen3 fine-tune for surgical PII detect+replace; the optional `anonymizer-slm` backend.
- Red-Gate Simple Talk, [*How to anonymize PII in LLM pipelines*](https://www.red-gate.com/simple-talk/data-security-privacy-compliance/how-to-anonymize-pii-in-llm-pipelines-5-key-techniques-explained/) — the five-technique taxonomy above and the adversarial "repeat the context" leak test now in `verify`.
- Earlier/optional models: [GLiNER](https://huggingface.co/urchade/gliner_multi_pii-v1), [Piiranha](https://huggingface.co/iiiorg/piiranha-v1-detect-personal-information).

## Scope & limits

This is a **risk-reduction layer, not a privacy guarantee** (same honest framing
as the original). It does not defend against query-pattern correlation, prompt
injection in tool output, or compromise of the local host. It only anonymizes
text — pasted screenshots or other non-text content bypass it entirely. It
assumes a single proxy process; the vault's in-memory cache has no cross-process
invalidation, so don't run it behind multiple workers (`serve` always starts
one). It is not a substitute for reading what your NDA and engagement contract
allow before using any cloud AI on client data. Verify coverage per engagement
with the `/audit` page, `export`, and the test suite. A fuller accounting of
assets, trust boundaries and residual risks lives in [THREAT-MODEL.md](THREAT-MODEL.md);
security issues go to [SECURITY.md](SECURITY.md).

## Releases & CI

Pushing a tag (`vX.Y.Z`) triggers two GitHub Actions workflows:
`Anonbar-macos-arm64.zip` + `Anonbar-macos-intel.zip` (self-contained apps with the
Python runtime embedded) are attached to the release, and matching sdist +
wheel are published to [PyPI](https://pypi.org/project/anonproxy/) via
trusted publishing.

## License

MIT.
