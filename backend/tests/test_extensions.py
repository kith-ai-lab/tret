"""The extension seam itself (tret/engine/extensions.py), independent of the
engine that calls it — ExtensionAPI's own contract: fail-open gates, hooks that
can never propagate, and a default instance that is a true no-op.

`tests/evals/test_extension_wiring.py` exercises the same seam through the real
engine; these are the properties that do not need a run at all.
"""
from __future__ import annotations

import logging
import uuid

import pytest

from tret.config import Settings
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult, get_extension_registry, load_extensions


@pytest.fixture(autouse=True)
def _reset_singleton():
    """Every test starts with no registry set, same as a fresh process."""
    extensions_module._registry = None
    yield
    extensions_module._registry = None


def _run():
    """A stand-in for a Run row: the seam never inspects it, only passes it
    through to whatever a gate or hook does with it."""
    return object()


# ── the default: no extensions loaded is a true no-op ─────────────────────────
def test_unset_extensions_setting_is_an_empty_list():
    assert Settings().extensions == []


async def test_with_no_extensions_loaded_the_registry_allows_everything():
    """get_extension_registry() before load_extensions has ever run — the state
    of a plain `TRET_EXTENSIONS` unset deployment."""
    reg = get_extension_registry()
    result = await reg.check_pre_run(None, _run(), uuid.uuid4())
    assert result == GateResult(allowed=True)
    # No hook to run, so this must not raise even with nothing wired.
    await reg.run_post_run_hooks(None, _run(), uuid.uuid4())
    await reg.run_startup_tasks()


async def test_load_extensions_with_an_empty_list_is_also_a_no_op():
    ext = load_extensions(app=None, module_names=[])
    result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    assert result.allowed is True


def test_get_extension_registry_is_a_singleton_like_get_event_bus():
    first = get_extension_registry()
    second = get_extension_registry()
    assert first is second


def test_load_extensions_sets_the_singleton():
    ext = load_extensions(app=None, module_names=[])
    assert get_extension_registry() is ext


# ── pre-run gates ───────────────────────────────────────────────────────────
async def test_an_explicit_veto_is_returned_verbatim():
    ext = ExtensionAPI(None)

    async def veto(db, run, workspace_id):
        return GateResult(allowed=False, reason="insufficient_credits", detail="balance is $0")

    ext.add_pre_run_gate(veto)
    result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    assert result.allowed is False
    assert result.reason == "insufficient_credits"
    assert result.detail == "balance is $0"


async def test_a_gate_that_allows_falls_through_to_the_next_gate():
    ext = ExtensionAPI(None)
    seen = []

    async def allows(db, run, workspace_id):
        seen.append("first")
        return GateResult(allowed=True)

    async def vetoes(db, run, workspace_id):
        seen.append("second")
        return GateResult(allowed=False, reason="second_gate")

    ext.add_pre_run_gate(allows)
    ext.add_pre_run_gate(vetoes)
    result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    assert result.reason == "second_gate"
    assert seen == ["first", "second"]


async def test_a_veto_short_circuits_later_gates():
    ext = ExtensionAPI(None)
    called = []

    async def vetoes(db, run, workspace_id):
        return GateResult(allowed=False, reason="first_gate")

    async def never_called(db, run, workspace_id):
        called.append(True)
        return GateResult(allowed=True)

    ext.add_pre_run_gate(vetoes)
    ext.add_pre_run_gate(never_called)
    result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    assert result.reason == "first_gate"
    assert called == []


async def test_a_raising_gate_fails_open_and_later_gates_still_run(caplog):
    ext = ExtensionAPI(None)

    async def raises(db, run, workspace_id):
        raise RuntimeError("boom")

    async def vetoes_after(db, run, workspace_id):
        return GateResult(allowed=False, reason="downstream_gate")

    ext.add_pre_run_gate(raises)
    ext.add_pre_run_gate(vetoes_after)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    # The broken gate did not veto (it never returned), and did not stop the
    # engine from asking the next one.
    assert result.reason == "downstream_gate"
    assert "raised" in caplog.text


async def test_no_gates_at_all_allows_the_run():
    ext = ExtensionAPI(None)
    result = await ext.check_pre_run(None, _run(), uuid.uuid4())
    assert result == GateResult(allowed=True)


# ── post-run hooks ────────────────────────────────────────────────────────────
async def test_every_hook_runs_even_if_an_earlier_one_raises():
    ext = ExtensionAPI(None)
    called = []

    async def raises(db, run, workspace_id):
        called.append("first")
        raise RuntimeError("boom")

    async def records(db, run, workspace_id):
        called.append("second")

    ext.add_post_run_hook(raises)
    ext.add_post_run_hook(records)
    await ext.run_post_run_hooks(None, _run(), uuid.uuid4())
    assert called == ["first", "second"]


