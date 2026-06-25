"""Audit dashboard + token gating."""
import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.proxy.app import create_app


def _client(token=""):
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "audit"
    s.engine_api_token = token
    return TestClient(create_app(s)), s


def test_audit_page_served():
    tc, _ = _client()
    r = tc.get("/audit")
    assert r.status_code == 200
    assert "Anonproxy audit" in r.text
    assert "audit" in r.text  # engagement name injected


def test_audit_disabled():
    s = Settings(); s.ephemeral = True; s.llm_enabled = False; s.audit_enabled = False
    tc = TestClient(create_app(s))
    assert tc.get("/audit").status_code == 404


def test_export_reflects_anonymized_entities():
    tc, _ = _client()
    tc.post("/anonproxy/anonymize",
            json={"text": "host 10.20.0.10", "engagement": "audit"})
    r = tc.get("/anonproxy/export?engagement=audit")
    assert r.status_code == 200
    originals = [m["original"] for m in r.json()["mappings"]]
    assert "10.20.0.10" in originals


def test_token_via_query_param():
    tc, _ = _client(token="sekret")
    # missing token -> 401
    assert tc.get("/anonproxy/export").status_code == 401
    # header
    assert tc.get("/anonproxy/export",
                  headers={"X-Anonproxy-Token": "sekret"}).status_code == 200
    # query param (used by the audit page)
    assert tc.get("/anonproxy/export?token=sekret").status_code == 200
