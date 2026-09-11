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


# ── OAuth client providers (workspace connections) ──────────────────────────────
def test_with_no_oauth_client_providers_registered_returns_none():
    ext = ExtensionAPI(None)
    assert ext.get_oauth_client_config("gdrive") is None


def test_the_first_non_none_provider_wins():
    from tret.services.connections import OAuthClientConfig

    ext = ExtensionAPI(None)

    def declines(provider):
        return None

    def provides(provider):
        return OAuthClientConfig(client_id="ext-id", client_secret="ext-secret")

    ext.add_oauth_client_provider(declines)
    ext.add_oauth_client_provider(provides)
    assert ext.get_oauth_client_config("gdrive") == OAuthClientConfig(
        client_id="ext-id", client_secret="ext-secret"
    )


def test_a_later_provider_is_never_asked_once_an_earlier_one_answers():
    from tret.services.connections import OAuthClientConfig

    ext = ExtensionAPI(None)
    calls = []

    def first(provider):
        return OAuthClientConfig(client_id="first-id", client_secret="first-secret")

    def second(provider):
        calls.append(provider)
        return OAuthClientConfig(client_id="second-id", client_secret="second-secret")

    ext.add_oauth_client_provider(first)
    ext.add_oauth_client_provider(second)
    result = ext.get_oauth_client_config("m365")
    assert result.client_id == "first-id"
    assert calls == []


def test_a_raising_oauth_client_provider_fails_open_and_later_providers_still_run(caplog):
    from tret.services.connections import OAuthClientConfig

    ext = ExtensionAPI(None)

    def raises(provider):
        raise RuntimeError("boom")

    def provides_after(provider):
        return OAuthClientConfig(client_id="after-id", client_secret="after-secret")

    ext.add_oauth_client_provider(raises)
    ext.add_oauth_client_provider(provides_after)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        result = ext.get_oauth_client_config("gdrive")
    assert result.client_id == "after-id"
    assert "raised" in caplog.text


def test_oauth_client_provider_receives_the_provider_string():
    ext = ExtensionAPI(None)
    seen = []

    def records(provider):
        seen.append(provider)
        return None

    ext.add_oauth_client_provider(records)
    ext.get_oauth_client_config("gdrive")
    ext.get_oauth_client_config("m365")
    assert seen == ["gdrive", "m365"]


# ── factor layer providers (managed emissions overrides) ────────────────────
async def test_with_no_factor_layer_providers_registered_returns_none_and_opens_no_session(
    monkeypatch,
):
    import tret.db.engine as db_engine_module

    def _must_not_be_called():
        raise AssertionError("must not open a session when nothing is registered")

    monkeypatch.setattr(db_engine_module, "get_session_factory", _must_not_be_called)
    ext = ExtensionAPI(None)
    result = await ext.get_factor_layer(uuid.uuid4())
    assert result is None


async def test_the_first_non_none_factor_layer_provider_wins():
    ext = ExtensionAPI(None)

    async def declines(db, workspace_id):
        return None

    async def provides(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 100, "label": "managed"}}}

    ext.add_factor_layer_provider(declines)
    ext.add_factor_layer_provider(provides)
    result = await ext.get_factor_layer(uuid.uuid4())
    assert result == {"grid": {"default": {"g_per_kwh": 100, "label": "managed"}}}


async def test_a_later_factor_layer_provider_is_never_asked_once_an_earlier_one_answers():
    ext = ExtensionAPI(None)
    calls = []

    async def first(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 50, "label": "first"}}}

    async def second(db, workspace_id):
        calls.append(workspace_id)
        return {"grid": {"default": {"g_per_kwh": 60, "label": "second"}}}

    ext.add_factor_layer_provider(first)
    ext.add_factor_layer_provider(second)
    result = await ext.get_factor_layer(uuid.uuid4())
    assert result["grid"]["default"]["g_per_kwh"] == 50
    assert calls == []