async def test_a_hook_exception_never_propagates(caplog):
    ext = ExtensionAPI(None)

    async def raises(db, run, workspace_id):
        raise RuntimeError("boom")

    ext.add_post_run_hook(raises)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        await ext.run_post_run_hooks(None, _run(), uuid.uuid4())  # must not raise
    assert "raised" in caplog.text


# ── workspace gates ──────────────────────────────────────────────────────────
async def test_with_no_workspace_gates_registered_the_action_is_allowed():
    reg = get_extension_registry()
    result = await reg.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result == GateResult(allowed=True)


async def test_no_workspace_gates_at_all_allows_the_action():
    ext = ExtensionAPI(None)
    result = await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result == GateResult(allowed=True)


async def test_a_workspace_gate_explicit_veto_is_returned_verbatim():
    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "invite"
        return GateResult(allowed=False, reason="seat_limit", detail="no seats left")

    ext.add_workspace_gate(veto)
    result = await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result.allowed is False
    assert result.reason == "seat_limit"
    assert result.detail == "no seats left"


async def test_a_workspace_gate_that_allows_falls_through_to_the_next_gate():
    ext = ExtensionAPI(None)
    seen = []

    async def allows(db, workspace_id, action):
        seen.append("first")
        return GateResult(allowed=True)

    async def vetoes(db, workspace_id, action):
        seen.append("second")
        return GateResult(allowed=False, reason="second_gate")

    ext.add_workspace_gate(allows)
    ext.add_workspace_gate(vetoes)
    result = await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result.reason == "second_gate"
    assert seen == ["first", "second"]


async def test_a_workspace_gate_veto_short_circuits_later_gates():
    ext = ExtensionAPI(None)
    called = []

    async def vetoes(db, workspace_id, action):
        return GateResult(allowed=False, reason="first_gate")

    async def never_called(db, workspace_id, action):
        called.append(True)
        return GateResult(allowed=True)

    ext.add_workspace_gate(vetoes)
    ext.add_workspace_gate(never_called)
    result = await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result.reason == "first_gate"
    assert called == []


async def test_a_raising_workspace_gate_fails_open_and_later_gates_still_run(caplog):
    ext = ExtensionAPI(None)

    async def raises(db, workspace_id, action):
        raise RuntimeError("boom")

    async def vetoes_after(db, workspace_id, action):
        return GateResult(allowed=False, reason="downstream_gate")

    ext.add_workspace_gate(raises)
    ext.add_workspace_gate(vetoes_after)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        result = await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    assert result.reason == "downstream_gate"
    assert "raised" in caplog.text


async def test_workspace_gate_receives_the_action_string():
    ext = ExtensionAPI(None)
    seen_actions = []

    async def records(db, workspace_id, action):
        seen_actions.append(action)
        return GateResult(allowed=True)

    ext.add_workspace_gate(records)
    await ext.check_workspace_gate(None, uuid.uuid4(), "invite")
    await ext.check_workspace_gate(None, uuid.uuid4(), "invite_redeem")
    assert seen_actions == ["invite", "invite_redeem"]


# ── startup tasks ──────────────────────────────────────────────────────────────
async def test_startup_tasks_run_in_registration_order():
    ext = ExtensionAPI(None)
    order = []

    async def first():
        order.append(1)

    async def second():
        order.append(2)

    ext.add_startup_task(first)
    ext.add_startup_task(second)
    await ext.run_startup_tasks()
    assert order == [1, 2]


# ── include_router ──────────────────────────────────────────────────────────────
def test_include_router_is_a_no_op_with_no_app():
    """load_extensions(app=None, ...) is what the pure-logic tests above use;
    an extension calling include_router against it must not raise."""
    ext = ExtensionAPI(None)
    from fastapi import APIRouter

    ext.include_router(APIRouter())  # must not raise


def test_include_router_mounts_onto_the_real_app():
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    router = APIRouter(prefix="/api/ext")

    @router.get("/ping")
    async def ping():
        return {"ok": True}

    ext = ExtensionAPI(app)
    ext.include_router(router)
    response = TestClient(app).get("/api/ext/ping")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


# ── load_extensions imports the named module and calls register(ext) ──────────
def test_load_extensions_imports_and_registers(tmp_path, monkeypatch):
    import sys

    module_dir = tmp_path
    (module_dir / "a_probe_extension.py").write_text(
        "registered_with = []\n"
        "def register(ext):\n"
        "    registered_with.append(ext)\n"
    )
    monkeypatch.syspath_prepend(str(module_dir))
    ext = load_extensions(app=None, module_names=["a_probe_extension"])
    probe = sys.modules["a_probe_extension"]
    assert probe.registered_with == [ext]
