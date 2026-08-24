"""A fake extension for exercising the seam in tret/engine/extensions.py.

Wired exactly the way the real proprietary billing package will be: an
ordinary module, imported by dotted name, with a synchronous `register(ext)`.
Nothing in `tret/` imports this — only `load_extensions("fake_extension")`
does, from tests.

`state` is a plain module-level dict rather than an object built per test,
because `load_extensions` calls `register(ext)` fresh every time it runs but
the *behaviour* a test wants (veto on, a hook that raises, ...) has to be set
before that call. Tests set `state` directly, then call `load_extensions`.
"""
from __future__ import annotations

from tret.engine.extensions import ExtensionAPI, GateResult

state = {
    "veto": False,
    "reason": "insufficient_credits",
    "detail": "workspace balance is $0.00",
    "gate_raises": False,
    # Unlike gate_raises (a plain bug in extension code), this exercises a real
    # failed statement against the `db` the gate is handed — the shape of a
    # cloud gate reading a billing table that isn't there on this deployment.
    "gate_db_error": False,
    "hook_raises": False,
    # (run_id, status, cost_usd) appended by the hook, in call order — what a
    # test reads back to assert the hook saw the run in its final state.
    "seen_runs": [],
}


def reset() -> None:
    state.update(
        veto=False,
        reason="insufficient_credits",
        detail="workspace balance is $0.00",
        gate_raises=False,
        gate_db_error=False,
        hook_raises=False,
        seen_runs=[],
    )


async def _gate(db, run, workspace_id) -> GateResult:
    if state["gate_raises"]:
        raise RuntimeError("fake_extension: gate misbehaving")
    if state["gate_db_error"]:
        # A genuine DBAPIError, not a plain Python bug: this is what a gate
        # touching a table the engine's own deployment doesn't have looks
        # like. `db` here is whatever the seam handed the gate — the whole
        # point of the isolation is that this cannot reach the engine's own
        # session.
        from sqlalchemy import text

        await db.execute(text("SELECT * FROM a_table_that_does_not_exist_at_all"))
    if state["veto"]:
        return GateResult(allowed=False, reason=state["reason"], detail=state["detail"])
    return GateResult(allowed=True)


async def _hook(db, run, workspace_id) -> None:
    if state["hook_raises"]:
        raise RuntimeError("fake_extension: hook misbehaving")
    state["seen_runs"].append((run.id, run.status, run.cost_usd))


def register(ext: ExtensionAPI) -> None:
    ext.add_pre_run_gate(_gate)
    ext.add_post_run_hook(_hook)
