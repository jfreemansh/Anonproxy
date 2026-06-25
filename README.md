# Anonproxy

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
projection of the reply (markdown noise removed, whitespace collapsed, case
folded, unicode hyphens normalized) while keeping an index map back to the
original text, so it recovers the value even when the model reformats it — and
swallows the surrounding `**` / backticks cleanly. (`anonproxy/restorer.py`)

**2. Streaming-safe.** A per-content-block hold-back buffer reassembles a
surrogate split across SSE deltas before restoring it, so you get real values
*as the response streams*, not after a full buffer. (`anonproxy/proxy/streaming.py`)

**3. Consistency by construction.** Surrogates are deterministic (HMAC keyed on
the engagement id) *and* vault-backed, so the same original always maps to the
same surrogate — even across restarts or a lost vault. A consistency rescan
re-detects anything the vault has ever seen, so an entity caught once is caught
every time. (`anonproxy/surrogates.py`, `anonproxy/engine.py`)

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

**A. Guided wizard (easiest)**

```bash
pip install -r requirements.txt
python -m anonproxy wizard
```

It asks for the engagement name, finds/pulls an Ollama model, writes a `.env`,
and offers to launch the proxy.

**B. Manual**

```bash
pip install -r requirements.txt
ollama pull qwen3:4b                         # optional, for best recall
python -m anonproxy serve --engagement acme-2026
```

**C. Docker (bundles Ollama)**

```bash
ENGAGEMENT_ID=acme-2026 docker compose up -d
docker compose exec ollama ollama pull qwen3:4b     # one-time
```

Then point your client at it:

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
claude

# OpenAI SDK / OpenRouter
#   base_url = "http://127.0.0.1:8080/v1"
```

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

curl -s http://127.0.0.1:8080/v1/messages \
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
- Open `http://127.0.0.1:8080/audit` to see exactly what was swapped.

The same works for OpenAI-style clients — just call
`http://127.0.0.1:8080/v1/chat/completions` with your usual OpenAI payload.

## Audit dashboard

Open `http://127.0.0.1:8080/audit` (or `python -m anonproxy audit`) to review
every `original → surrogate` mapping live during an engagement — filterable by
type, with counts and CSV export. It binds to localhost and honours
`ANONPROXY_API_TOKEN` if set; disable it with `ANONPROXY_AUDIT=false`. It exposes
the reverse lookup, so treat it as an operator-only debug view.

## Engagement workflow (Nethemba)

1. **One engagement id per client** (`--engagement acme-2026`). This isolates the
   vault so surrogates never cross between clients.
2. Run the proxy locally on your testing machine. Everything — proxy, vault, and
   the Ollama detector — stays on the host.
3. Work normally — every request is anonymized, every reply restored. Watch
   coverage live at `/audit`.
