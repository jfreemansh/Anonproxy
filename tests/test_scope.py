"""Engagement scope seed: bare hostnames / domains / orgs always anonymized."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy import Engine


def _engine(scope_terms, detectors=("regex",)):
    s = Settings()
    s.ephemeral = True
    s.detectors = list(detectors)
    s.scope_terms = list(scope_terms)
    return Engine(settings=s)


def test_bare_hostname_caught_via_scope():
    eng = _engine(["DC01", "WEB-PRD-03", "Acme Corp"])
    text = "Pivoted from DC01 to WEB-PRD-03 inside Acme Corp."
    anon = eng.anonymize(text, use_llm=False)   # regex-only mode
    for secret in ("DC01", "WEB-PRD-03", "Acme Corp"):
        assert secret not in anon, f"leaked {secret}"
    assert eng.deanonymize(anon) == text


def test_scope_runs_even_without_contextual():
    # scope is part of the floor, so it works with detectors=["regex"] and no LLM
    eng = _engine(["internalhost"], detectors=["regex"])
    anon = eng.anonymize("connect to internalhost now", use_llm=False)
    assert "internalhost" not in anon


def test_scope_word_boundary_no_partial_match():
    eng = _engine(["acme"])
    # "acme" should match as a token but not inside "acmespeak" or "placemat"
    anon = eng.anonymize("acme placemat acmespeak", use_llm=False)
    assert anon.split()[1] == "placemat"        # untouched
    assert "acmespeak" in anon                    # untouched
    assert anon.split()[0] != "acme"              # the standalone token was replaced


def test_scope_auto_included_when_terms_present():
    eng = _engine(["DC01"], detectors=["regex"])
    names = [d.name for d in eng.detectors]
    assert "scope" in names


def test_scope_type_override():
    eng = _engine(["10.20.0.0/16=CIDR", "VAULT01"])
    out = eng.anonymize("range 10.20.0.0/16 host VAULT01", use_llm=False)
    assert "10.20.0.0/16" not in out and "VAULT01" not in out
