"""Settings normalisation (esp. the OLLAMA_HOST=0.0.0.0 / no-scheme gotcha)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy.config import Settings


def _host(value):
    s = Settings()
    s.ollama_host = value
    s.__post_init__()
    return s.ollama_host


def test_adds_scheme():
    assert _host("localhost:11434") == "http://localhost:11434"


def test_rewrites_bind_address_to_loopback():
    assert _host("0.0.0.0:11434") == "http://127.0.0.1:11434"
    assert _host("http://0.0.0.0:11434") == "http://127.0.0.1:11434"


def test_leaves_good_host_alone():
    assert _host("http://127.0.0.1:11434") == "http://127.0.0.1:11434"
    assert _host("https://ollama.box.local:11434") == "https://ollama.box.local:11434"


def test_env_var_normalised(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    assert Settings().ollama_host == "http://127.0.0.1:11434"
