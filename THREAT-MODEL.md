# Threat model

What Anonproxy protects, what it deliberately does not, and what is left to
the operator. Honest framing: **this is risk reduction, not a guarantee**.

## Assets

1. Real client data inside LLM request bodies (the thing we exist to protect)
2. Vault mappings (`original ↔ surrogate` — as sensitive as the originals)
3. Credentials transiting headers (`x-api-key` / OAuth bearer) en route upstream

## Trust boundaries

```
client tools ──(localhost HTTP)──▶ Anonproxy ──(TLS, internet)──▶ LLM API
                                     ▲
operator browser ───(localhost)──────┘   /audit dashboard
```

## Protections in scope

| Threat | Mitigation |
|---|---|
| Client data in outbound bodies | regex floor + contextual backends + consistency rescan replace every detected entity before forwarding |
| Surrogates coming back unreversed | tolerant restorer incl. streamed tool-call arguments |
| Payload-bearing endpoints bypassing redaction | `/v1/messages`, `/chat/completions`, `/completions`, `/count_tokens` handled; strict mode refuses unknown `/v1/*` writes |
| Local attacker reading the engine API | token-gated (`ANONPROXY_API_TOKEN`); loopback bind by default |
| Operator browser exposure of mappings | audit page carries no data unauthenticated; disableable entirely |
| Vault theft at rest | opt-in AES-GCM envelope (`ANONPROXY_VAULT_PASSPHRASE`) or ephemeral mode |
| Cross-client bleed | per-engagement vaults; deterministic surrogates keyed by engagement id |

## Explicitly out of scope (residual risks)

| Threat | Why it remains | Operator mitigation |
|---|---|---|
| Query-pattern correlation | consistent surrogates are linkable across turns by design | rotate engagement id per session |
| Non-text content | images/screenshots/PDFs bypass text rewriting | never paste raw evidence into vision-capable chats |
| Prompt injection in tool output | model may be manipulated within the conversation | standard LLM-injection hygiene; nothing real to leak but behavior can still be steered |
| Compromise of the local host | proxy runs with your privileges; plaintext temp db during runtime | full-disk encryption, passphrase-protected vaults, close-out ritual |
| Unredacted detection misses | regex+LLM recall is probabilistic | `verify` before engagements; review `/audit` live |
| Upstream sees metadata | traffic timing/size/model usage visible to provider | unavoidable without batching/rerouting designs |

## Verification

`python -m anonproxy verify` runs leak, round-trip, adversarial-echo,
tool-call-payload and benign-pattern probes against the active detector chain;
CI runs the same suite on every push.
