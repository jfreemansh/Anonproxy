"""Vault surrogate-collision path: 64 salted attempts, then a clear error."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from anonproxy.config import Settings
from anonproxy.vault import Vault
from anonproxy import surrogates


def test_collision_exhaustion_logs_and_raises(monkeypatch, caplog):
    s = Settings()
    s.ephemeral = True
    v = Vault(s)

    # every call returns the same surrogate regardless of salt, forcing the
    # 64-attempt loop to exhaust once one original already holds it
    monkeypatch.setattr(surrogates, "generate", lambda *a, **kw: "always-the-same-surrogate")

    v.get_or_create("first-value", "IP_ADDRESS")
    with caplog.at_level("ERROR", logger="anonproxy.vault"):
        with pytest.raises(RuntimeError, match="could not generate a unique surrogate"):
            v.get_or_create("second-value", "IP_ADDRESS")

    assert any("second-value" in r.getMessage() for r in caplog.records)
