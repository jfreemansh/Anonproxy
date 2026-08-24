"""Confidence plumbing: backends' scores reach export + the audit UI."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from anonproxy.config import Settings
from anonproxy.detectors import Match, build_detectors
from anonproxy.engine import Engine


class _ShakyBackend:
    name = "shaky"
    floor = False

    def available(self):
        return True

    def detect(self, text):
        hits = []
        if "webdb" in text:
            hits.append(Match(text="webdb", entity_type="HOSTNAME",
                              source="shaky", confidence=0.55))
        return hits

    def status(self):
        return {"available": True}


def _engine():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    eng = Engine(settings=s)
    eng.detectors = [build_detectors(s)[0], _ShakyBackend()]
    return eng


def test_confidences_flow_to_export():
    eng = _engine()
    eng.anonymize("host webdb at 10.20.0.10")
    rows = {r["original"]: r for r in eng.export()}
    assert rows["webdb"]["confidence"] == 0.55     # backend score preserved
    assert rows["10.20.0.10"]["confidence"] == 1.0  # deterministic floor


def test_rescan_keeps_original_score():
    eng = _engine()
    eng.anonymize("host webdb up")
    s2 = Settings()
    s2.detectors = ["regex"]
    eng.detectors = [build_detectors(s2)[0]]   # backend gone next turn
    out = eng.anonymize("webdb again")
    assert "webdb" not in out                          # consistency rescan
    rows = {r["original"]: r for r in eng.export()}
    assert rows["webdb"]["confidence"] == 0.55         # score retained, not reset


def test_api_and_page_expose_it():
    from anonproxy import audit
    from anonproxy.proxy.app import create_app
    s = Settings()
    s.ephemeral = True
    s.llm_enabled = False
    s.engagement_id = "conf"
    s.detectors = ["regex"]
    app = create_app(s)
    tc = TestClient(app)
    tc.post("/anonproxy/anonymize", json={"text": "ip 10.20.0.10",
                                          "engagement": "conf"})
    rows = tc.get("/anonproxy/export?engagement=conf").json()["mappings"]
    assert all("confidence" in m for m in rows)
    assert all(m["confidence"] == 1.0 for m in rows)   # regex floor
    page = audit.render_page("t", token_required=False)
    assert 'id="lowonly"' in page and "confidence" in page