async def test_a_raising_factor_layer_provider_fails_open_and_later_providers_still_run(caplog):
    ext = ExtensionAPI(None)

    async def raises(db, workspace_id):
        raise RuntimeError("boom")

    async def provides_after(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 70, "label": "after"}}}

    ext.add_factor_layer_provider(raises)
    ext.add_factor_layer_provider(provides_after)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        result = await ext.get_factor_layer(uuid.uuid4())
    assert result["grid"]["default"]["g_per_kwh"] == 70
    assert "raised" in caplog.text


async def test_an_invalid_document_is_treated_as_none_and_logs_a_warning(caplog):
    """Missing the label `GridBlock` requires whenever `g_per_kwh` is set —
    `EmissionsOverrides` rejects it, and the seam must fail open rather than
    let a broken managed document reach `build_factor_set` and break a run."""
    ext = ExtensionAPI(None)

    async def invalid(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 42}}}

    ext.add_factor_layer_provider(invalid)
    with caplog.at_level(logging.WARNING, logger="tret.extensions"):
        result = await ext.get_factor_layer(uuid.uuid4())
    assert result is None
    assert "validation" in caplog.text.lower()


async def test_an_invalid_document_falls_through_to_the_next_provider(caplog):
    ext = ExtensionAPI(None)

    async def invalid(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 42}}}  # no label -> fails validation

    async def valid_after(db, workspace_id):
        return {"grid": {"default": {"g_per_kwh": 55, "label": "fallback"}}}

    ext.add_factor_layer_provider(invalid)
    ext.add_factor_layer_provider(valid_after)
    with caplog.at_level(logging.WARNING, logger="tret.extensions"):
        result = await ext.get_factor_layer(uuid.uuid4())
    assert result == {"grid": {"default": {"g_per_kwh": 55, "label": "fallback"}}}


async def test_with_no_factor_layer_providers_at_all_returns_none():
    ext = ExtensionAPI(None)
    result = await ext.get_factor_layer(uuid.uuid4())
    assert result is None


async def test_factor_layer_provider_receives_the_workspace_id():
    ext = ExtensionAPI(None)
    seen = []
    workspace_id = uuid.uuid4()

    async def records(db, wid):
        seen.append(wid)
        return None

    ext.add_factor_layer_provider(records)
    await ext.get_factor_layer(workspace_id)
    assert seen == [workspace_id]


# ── workspace settings hooks (change history for e.g. emissions overrides) ──
async def test_with_no_workspace_settings_hooks_registered_opens_no_session(monkeypatch):
    import tret.db.engine as db_engine_module

    def _must_not_be_called():
        raise AssertionError("must not open a session when nothing is registered")

    monkeypatch.setattr(db_engine_module, "get_session_factory", _must_not_be_called)
    ext = ExtensionAPI(None)
    # Must not raise, and must not touch the database.
    await ext.run_workspace_settings_hooks(
        None, uuid.uuid4(), "emissions", {"a": 1}, {"a": 2}, uuid.uuid4()
    )


async def test_every_workspace_settings_hook_runs_even_if_an_earlier_one_raises():
    ext = ExtensionAPI(None)
    called = []

    async def raises(db, workspace_id, key, before, after, user_id):
        called.append("first")
        raise RuntimeError("boom")

    async def records(db, workspace_id, key, before, after, user_id):
        called.append("second")

    ext.add_workspace_settings_hook(raises)
    ext.add_workspace_settings_hook(records)
    await ext.run_workspace_settings_hooks(
        None, uuid.uuid4(), "emissions", None, {"a": 1}, uuid.uuid4()
    )
    assert called == ["first", "second"]


async def test_a_workspace_settings_hook_exception_never_propagates(caplog):
    ext = ExtensionAPI(None)

    async def raises(db, workspace_id, key, before, after, user_id):
        raise RuntimeError("boom")

    ext.add_workspace_settings_hook(raises)
    with caplog.at_level(logging.ERROR, logger="tret.extensions"):
        await ext.run_workspace_settings_hooks(
            None, uuid.uuid4(), "emissions", None, {"a": 1}, uuid.uuid4()
        )  # must not raise
    assert "raised" in caplog.text


