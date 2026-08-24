# Security policy

Anonproxy exists to protect sensitive data; reports about ways it fails to do
that are top priority.

## Supported versions

Only the latest release (and `main`) is supported.

## Reporting a vulnerability

Use GitHub's **private vulnerability reporting** on this repository
(Security → Report a vulnerability). Please do not open public issues for
anything that could expose the anonymization guarantees.

High-value areas, if you want direction:

* bypassing redaction via request/response shapes not covered by
  `anonproxy/proxy/transform.py` or `streaming.py`
* restoration correctness (`restorer.py`) — anything returning *wrong*
  original values is a data-integrity bug
* vault confidentiality (`vault.py`, including the encrypted-at-rest path)
* the local engine API / audit dashboard (`proxy/app.py`, `audit.py`)

## Hardening recommendations for operators

* keep the proxy bound to loopback (the default) — set `ANONPROXY_API_TOKEN`
  if anything else can reach it
* prefer `ANONPROXY_VAULT_PASSPHRASE` or ephemeral mode over plaintext vaults
* rotate engagement ids per client; see THREAT-MODEL.md for residual risks
