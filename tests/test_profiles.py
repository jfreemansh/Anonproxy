"""Engagement profiles: per-test context store + close-out ritual."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.profiles import (Profile, ProfileStore, client_env_lines,
                                close_engagement, sanitize_name)


def _store(tmp_path):
    return ProfileStore(directory=tmp_path / "profiles")


def test_sanitize_name():
    assert sanitize_name("Acme Corp Web!") == "Acme-Corp-Web"
    # path traversal is neutralized: bad chars -> '-', leading '-/.' stripped
    assert sanitize_name("../../etc/passwd") == "etc-passwd"
    assert sanitize_name("  ") == "engagement"
    assert sanitize_name("x" * 100).endswith("x") and len(sanitize_name("x" * 100)) == 64


def test_store_roundtrip_and_recency(tmp_path):
    st = _store(tmp_path)
    a = Profile(name="acme-web", scope_terms=["acme.com"], port=8081,
                notes="web test")
    b = Profile(name="acme-api", ephemeral=True)
    st.save(a)
    st.save(b)
    got = st.get("acme-web")
    assert got.scope_terms == ["acme.com"] and got.port == 8081 and got.notes == "web test"
    # most-recently-used first once touched
    st.touch("acme-web")
    names = [p.name for p in st.list()]
    assert names[0] == "acme-web" and set(names) == {"acme-web", "acme-api"}
    assert st.delete("acme-api") and not st.exists("acme-api")
    assert st.get("nope") is None


def test_apply_maps_profile_onto_settings(tmp_path):
    prof = Profile(name="t1", scope_terms=["a.com"], detectors=["regex"],
                   ollama_model="m:latest", ephemeral=True, port=9099)
    s = Settings()
    s.host = "127.0.0.1"
    prof.apply(s)
    assert s.engagement_id == "t1"
    assert s.scope_terms == ["a.com"] and s.detectors == ["regex"]
    assert s.ollama_model == "m:latest" and s.ephemeral and s.port == 9099


def test_client_env_lines():
    lines = "\n".join(client_env_lines(Profile(name="p", port=8123)))
    assert "export ANTHROPIC_BASE_URL=http://127.0.0.1:8123" in lines
    assert 'OPENAI_BASE_URL="http://127.0.0.1:8123/v1"' in lines


def test_close_exports_evidence_and_wipes_vault(tmp_path):
    from anonproxy import Engine

    s = Settings()
    s.vault_dir = tmp_path / "vaults"
    s.engagement_id = "close-t"
    # deterministic: regex floor only (a live local Ollama would add entities)
    s.detectors = ["regex"]
    eng = Engine(settings=s)                    # persistent vault on disk
    eng.anonymize("host dc01.acme.local at 10.20.0.10")

    res = close_engagement(s, exports_dir=tmp_path / "exports")
    assert res["count"] == 2
    for path in (res["json"], res["csv"]):
        assert path.startswith(str(tmp_path / "exports"))
        assert os.path.exists(path)
    data = json.loads(open(res["json"]).read())
    originals = {m["original"] for m in data["mappings"]}
    assert {"dc01.acme.local", "10.20.0.10"} <= originals
    assert "original,surrogate" in open(res["csv"]).read().replace(" ", "")
    assert res["vault_removed"] is True
    assert not os.path.exists(str(s.vault_path()))          # vault wiped
    # keep_vault leaves it alone
    eng2 = Engine(settings=s)
    eng2.anonymize("10.20.0.10")
    res2 = close_engagement(s, keep_vault=True, exports_dir=tmp_path / "e2")
    assert res2["vault_removed"] is False and os.path.exists(str(s.vault_path()))


def test_cli_profile_new_list_env_rm(tmp_path, monkeypatch, capsys):
    from anonproxy import cli
    monkeypatch.setenv("ANONPROXY_PROFILE_DIR", str(tmp_path / "profiles"))
    # cli.main() loads the developer's real .env into os.environ — poison for
    # every later test building a default Settings(). Keep this hermetic.
    monkeypatch.setattr(cli, "_load_dotenv", lambda *a, **kw: None)

    rc = cli.main(["profile", "new", "acme web",
                   "--scope", "acme.com , DC01", "--port", "8091",
                   "--detectors", "regex", "--notes", "n1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "saved" in out and "acme-web.json" in out

    cli.main(["profile", "list"])
    listed = capsys.readouterr().out
    assert "acme-web" in listed and "8091" in listed and "regex" in listed

    cli.main(["env", "acme-web"])
    env = capsys.readouterr().out
    assert "ANTHROPIC_BASE_URL=http://127.0.0.1:8091" in env

    # duplicate creation is rejected; unknown profile resolution errors cleanly
    try:
        cli.main(["profile", "new", "acme-web"])
        raise AssertionError("expected SystemExit")
    except SystemExit:
        pass
    try:
        cli.main(["up", "ghost"])
        raise AssertionError("expected SystemExit")
    except SystemExit as e:
        assert "ghost" in str(e)

    assert cli.main(["profile", "rm", "acme-web"]) == 0
