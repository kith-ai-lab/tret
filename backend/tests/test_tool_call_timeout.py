"""A single tool call can never hang a run forever (TRET_TOOL_CALL_TIMEOUT_SECONDS)."""
from __future__ import annotations

import asyncio

import pytest

from tret.config import get_settings
from tret.engine import tools as tools_mod
from tret.providers.base import ToolSpec


def _spec(name, handler):
    return ToolSpec(name=name, description="t", parameters={"type": "object", "properties": {}}, handler=handler)


@pytest.fixture
def short_timeout(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_call_timeout_seconds", 0.2, raising=False)
    monkeypatch.setattr(tools_mod, "TOOL_CANCEL_GRACE_SECONDS", 0.2)


async def test_hung_tool_returns_error_result(short_timeout):
    async def hang(ctx):
        await asyncio.Event().wait()

    text, is_error = await tools_mod.execute_tool(None, _spec("hang", hang), {})
    assert is_error and "did not finish" in text


async def test_tool_ignoring_cancellation_is_abandoned(short_timeout):
    release = asyncio.Event()

    async def stubborn(ctx):
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue  # swallows cancel, like an await stuck in a dead socket
        return "late"

    with pytest.raises(tools_mod.ToolCallAbandoned):
        await asyncio.wait_for(tools_mod.execute_tool(None, _spec("stubborn", stubborn), {}), 5)
    release.set()
    await asyncio.sleep(0.05)
    assert not tools_mod._HANDLER_TASKS


async def test_outer_cancellation_waits_for_handler_cleanup(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_call_timeout_seconds", 30, raising=False)
    started, cleaned = asyncio.Event(), asyncio.Event()

    async def handler(ctx):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0.05)  # cleanup that takes a moment
            cleaned.set()

    outer = asyncio.ensure_future(tools_mod.execute_tool(None, _spec("h", handler), {}))
    await started.wait()
    outer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert cleaned.is_set()


async def test_capture_filing_exempt(short_timeout):
    async def slow(ctx):
        await asyncio.sleep(0.5)
        return "ok"

    assert await tools_mod.execute_tool(None, _spec("capture_filing", slow), {}) == ("ok", False)


async def test_fast_tool_unaffected(short_timeout):
    async def ok(ctx):
        return "fine"

    assert await tools_mod.execute_tool(None, _spec("ok", ok), {}) == ("fine", False)


async def test_delegating_tools_exempt(short_timeout):
    async def slow(ctx):
        await asyncio.sleep(0.5)
        return "child done"

    assert await tools_mod.execute_tool(None, _spec("delegate_parallel", slow), {}) == ("child done", False)


async def test_zero_disables(monkeypatch):
    monkeypatch.setattr(get_settings(), "tool_call_timeout_seconds", 0, raising=False)

    async def ok(ctx):
        return "x"

    assert await tools_mod.execute_tool(None, _spec("ok", ok), {}) == ("x", False)
