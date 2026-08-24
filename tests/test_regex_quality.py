"""Regex-floor quality: field-observed false positives must stay dead and
labeled entities must stay caught (burp-history analysis workload).

Regression guard for the serialized-JSON trap: bodies arrive with \\r\\n as
literal backslash sequences, so value classes that only exclude literal
whitespace swallow headers and whole JSON records into one token.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.detectors.regex_detector import _RULES


def _hits(text):
    out = []
    for rule in _RULES:
        etype, pat = rule[0], rule[1]
        grp = rule[2] if len(rule) > 2 else 0
        m = pat.search(text)
        if m:
            out.append((etype, m.group(grp) if grp else m.group(0)))
    return out


def test_cookie_value_stops_at_serialized_escapes():
    hits = _hits("Set-Cookie: wordpress_sec_ca11d19b=nickkilla%7C1783150759"
                 "%7CzXE0oi5BfKMLCqElPvLFwbz3Xv6pdikk; path=/x; HttpOnly")
    tokens = [v for t, v in hits if t == "TOKEN"]
    assert tokens, "cookie value must still be caught"
    for v in tokens:
        assert "\\r" not in v and "\\n" not in v, (
            f"token swallowed a serialized escape sequence: {v!r}")
        assert "path=" not in v and "HttpOnly" not in v


def test_benign_cookie_attributes_are_not_tokens():
    for text in ("; path=/wp-content/plugins; secure; HttpOnly",
                 "; path=/wp-admin; domain=.example.com"):
        tokens = [v for t, v in _hits(text) if t == "TOKEN"]
        assert not tokens, f"benign cookie attribute became a TOKEN: {tokens}"


def test_no_json_structure_eaten_into_tokens():
    hits = _hits('max-age=86400\\r\\n\\r\\n{"id":1,"username":"nickkilla"}')
    for t, v in hits:
        assert "{" not in v and '"username"' not in v, (
            f"{t} swallowed JSON structure: {v!r}")


def test_serialized_escape_sequences_are_not_usernames():
    for text in ("tail -50\\necho done", "re\\ndata here",
                 'error\\nfailed\\thard'):
        users = [v for t, v in _hits(text) if t == "USERNAME"]
        assert not users, f"\\-escape artifact became a USERNAME: {users}"


def test_labeled_usernames_are_caught():
    cases = [
        ('{"id":1,"username":"nickkilla","name":"me"}', "nickkilla"),
        ("log=nickkilla&wfls-email-verification=", "nickkilla"),
        ("user=jsmith&y=2", "jsmith"),
        ("login=admin&view=1", "admin"),
    ]
    for text, want in cases:
        users = [v for t, v in _hits(text) if t == "USERNAME"]
        assert want in users, f"labeled username {want!r} missed in {text!r}: {users}"


def test_labeled_username_excludes_booleans_and_numbers():
    for text in ("user=true&x=1", "user=1&action=x", "user=null&view=1"):
        users = [v for t, v in _hits(text) if t == "USERNAME"]
        assert not users, f"boolean/number became a USERNAME: {users}"


def test_js_object_literal_labels_are_not_usernames():
    """Field FP: burp exports carry i18n/UI tables (User:"Failed",
    log:"Hit") — unquoted-key colon pairs must never become USERNAMEs."""
    for text in ('User:"Failed"', 'log:"Hit"', 'log="Hit"', "log='Hit'",
                 'Delete:"Delete"', 'Username:"Username"', 'Acknowledge:"Ack"',
                 'Ticket:"Ticket"', 'Alert:"Alert"'):
        users = [v for t, v in _hits(text) if t == "USERNAME"]
        assert not users, f"UI label became a USERNAME: {users}"


def test_real_domain_accounts_still_caught():
    users = [v for t, v in _hits("server CORP\\jsmith logged in")
             if t == "USERNAME"]
    assert "CORP\\jsmith" in users


def test_labeled_credentials_still_caught():
    creds = [v for t, v in _hits("pwd=Jackhoffmaster1!&x=1")
             if t == "CREDENTIAL"]
    assert "Jackhoffmaster1!" in creds
