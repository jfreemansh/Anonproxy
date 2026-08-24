"""Empty text content blocks must never reach upstream — Anthropic 400s them
('text content blocks must be non-empty'), and a poisoned client history
would otherwise replay them in every request forever."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from anonproxy import Engine
from anonproxy.config import Settings
from anonproxy.proxy.transform import anonymize_anthropic_request


def _engine():
    s = Settings()
    s.ephemeral = True
    s.detectors = ["regex"]
    return Engine(settings=s)


def _msgs(engine, messages):
    out = anonymize_anthropic_request(engine, {"model": "m", "messages": messages})
    return out["messages"]


def test_empty_text_block_dropped_from_mixed_message():
    msgs = _msgs(_engine(), [{"role": "assistant", "content": [
        {"type": "text", "text": ""},
        {"type": "text", "text": "real content"},
    ]}])
    blocks = msgs[0]["content"]
    assert len(blocks) == 1 and blocks[0]["text"] == "real content"


def test_message_of_only_empty_blocks_gets_placeholder():
    msgs = _msgs(_engine(), [{"role": "assistant", "content": [
        {"type": "text", "text": ""}, {"type": "text", "text": "  "},
    ]}])
    blocks = msgs[0]["content"]
    assert len(blocks) == 1 and blocks[0]["text"] == " "


def test_tool_result_empty_inner_content_repaired():
    msgs = _msgs(_engine(), [{"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "t1",
         "content": [{"type": "text", "text": ""}]},
    ]}])
    block = msgs[0]["content"][0]
    assert block["content"] == " "


def test_whitespace_only_string_content_repaired():
    msgs = _msgs(_engine(), [{"role": "user", "content": "   "}])
    assert msgs[0]["content"] == " "


def test_normal_content_untouched():
    msgs = _msgs(_engine(), [{"role": "user", "content": [
        {"type": "text", "text": "host 10.20.0.10 up"},
        {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"c": "ls"}},
    ]}])
    blocks = msgs[0]["content"]
    assert len(blocks) == 2
    assert "10.20.0.10" not in blocks[0]["text"]      # anonymization still ran
    assert blocks[1]["type"] == "tool_use"
