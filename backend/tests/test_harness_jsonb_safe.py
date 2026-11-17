"""Run messages are cleaned of NUL characters before they are persisted.

Postgres refuses `\\u0000` in JSONB, so a tool result carrying one (PDF text
extraction does) used to fail the save of `runs.messages` and the whole run.
"""

from tret.engine.harness import _jsonb_safe


def test_nul_characters_are_removed_from_nested_strings():
    messages = [
        {"role": "tool", "content": "page 1\x00\x00 text", "meta": {"note": "a\x00b"}},
        {"role": "assistant", "content": None, "tool_calls": [{"arguments": {"q": "x\x00"}}]},
    ]
    cleaned = _jsonb_safe(messages)
    assert cleaned == [
        {"role": "tool", "content": "page 1 text", "meta": {"note": "ab"}},
        {"role": "assistant", "content": None, "tool_calls": [{"arguments": {"q": "x"}}]},
    ]
    assert "\x00" in messages[0]["content"]  # the in-memory copy is untouched


def test_values_without_nul_are_unchanged():
    messages = [{"role": "user", "content": "plain", "n": 3, "ok": True, "f": 1.5}]
    assert _jsonb_safe(messages) == messages
