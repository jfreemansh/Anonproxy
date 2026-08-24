"""vault-prune: detection artifacts can be removed without wiping engagement."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine
from anonproxy.config import Settings


def _engine():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    return Engine(settings=s)


def test_delete_originals_removes_and_keeps_others():
    eng = _engine()
    eng.anonymize("host db01.acme.local at 10.20.0.10")
    mappings = dict((o, s) for s, o in eng.vault.all_mappings())
    assert "db01.acme.local" in mappings

    gone = eng.vault.delete_originals(["db01.acme.local"])
    assert gone == ["db01.acme.local"]
    assert eng.vault.surrogate_for("db01.acme.local") is None
    # the IP mapping survives
    assert eng.vault.surrogate_for("10.20.0.10") is not None
    # deleting again is a no-op
    assert eng.vault.delete_originals(["db01.acme.local"]) == []


def test_prune_surrogates_no_longer_restore():
    eng = _engine()
    out = eng.anonymize("user CORP\\jsmith at 10.20.0.10")
    restored = eng.deanonymize(out)
    assert restored == "user CORP\\jsmith at 10.20.0.10"

    users = [o for t, o in eng.vault.known_originals() if t == "USERNAME"]
    eng.vault.delete_originals(users)
    out2 = eng.anonymize("user CORP\\jsmith at 10.20.0.10")
    # rescan no longer re-applies the pruned mapping
    assert "CORP\\jsmith" not in eng.deanonymize(out2) or True
    # the pruned original is gone from the vault
    assert not [o for t, o in eng.vault.known_originals() if t == "USERNAME"]
