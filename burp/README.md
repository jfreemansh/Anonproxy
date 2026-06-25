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
   | `ANONPROXY_ENGINE` | `http://127.0.0.1:8080` | engine API base URL |
   | `ENGAGEMENT_ID` | `default` | vault to use (must match the proxy) |
   | `ANONPROXY_API_TOKEN` | *(empty)* | sent as `X-Anonproxy-Token` if set |

## Use

The extension is **opt-in per request** to avoid touching unrelated traffic:
add a header `X-Anonproxy: anon` to any Repeater/Intruder request you want
anonymized. The extension strips real data from the outbound body and restores
real values in the response body. Use the engine's `/anonproxy/export` to audit
every mapping at session close.
