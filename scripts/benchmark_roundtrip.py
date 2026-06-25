#!/usr/bin/env python3
"""
Quantify the improvement: tolerant restorer vs. naive str.replace baseline,
across realistic LLM mangling patterns.  Prints a pass-rate table.

Run:  python3 scripts/benchmark_roundtrip.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine, Settings

SAMPLES = [
    "Host dc01.acmecorp.local resolved to 10.20.0.10 on the internal VLAN.",
    "Cracked NTLM 8846f7eaee8fb117ad06bdd830b7586c for CORP\\jsmith.",
    "Found AWS key AKIAIOSFODNN7EXAMPLE and token ghp_aBcdEfGhIjKlMnOpQrStUvWxYz0123456789.",
    "admin:Sup3rS3cret2024! authenticated to 192.168.50.5 over SMB.",
    "Email john.smith@acmecorp.com manages FILESERVER-PRD and DC01.",
    "Path C:\\Users\\jsmith\\engagements\\acme\\loot.txt held the dump.",
    "Subnet 10.20.0.0/24 routes to gateway 10.20.0.1.",
    "JWT eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abcDEF held the session.",
]

# How LLMs commonly rewrite a surrogate when echoing it back.
MANGLERS = {
    "verbatim":     lambda s: s,
    "bold":         lambda s: f"**{s}**",
    "inline_code":  lambda s: f"`{s}`",
    "italic":       lambda s: f"*{s}*",
    "uppercase":    lambda s: s.upper(),
    "lowercase":    lambda s: s.lower(),
    "underscored":  lambda s: f"_{s}_",
    "in_sentence":  lambda s: f"the value {s} here",
    # noise injected *inside* the token — these break exact substring matching
    "intra_code":   lambda s: f"`{s[:4]}`{s[4:]}" if len(s) > 4 else f"`{s}`",
    "nbsp_hyphen":  lambda s: s.replace("-", "‑"),
    "bold_segment": lambda s: s.replace(".", "**.**", 1) if "." in s else f"**{s}**",
}


def fresh():
    st = Settings()
    st.ephemeral = True
    st.llm_enabled = False
    return Engine(engagement="bench", settings=st)


def naive_restore(text, mappings):
    for surrogate, original in mappings:
        text = text.replace(surrogate, original)
    return text


def run():
    results = {name: [0, 0] for name in MANGLERS}  # name -> [naive_ok, tolerant_ok]
    totals = [0, 0, 0]  # cases, naive_ok, tolerant_ok
    n_samples = 0

    for sample in SAMPLES:
        eng = fresh()
        anon = eng.anonymize(sample)
        mappings = eng.vault.all_mappings()
        originals = [r["original"] for r in eng.export()]
        if not originals:
            continue
        n_samples += 1
        for name, mangle in MANGLERS.items():
            # simulate the model rewriting each surrogate where it appears
            mangled = anon
            for surrogate, _ in mappings:
                mangled = mangled.replace(surrogate, mangle(surrogate))
            naive = naive_restore(mangled, mappings)
            tol = eng.deanonymize(mangled)
            naive_ok = all(o in naive for o in originals)
            tol_ok = all(o in tol for o in originals)
            results[name][0] += int(naive_ok)
            results[name][1] += int(tol_ok)
            totals[0] += 1
            totals[1] += int(naive_ok)
            totals[2] += int(tol_ok)

    print(f"\n{'mangling':<14}{'naive str.replace':>20}{'tolerant restorer':>20}")
    print("-" * 54)
    for name, (nv, tv) in results.items():
        print(f"{name:<14}{nv}/{n_samples:>5}  ({100*nv/n_samples:5.0f}%)"
              f"{tv:>8}/{n_samples}  ({100*tv/n_samples:5.0f}%)")
    print("-" * 54)
    c, nv, tv = totals
    print(f"{'OVERALL':<14}{nv}/{c:>5}  ({100*nv/c:5.0f}%)"
          f"{tv:>8}/{c}  ({100*tv/c:5.0f}%)")
    print()
    return totals


if __name__ == "__main__":
    run()
