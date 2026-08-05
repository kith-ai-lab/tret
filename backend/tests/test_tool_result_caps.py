"""Tool-result caps: the byte/row ceilings and the last-resort backstop.

Row-level truncation and its effect on what may be cited is covered end to end
in tests/evals/test_token_economy.py; these are the unit-level edges.
"""
from __future__ import annotations

import json

from bench.engine import tools as tools_module
from bench.engine.tools import (
    MAX_RESULT_BYTES,
    RESULT_MARKER_SLACK_BYTES,
    _cap_result_text,
    _cap_rows,
    execute_tool,
)
from bench.providers.base import ToolSpec


def _rows(n: int, width: int = 10) -> list[dict]:
    return [{"i": i, "pad": "x" * width} for i in range(n)]


def test_rows_are_capped_by_count(monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_RESULT_ROWS", 5)
    assert len(_cap_rows(_rows(100))) == 5
    assert _cap_rows(_rows(3)) == _rows(3)  # under the cap: untouched


def test_rows_are_capped_by_serialized_size(monkeypatch):
    monkeypatch.setattr(tools_module, "MAX_RESULT_ROWS", 1000)
    monkeypatch.setattr(tools_module, "MAX_RESULT_BYTES", 2_000)
    kept = _cap_rows(_rows(500, width=100))
    assert 0 < len(kept) < 500
    assert len(json.dumps(kept).encode()) <= 2_000


def test_a_single_huge_row_is_never_dropped_to_nothing(monkeypatch):
    """One row over the byte cap still comes back — the backstop trims it."""
    monkeypatch.setattr(tools_module, "MAX_RESULT_BYTES", 100)
    assert len(_cap_rows(_rows(1, width=10_000))) == 1


def test_result_text_backstop_cuts_and_says_so():
    ceiling = MAX_RESULT_BYTES + RESULT_MARKER_SLACK_BYTES
    text = _cap_result_text("y" * (ceiling * 2))
    assert "[TRUNCATED:" in text
    assert "may not be cited" in text
    assert len(text.encode()) < ceiling + 500


def test_result_text_under_the_backstop_is_returned_verbatim():
    text = "a normal tool result"
    assert _cap_result_text(text) == text


async def test_execute_tool_applies_the_backstop_to_any_tool():
    async def flood(ctx, **kw):
        return "z" * (MAX_RESULT_BYTES * 3)

    spec = ToolSpec(name="flood", description="", parameters={}, handler=flood)
    result, is_error = await execute_tool(None, spec, {})
    assert is_error is False
    assert "[TRUNCATED:" in result
    assert len(result.encode()) < MAX_RESULT_BYTES * 3
