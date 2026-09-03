"""The extension seam (tret/engine/extensions.py), driven through the real
engine rather than mocked — the same `world` fixture the other golden-run
suites use, with `fake_extension.py` standing in for the proprietary billing
package this seam exists for.

Three properties matter and each is a way a broken wiring would quietly cost
someone money or hide a bug:

1. A pre-run gate's veto refuses the run before the first token — no provider
   call, no spend — and the refusal reason ends up in `run.error`.
2. A post-run hook sees the run only after its final cost is durable, so an
   extension metering spend against a balance is never shown a partial number.
3. A broken extension (a gate or hook that raises) cannot take a run down with
   it — the run's own status and error are exactly what they would have been
   with no extension loaded at all.
"""
from __future__ import annotations

import fake_extension
from replay_provider import ReplayProvider, ScriptedTurn

import tret.engine.extensions as extensions_module
from tret.engine.extensions import load_extensions


def setup_function() -> None:
    """Every test starts with a clean singleton and a clean fake extension —
    `load_extensions` sets the process-wide registry, so a test that forgot to
    reset it would leak its gate/hook into the next one."""
    fake_extension.reset()
    extensions_module._registry = None


def teardown_function() -> None:
    fake_extension.reset()
    extensions_module._registry = None


# ── (a) a gate's veto refuses the run before it spends anything ───────────────
async def test_a_gate_veto_fails_the_run_before_the_first_token(world):
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(
        veto=True, reason="insufficient_credits", detail="workspace balance is $0.00"
    )

    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert result.run.error == "insufficient_credits: workspace balance is $0.00"
    # Refused before the first token: no model call, no cost, no transcript.
    assert result.provider.turns_played == 0
    assert result.run.messages == []
    assert result.run.cost_usd == 0
    assert result.event_types == ["error"]


async def test_a_gate_veto_with_no_detail_uses_the_reason_alone(world):
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(veto=True, reason="workspace_suspended", detail=None)

    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert result.run.error == "workspace_suspended"
    assert result.provider.turns_played == 0


async def test_a_gate_veto_never_fires_the_post_run_hook(world):
    """A run the gate itself refused never got a hold placed on it, so there is
    nothing for a post-run hook to release — firing it here would be a
    spurious call recording a run that never actually started, not a safety
    net for one that did."""
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(
        veto=True, reason="insufficient_credits", detail="workspace balance is $0.00"
    )

    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert fake_extension.state["seen_runs"] == []


# ── a fail-before-start reached AFTER the gate still fires the hook ───────────
async def test_a_fail_before_start_after_the_gate_still_fires_the_post_run_hook(world):
    """Unlike the gate's own veto above, every OTHER fail-before-start path
    (unknown task type, unknown tool, no route) is only reached once
    `check_pre_run` has already allowed the run — a billing extension's gate
    may have placed a hold by then, and its post-run hook is the only thing
    that releases it. Refusing an undeclared task type is the cheapest way to
    reach one of these paths without a live route.
    """
    load_extensions(None, ["fake_extension"])

    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        task_type="a_task_type_this_pack_never_declared",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert "unknown_task_type" in result.run.error
    assert result.provider.turns_played == 0  # refused before the first token
    assert len(fake_extension.state["seen_runs"]) == 1
    seen_id, seen_status, _seen_cost = fake_extension.state["seen_runs"][0]
    assert seen_id == result.run.id
    assert seen_status == "failed"


# ── (b) a post-run hook sees the run with its final cost ──────────────────────
async def test_a_post_run_hook_sees_the_completed_run_with_its_final_cost(world):
    load_extensions(None, ["fake_extension"])

    provider = ReplayProvider([ScriptedTurn(text="Here is what I can say without tools.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise what you know."},
    )

    assert result.run.status == "completed", result.run.error
    assert len(fake_extension.state["seen_runs"]) == 1
    seen_id, seen_status, seen_cost = fake_extension.state["seen_runs"][0]
    assert seen_id == result.run.id
    assert seen_status == "completed"
    # The run's own final cost, not zero and not a partial figure from before
    # the last turn — the hook runs only after that is committed.
    assert seen_cost == result.run.cost_usd
    assert seen_cost > 0


async def test_a_post_run_hook_still_runs_after_a_midstream_provider_crash(world):
    """A crashed run may have already accumulated real cost — the hook that
    meters spend against a balance must see it even off the normal finish path."""
    load_extensions(None, ["fake_extension"])

    provider = ReplayProvider(
        [ScriptedTurn(text="Partial answer", provider_error="503 upstream connection reset")]
    )
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert len(fake_extension.state["seen_runs"]) == 1
    seen_id, seen_status, _seen_cost = fake_extension.state["seen_runs"][0]
    assert seen_id == result.run.id
    assert seen_status == "failed"


# ── (c) a broken extension cannot change the run's own outcome ────────────────
async def test_a_raising_gate_fails_open_and_the_run_proceeds_normally(world):
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(gate_raises=True)

    provider = ReplayProvider([ScriptedTurn(text="Here is what I can say without tools.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise what you know."},
    )

    assert result.run.status == "completed", result.run.error
    assert result.provider.turns_played == 1


async def test_a_gate_that_fails_a_real_db_statement_fails_open_and_the_engine_session_is_unharmed(
    world,
):
    """The isolation this whole seam exists for: a real DBAPIError from a gate,
    not just a plain Python bug.

    A gate and the engine used to share one AsyncSession, so a failed statement
    from the gate could leave that session unusable for everything the engine
    still needed to do with it (worst on Postgres, where a failed statement
    aborts the whole transaction). The fix hands gates a session of their own,
    so the run's own persistence — which happens on the engine's session, after
    this gate has already run — is completely unaffected by the gate's broken
    query.
    """
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(gate_db_error=True)

    provider = ReplayProvider([ScriptedTurn(text="Here is what I can say without tools.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise what you know."},
    )

    assert result.run.status == "completed", result.run.error
    assert result.provider.turns_played == 1
    # The run's normal persistence went through fine on the engine's own
    # session — cost, messages, everything — proving that session was never
    # touched by the gate's failed statement.
    assert result.run.cost_usd > 0
    assert result.run.messages


async def test_a_raising_hook_does_not_change_run_status_or_error(world):
    load_extensions(None, ["fake_extension"])
    fake_extension.state.update(hook_raises=True)

    provider = ReplayProvider([ScriptedTurn(text="Here is what I can say without tools.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise what you know."},
    )

    # Exactly what a run with no extension loaded would report: the hook's own
    # exception is caught and logged, never surfaced as the run's outcome.
    assert result.run.status == "completed"
    assert result.run.error is None
    # The hook did run (and did raise) — it just never got to record anything.
    assert fake_extension.state["seen_runs"] == []