4. At session close, `python -m anonproxy export --engagement acme-2026` (or the
   audit page's CSV export) dumps the full `original → surrogate` map for your
   evidence/audit trail, then archive or delete the vault. Use
   `ANONPROXY_EPHEMERAL=1` for in-memory-only (no disk persistence).

## Configuration

| Variable | Default | Description |
|---|---|---|
| `ENGAGEMENT_ID` | `default` | **Change per client.** Isolates the vault. |
| `ANONPROXY_SCOPE` | *(empty)* | Comma list of client domains/hostnames/orgs always anonymized. |
| `ANONPROXY_SCOPE_FILE` | *(empty)* | File of scope terms (one per line, optional `value=TYPE`). |
| `ANONPROXY_DETECTORS` | `regex,ollama` | Backend chain (see [Detection backends](#detection-backends)). |
| `LLM_ENABLED` | `true` | `false` = drop the Ollama backend. |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama endpoint. |
| `OLLAMA_MODEL` | `qwen3:4b` | Local detection model. Any Ollama model works (`--model`). |
| `GLINER_MODEL` | `urchade/gliner_multi_pii-v1` | GLiNER model (if enabled). |
| `PIIRANHA_MODEL` | `iiiorg/piiranha-v1-detect-personal-information` | Piiranha model (if enabled). |
| `ANONYMIZER_SLM_MODEL` | `anonymizer-slm` | Ollama name of the imported Anonymizer SLM. |
| `ANONPROXY_TOLERANT` | `true` | Tolerant restoration (vs. exact). |
| `ANONPROXY_EPHEMERAL` | `false` | In-memory vault, nothing on disk. |
| `ANONPROXY_AUDIT` | `true` | Serve the `/audit` dashboard. |
| `PORT` / `HOST` | `8080` / `127.0.0.1` | Proxy listen address. |
| `ANTHROPIC_UPSTREAM` | `https://api.anthropic.com` | Anthropic upstream. |
| `OPENAI_UPSTREAM` | `https://api.openai.com` | OpenAI upstream. |
| `ANONPROXY_API_TOKEN` | *(empty)* | Require `X-Anonproxy-Token` on the engine API. |

## Detection backends

Detection is a configurable chain set by `ANONPROXY_DETECTORS` (the wizard asks
too). The regex floor is always on and always first; everything else is
optional, lazily loaded, and skipped with a warning if its dependency or model
isn't present — so the default never breaks. Pick the trade-off your colleague
wants:

| Backend | Catches | Cost / setup |
|---|---|---|
| `regex` *(always on)* | IPs, CIDRs, hashes, JWTs, cloud keys, MACs, FQDNs, emails, labelled creds, **payment cards (Luhn), session cookies (PHPSESSID/JSESSIONID/…), Bearer/Basic auth, SSNs** | none — deterministic floor |
| `ollama` *(default)* | bare hostnames, org/project/person names, unlabelled creds in prose | local Ollama + a model (`--model`) |
| `gliner2` ⭐ *(recommended robust)* | 42 PII types, best span-level F1 on SPY (beats the others), <100ms | `pip install "anonproxy[gliner2]"`, CPU-friendly |
| `openai-privacy-filter` | ~96–97% F1 PII (Apache-2.0, OpenAI) | `pip install "anonproxy[openai-pii]"` (torch) |
| `gliner` | zero-shot person/org/username/hostname/email (older urchade model) | `pip install "anonproxy[gliner]"`, CPU-friendly |
| `piiranha` | high-accuracy passwords/emails/usernames (6 languages) | `pip install "anonproxy[piiranha]"` (torch) |
| `anonymizer-slm` | purpose-built PII detect+replace (Eternis Qwen3 fine-tune) | import GGUF via `models/anonymizer-slm.Modelfile` |

> **Newest / best recall:** `gliner2` (Fastino's GLiNER2-PII, May 2026) currently
> tops the SPY PII benchmark — it's the recommended contextual backend when you
> want maximum coverage. `ANONPROXY_DETECTORS=regex,gliner2`. (When pulling
> `openai/privacy-filter`, use exactly that org — typosquats have appeared.)
>
> **Web-app testing:** the regex floor now covers payment cards, session cookies,
> Bearer/Basic auth and SSNs, so a lot of HTTP traffic is handled deterministically.
> But **names and addresses in request/response bodies are not regex-detectable** —
> for those add a PII model: `ANONPROXY_DETECTORS=regex,gliner2`. Always confirm on
> real traffic with `python -m anonproxy verify`.

```bash
# default, out of the box
ANONPROXY_DETECTORS=regex,ollama python -m anonproxy serve

# more robust: regex floor + a dedicated PII model, no Ollama needed
ANONPROXY_DETECTORS=regex,gliner python -m anonproxy serve

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

2. **A contextual backend** (`gliner2` / `ollama`) infers hostnames/org names it
   wasn't told about — good for catching scope you forgot to list.

Use both: seed what you know, let the model catch the rest.

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
upstream. Exit code is non-zero if anything real leaks.

Check what's actually active any time:

```bash
curl -s http://127.0.0.1:8080/anonproxy/health | python -m json.tool
# -> detectors[]: name, available, model/effective_model, detail
```

If a configured Ollama model isn't pulled, the detector auto-falls back to an
installed one and says so in `health` and `verify` (so "Ollama is running but no
model" can't silently degrade you to regex-only).

## Tests

```bash
python3 -m pytest -q                    # 78 tests: round-trip, streaming, proxy, audit, verify, detectors, webapp, scope, config, polish
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
injection in tool output, or compromise of the local host. It is not a substitute
for reading what your NDA and engagement contract allow before using any cloud
AI on client data. Verify coverage per engagement with the `/audit` page,
`export`, and the test suite.

## License

MIT.