async def test_workspace_settings_hook_receives_the_full_call_shape():
    ext = ExtensionAPI(None)
    seen = []
    workspace_id = uuid.uuid4()
    user_id = uuid.uuid4()
    before = {"grid": {"default": {"g_per_kwh": 42, "label": "old"}}}
    after = {"grid": {"default": {"g_per_kwh": 55, "label": "new"}}}

    async def records(db, wid, key, before_doc, after_doc, uid):
        seen.append((wid, key, before_doc, after_doc, uid))

    ext.add_workspace_settings_hook(records)
    await ext.run_workspace_settings_hooks(None, workspace_id, "emissions", before, after, user_id)
    assert seen == [(workspace_id, "emissions", before, after, user_id)]


async def test_workspace_settings_hook_sees_none_before_and_none_after():
    """A first write has no prior document; a DELETE leaves no document at
    all — both are valid, and distinct from each other and from `{}`."""
    ext = ExtensionAPI(None)
    seen = []

    async def records(db, wid, key, before, after, uid):
        seen.append((before, after))

    ext.add_workspace_settings_hook(records)
    await ext.run_workspace_settings_hooks(None, uuid.uuid4(), "emissions", None, {"a": 1}, uuid.uuid4())
    await ext.run_workspace_settings_hooks(None, uuid.uuid4(), "emissions", {"a": 1}, None, uuid.uuid4())
    assert seen == [(None, {"a": 1}), ({"a": 1}, None)]


async def test_with_no_workspace_settings_hooks_at_all_does_not_raise():
    ext = ExtensionAPI(None)
    await ext.run_workspace_settings_hooks(
        None, uuid.uuid4(), "emissions", None, None, None
    )


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


async def test_load_extensions_registers_core_budget_gate_and_hook_before_an_extension(
    tmp_path, monkeypatch,
):
    """`load_extensions` wires core's own per-workspace budget gate and alert
    hook (`services/budgets.py`) onto the registry before it ever imports a
    named extension module and calls that module's own `register(ext)` — see
    `load_extensions`'s own docstring. Faking the two budgets.py functions
    (rather than exercising the real, DB-backed ones) mirrors every other
    gate/hook test in this file; the extension side is a real imported
    module, the same `a_probe_extension.py`-on-`sys.path` pattern
    `test_load_extensions_imports_and_registers` above uses, so this proves
    the actual `load_extensions` import-and-register path, not a hand-built
    stand-in for it."""
    import sys

    from tret.services import budgets as budgets_module

    order: list[str] = []

    async def fake_core_gate(db, run, workspace_id):
        order.append("core_gate")
        return GateResult(allowed=True)

    async def fake_core_hook(db, run, workspace_id):
        order.append("core_hook")

    monkeypatch.setattr(budgets_module, "budget_pre_run_gate", fake_core_gate)
    monkeypatch.setattr(budgets_module, "budget_alert_post_run_hook", fake_core_hook)

    (tmp_path / "a_probe_extension_budget_order.py").write_text(
        "from tret.engine.extensions import GateResult\n"
        "\n"
        "async def ext_gate(db, run, workspace_id):\n"
        "    shared_order.append('ext_gate')\n"
        "    return GateResult(allowed=True)\n"
        "\n"
        "async def ext_hook(db, run, workspace_id):\n"
        "    shared_order.append('ext_hook')\n"
        "\n"
        "def register(ext):\n"
        "    ext.add_pre_run_gate(ext_gate)\n"
        "    ext.add_post_run_hook(ext_hook)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    ext = load_extensions(app=None, module_names=["a_probe_extension_budget_order"])
    # Injected after import (so `register` above never needed to know where
    # it comes from), but read by `ext_gate`/`ext_hook` at call time — a
    # plain global lookup in the probe module's own namespace, resolved when
    # those functions actually run, not when they were defined.
    sys.modules["a_probe_extension_budget_order"].shared_order = order

    await ext.check_pre_run(None, _run(), uuid.uuid4())
    await ext.run_post_run_hooks(None, _run(), uuid.uuid4())

    assert order == ["core_gate", "ext_gate", "core_hook", "ext_hook"]
