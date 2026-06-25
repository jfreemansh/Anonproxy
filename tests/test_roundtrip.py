"""
Round-trip reliability tests — the reason this project exists.

Each case anonymizes text, then simulates how an LLM commonly *mangles* the
surrogate in its reply, then asserts the tolerant restorer still recovers the
original value.  The naive ``str.replace`` baseline is tested alongside to make
the improvement explicit.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine, Settings


def fresh_engine():
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False          # deterministic: regex + consistency only
    return Engine(engagement="test", settings=s)


# (label, original-text, function that mangles the surrogate the way an LLM might)
MANGLERS = {
    "verbatim":      lambda s: s,
    "bold":          lambda s: f"**{s}**",
    "inline_code":   lambda s: f"`{s}`",
    "italic_star":   lambda s: f"*{s}*",
    "uppercased":    lambda s: s.upper(),
    "lowercased":    lambda s: s.lower(),
    "code_fence":    lambda s: f"```\n{s}\n```",
    "sentence":      lambda s: f"The host {s} is reachable.",
}


SAMPLES = [
    "Host dc01.acmecorp.local resolved to 10.20.0.10",
    "Cracked NTLM hash 8846f7eaee8fb117ad06bdd830b7586c for user CORP\\jsmith",
    "Found AWS key AKIAIOSFODNN7EXAMPLE in the config",
    "Login admin:Sup3rS3cret2024! worked against 192.168.50.5",
    "Contact john.smith@acmecorp.com about FILESERVER-PRD",
]


def _surrogates_in(engine, original_text):
    """Anonymize and return the surrogate string for assertions."""
    return engine.anonymize(original_text)


@pytest.mark.parametrize("text", SAMPLES)
@pytest.mark.parametrize("mangle_name", list(MANGLERS))
def test_tolerant_roundtrip(text, mangle_name):
    engine = fresh_engine()
    anon = _surrogates_in(engine, text)
    assert anon != text, "nothing was anonymized"

    mangled = MANGLERS[mangle_name](anon)
    restored = engine.deanonymize(mangled)

    # every original token that was replaced must reappear after restoration
    # (compare on the anonymized->restored vs the original entities)
    for original in _original_tokens(engine):
        assert original in restored, (
            f"[{mangle_name}] lost {original!r}\n  anon={anon!r}\n  restored={restored!r}"
        )


def _original_tokens(engine):
    return [row["original"] for row in engine.export()]


def test_naive_baseline_fails_where_tolerant_succeeds():
    """Demonstrate the concrete failure the tolerant restorer fixes."""
    engine = fresh_engine()
    text = "Host dc01.acmecorp.local at 10.20.0.10"
    anon = engine.anonymize(text)

    # LLM echoes each surrogate back wrapped in bold + case-changed
    mangled = anon
    for surrogate, _ in engine.vault.all_mappings():
        mangled = mangled.replace(surrogate, f"**{surrogate.upper()}**")

    # naive exact replace (what the original did)
    naive = mangled
    for surrogate, original in engine.vault.all_mappings():
        naive = naive.replace(surrogate, original)

    tolerant = engine.deanonymize(mangled)

    originals = _original_tokens(engine)
    assert any(o not in naive for o in originals), "baseline unexpectedly passed"
    assert all(o in tolerant for o in originals), "tolerant restore failed"


def test_consistency_same_surrogate_each_time():
    engine = fresh_engine()
    a = engine.anonymize("first sighting 10.20.0.10")
    b = engine.anonymize("later sighting 10.20.0.10 again")
    # the surrogate for the IP must be identical across calls
    surr = engine.vault.surrogate_for("10.20.0.10")
    assert surr is not None
    assert surr in a and surr in b


def test_format_preserving_shapes():
    engine = fresh_engine()
    import ipaddress
    ip_s = engine.vault  # ensure created
    engine.anonymize("10.20.0.10 and hash 8846f7eaee8fb117ad06bdd830b7586c")
    ip_sur = engine.vault.surrogate_for("10.20.0.10")
    ipaddress.ip_address(ip_sur)                      # valid IP
    h_sur = engine.vault.surrogate_for("8846f7eaee8fb117ad06bdd830b7586c")
    assert len(h_sur) == 32 and all(c in "0123456789abcdef" for c in h_sur.lower())
