"""Ollama detector must re-probe availability after a live query fails,
instead of trusting a stale cached "available" forever (silent, uncounted
detection outage if Ollama dies mid-engagement)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy.detectors.llm_detector import LLMDetector


class _Resp:
    def __init__(self, json_data, status=200):
        self._json = json_data
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")

    def json(self):
        return self._json


def test_availability_reprobes_after_query_failure(monkeypatch):
    import anonproxy.detectors.llm_detector as mod

    s = Settings()
    s.ollama_model = "qwen3:4b"
    det = LLMDetector(s)

    # Ollama up, model installed
    monkeypatch.setattr(mod.httpx, "get",
                        lambda *a, **kw: _Resp({"models": [{"name": "qwen3:4b"}]}))
    assert det.available() is True

    # a live query then fails (Ollama crashed mid-session)
    def _boom(*a, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(mod.httpx, "post", _boom)
    assert det.detect("host 10.20.0.10") == []

    # the stale cache must be invalidated — status reflects the outage
    # (check before any other call, since available()/model_status() would
    # immediately re-probe and overwrite _reason)
    assert det._available is None
    assert "query failed" in det._reason

    # and the NEXT availability check actually re-probes rather than trusting
    # the old "True" — simulate Ollama still being down
    def _get_down(*a, **kw):
        raise RuntimeError("connection refused")
    monkeypatch.setattr(mod.httpx, "get", _get_down)
    assert det.available() is False
