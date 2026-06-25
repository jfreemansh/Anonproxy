# Anonproxy — Quick Start

A no-jargon guide. If you can copy-paste three commands, you can run this.

## What it does (30 seconds)

When you use a cloud AI (Claude, ChatGPT) on a pentest, your prompts can contain
client data — IPs, hostnames, passwords, hashes. You don't want that data sitting
in someone else's logs.

Anonproxy fixes that. It runs **on your machine**. Your AI tools talk to it
instead of the cloud. It:

1. Replaces real client data with realistic fakes **before** anything leaves your
   laptop (`10.20.0.10` → `203.0.113.47`, `dc01.acme.local` → `host-ab12.pentest.local`).
2. Sends only the fakes to the cloud AI.
3. Puts the **real** values back into the answer you read.

The cloud never sees real data. You never see the fakes. Nothing else changes.

## Setup (one time)

You need Python 3.10+. A local "Ollama" model is optional but improves results.

```bash
pip install -r requirements.txt
python -m anonproxy wizard
```

The wizard asks a few simple questions (client name, which model, etc.) and
starts everything for you. That's it.

> Prefer not to answer questions? Just run:
> `python -m anonproxy serve --engagement my-client`

## Tell it your scope (do this — it's the big one)

Patterns catch IPs, emails, full domains and the like automatically. But they
**can't guess a bare hostname** like `DC01` or a company name in prose. So just
tell Anonproxy what's in scope.

The wizard creates a file named `<client>-scope.txt`. Open it and list the
client's domains, hostnames and org name — one per line:

```
acme.com
portal.acme.com
DC01
WEB-PRD-03
Acme Corp
```

Everything in that file is always swapped out, even bare names. Keep adding to it
as you discover more during the test. (You can also pass `--scope "acme.com,DC01"`
on the command line.)

## Daily use

**1. Start it** (once per work session):

```bash
python -m anonproxy serve --engagement my-client
```

Use a different `--engagement` name for each client — it keeps their data
separate.

**2. Point your AI tool at it.** Leave the proxy running and, in another terminal:

```bash
# Claude Code
export ANTHROPIC_BASE_URL=http://127.0.0.1:8080
claude
```

For other tools, set their "base URL" / "API endpoint" to
`http://127.0.0.1:8080` (Claude) or `http://127.0.0.1:8080/v1` (OpenAI-style).

**3. Work normally.** Run your tools, paste output, ask questions. Everything is
anonymized and restored automatically.

## See what it's doing

Open this in your browser while the proxy runs:

```
http://127.0.0.1:8080/audit
```

You'll see a live table of every real value and the fake it was swapped for, with
a button to export it for your report.

## Check it before you trust it

Run this any time to confirm nothing leaks:

```bash
python -m anonproxy verify
```

It feeds sample tool output (nmap, password dumps, configs) through the system
and tells you, in plain language, whether anything slipped through. Green = good.

## When you're done

Your data lives in a local file per client. To wipe it, delete the engagement's
vault, or run with `ANONPROXY_EPHEMERAL=1` so nothing is ever written to disk.

## Common questions

**Do I need Burp Suite?** No. Anonproxy is a standalone proxy. Burp support is a
separate optional add-on for people who specifically test traffic inside Burp.

**Does my data go to a server somewhere?** No. Everything runs on your machine.
There is no cloud component except the AI you were already using.

**What if I don't install the local Ollama model?** It still works — the built-in
pattern matching catches IPs, hashes, keys, emails and FQDNs. The local model
just adds extra coverage for things like bare hostnames and names in prose.

**Is this a guarantee?** No tool is. It's a strong safety layer. Always follow
your NDA and engagement rules, and use `verify` and the audit page to check.

---

Want the full technical details, configuration options, and the Burp add-on? See
[README.md](README.md).
