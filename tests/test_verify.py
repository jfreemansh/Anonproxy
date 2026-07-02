"""verify: regex floor must catch all regex-tagged secrets with zero leaks and
intact round-trips, even without Ollama."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings
from anonproxy import verify


def test_regex_only_no_leaks_and_roundtrip():
    s = Settings()
    s.llm_enabled = False
    report = verify.run(s, use_llm=False)
    assert report["total_leaks"] == 0, report["results"]
    assert report["roundtrip_failures"] == 0, report["results"]


def test_llm_tagged_secrets_flagged_not_leaked_without_ollama():
    s = Settings()
    s.llm_enabled = False
    report = verify.run(s, use_llm=False)
    # DC01 / Summer2024! are llm-only; regex-only they must show as needs-LLM,
    # never as a silent leak
    assert report["needs_llm"] >= 1
    for r in report["results"]:
        assert not r["leaks"]


def test_model_name_matching():
    from anonproxy.detectors.llm_detector import LLMDetector as L
    installed = ["qwen2.5:3b", "llama3.2:latest"]
    # exact
    assert L._match_model("qwen2.5:3b", installed) == "qwen2.5:3b"
    # base name -> tagged variant
    assert L._match_model("qwen2.5", installed) == "qwen2.5:3b"
    # name -> :latest
    assert L._match_model("llama3.2", installed) == "llama3.2:latest"
    # not installed
    assert L._match_model("phi3.5", installed) is None


def test_detector_status_shape():
    s = Settings()
    s.detectors = ["regex"]
    report = verify.run(s, use_llm=False)
    assert isinstance(report["detectors"], list)
    names = [d["name"] for d in report["detectors"]]
    assert "regex" in names
    for d in report["detectors"]:
        assert "available" in d and "name" in d


def test_adversarial_probe_no_outbound_leak():
    s = Settings()
    s.detectors = ["regex"]
    report = verify.run(s, use_llm=False)
    # regex-layer secrets must never appear in what would be sent upstream
    assert report["adversarial"]["leaked"] == []


def test_tool_call_probe_no_leak():
    s = Settings()
    s.detectors = ["regex"]
    report = verify.run(s, use_llm=False)
    tcp = report["tool_call_probe"]
    assert tcp["anthropic_tool_use_leak"] is False
    assert tcp["openai_tool_call_leak"] is False
