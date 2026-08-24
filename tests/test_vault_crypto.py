"""Opt-in vault-at-rest encryption: envelope round-trip, wrong key, close-out."""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine  # noqa: E402
from anonproxy.config import Settings  # noqa: E402
from anonproxy.profiles import close_engagement  # noqa: E402


def _settings(tmp_path, **kw):
    s = Settings()
    s.vault_dir = tmp_path / "vaults"
    s.detectors = ["regex"]
    s.engagement_id = "enc-test"
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def test_encrypted_roundtrip_and_no_plaintext(tmp_path):
    s = _settings(tmp_path, vault_passphrase="correct horse")
    eng = Engine(settings=s)
    out = eng.anonymize("host dc01.acme.local at 10.20.0.10")
    assert "dc01.acme.local" not in out
    assert eng.deanonymize(out) == "host dc01.acme.local at 10.20.0.10"

    enc_path = str(s.vault_path()) + ".enc"
    plain = str(s.vault_path())
    assert os.path.exists(enc_path), "envelope must exist"
    assert not os.path.exists(plain), "no plaintext sqlite may remain"
    blob = open(enc_path, "rb").read()
    assert blob[:7] == b"ANPENC1" and b"acme" not in blob

    # reopen with same passphrase: mappings survive
    eng2 = Engine(settings=s)
    assert eng2.deanonymize(out) == "host dc01.acme.local at 10.20.0.10"
    assert eng2.stats()["encrypted"] is True


def test_wrong_passphrase_fails_clean(tmp_path):
    s1 = _settings(tmp_path, vault_passphrase="right")
    Engine(settings=s1).anonymize("10.20.0.10")
    s2 = _settings(tmp_path, vault_passphrase="wrong")
    with pytest.raises(RuntimeError, match="PASSPHRASE"):
        Engine(settings=s2)


def test_unencrypted_mode_unchanged(tmp_path):
    s = _settings(tmp_path)
    eng = Engine(settings=s)
    eng.anonymize("10.20.0.10")
    assert os.path.exists(str(s.vault_path()))
    assert not os.path.exists(str(s.vault_path()) + ".enc")
    assert eng.stats()["encrypted"] is False


def test_close_out_removes_envelope(tmp_path):
    s = _settings(tmp_path, vault_passphrase="k")
    Engine(settings=s).anonymize("10.20.0.10")
    res = close_engagement(s, exports_dir=tmp_path / "ex")
    assert res["count"] >= 1 and res["vault_removed"] is True
    assert not os.path.exists(str(s.vault_path()) + ".enc")
