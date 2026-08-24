# Anonproxy — Burp Suite extension

A thin Burp extension (Montoya API) that delegates anonymization/restoration to
the **same Anonproxy engine** the proxy uses. Burp, Claude Code, and any
OpenAI-compatible client therefore share **one engagement vault** and produce
**identical, consistent surrogates** — and you get the tolerant restorer on
responses, which Burp's literal Match/Replace rules cannot do.

## Why this beats Match/Replace rules

Burp's Match/Replace is literal-string only:

* it only replaces strings you pre-seeded — it has no detection, so anything you
  didn't anticipate (a new hostname, a fresh hash) leaks;
* it can't restore a surrogate the model reformatted (`**host-ab12**`, case
  changes, line wraps) — exactly the ~75% problem.

This extension calls the engine, which does layered detection (regex + local
LLM + known-entity rescan) and tolerant, format-aware restoration.

## Build

```bash
cd burp
gradle jar          # -> build/libs/anonproxy-burp-0.1.0.jar
```

(Requires JDK 17+ and Gradle. The Montoya API is `compileOnly`; Burp supplies it
at runtime.)

## Load

1. Start the engine:  `python -m anonproxy serve --engagement acme-2026`
2. Burp → Extensions → Add → Extension type **Java** → select the jar.
3. Configure via environment variables before launching Burp:

   | Variable | Default | Meaning |
   |---|---|---|
   Configuration precedence: JVM system property > environment variable > default.
Burp launched from the Dock does not inherit your shell env — prefer system
properties via your launcher/vmoptions (`-Danonproxy.engine=…`,
`-Danonproxy.engagement=acme-2026`, `-Danonproxy.token=…`) or
`launchctl setenv ENGAGEMENT_ID …`.

| Setting | Default | Purpose |
|---|---|---|
| `ANONPROXY_ENGINE` / `anonproxy.engine` | `http://127.0.0.1:8099` | engine API base URL |
| `ENGAGEMENT_ID` / `anonproxy.engagement` | `default` | vault isolation per client |
| `ANONPROXY_API_TOKEN` / `anonproxy.token` | *(empty)* | engine API auth token |
   | `ENGAGEMENT_ID` | `default` | vault to use (must match the proxy) |
   | `ANONPROXY_API_TOKEN` | *(empty)* | sent as `X-Anonproxy-Token` if set |

## Use

The extension is **opt-in per request** to avoid touching unrelated traffic:
add a header `X-Anonproxy: anon` to any Repeater/Intruder request you want
anonymized. The extension anonymizes the outbound body and `Cookie` header,
and restores real values in the response body and any `Set-Cookie` headers.
Use the engine's `/anonproxy/export` to audit every mapping at session close.

**Scope, deliberately:** only the `Cookie`/`Set-Cookie` headers are touched,
not `Host` or `Authorization`. For the intended workflow — a Repeater request
*to* an LLM endpoint — `Host` is the LLM provider's own domain and
`Authorization` is your own API key; anonymizing either would break delivery.
A `Cookie` header, though, is never required to reach an LLM API, and a
leftover one from "Send to Repeater"-ing a captured target request (with a
real, live session token) is exactly the accidental-leak class this tool
exists to catch — that was a real gap until this fix.

**Request/response correlation uses `messageId()`**, not a header surviving
into `initiatingRequest()` — Burp's javadoc confirms a response's `messageId()`
is identical to its request's, which sidesteps any question about whether
`initiatingRequest()` reflects the request pre- or post-modification.

**Verified live, end to end.** Built with `gradle jar`, loaded into a real Burp
instance, and driven through Burp's own MCP integration: real requests with a
real `Cookie` header and a fake IP in the body, sent to httpbin.org, confirmed
via httpbin's own echo that only surrogates ever left the machine and that the
response correctly restored real values.

That live test also caught a real bug the Montoya javadoc alone didn't
surface: `withRemovedHeader(HttpHeader)` removes **by name**, not by the
specific instance. The original code removed-then-re-added each `Set-Cookie`
header one at a time in a loop, which meant every iteration wiped out
whatever the previous one had just added — only the *last* `Set-Cookie`
header would ever survive on a response carrying more than one (a very common
case). Fixed by computing every replacement value first, then doing exactly
one bulk `withRemovedHeader("Set-Cookie")` + one bulk `withAddedHeaders(...)`.
Re-verified against a real two-`Set-Cookie` response — both now survive
independently, each restoring correctly.

## Context menu: Copy anonymized to clipboard

Right-click any request/response (Proxy history, Repeater, an editor) and
choose **Anonproxy: Copy anonymized to clipboard** — the full HTTP text
lands on your clipboard with real values replaced by engagement
surrogates, ready to paste into claude.ai, ChatGPT, a ticket, wherever.
Fail-closed: if the engine is unreachable, nothing is copied.

## Engagement: follows the engine

By default the extension asks the engine which engagement it is serving
(`/anonproxy/health`) and uses that — so Burp always shares the vault the
menubar started, whatever its name. An explicit override still wins:
`-Danonproxy.engagement=acme-2026` or `ENGAGEMENT_ID=acme-2026`.
