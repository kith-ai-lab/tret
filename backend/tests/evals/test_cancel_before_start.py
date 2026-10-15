"""A run whose cancel flag is set before `execute()` reaches it (a delegated
child that sat in the slot queue while its user pressed Stop) ends `cancelled`
without a model call, and a run whose row is already terminal is not executed
at all (its late slot must not resurrect it).

Real engine and database via the `world` fixture, same as the other golden-run
tests in this directory.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

from golden_world import _replay_registry
from replay_provider import ReplayProvider, ScriptedTurn

from tret.db.models import Harness, Run
from tret.engine.harness import HarnessEngine
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.router_llm.priors import NoPriors

MODEL_ID = "test/only-model"


def _catalog() -> ModelCatalog:
    info = ModelInfo(
        id=MODEL_ID,
        provider="anthropic",
        wire_id="only",
        display_name="only",
        context_window=200_000,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("1"),
        cost_tier="economy",
    )
    catalog = ModelCatalog()
    catalog._static = {info.id: info}
    return catalog


async def _make_run(world, *, status: str) -> object:
    async with world.session_factory() as db:
        harness = Harness(
            workspace_id=world.workspace_id,
            name="Cancel Harness",
            task_profile="freeform",
            model_policy={"mode": "pinned", "model": MODEL_ID},
            tool_names=[],
            loop_config={"max_iterations": 2, "max_output_tokens": 200, "temperature": 0.0},
            created_by=world.user_id,
        )
        db.add(harness)
        await db.flush()
        run = Run(
            project_id=world.project_id,
            harness_id=harness.id,
            task_type="chat",
            task_input={"message": "hi"},
            status=status,
            created_by=world.user_id,
        )
        db.add(run)
        await db.commit()
        return run.id


async def _execute(world, engine, run_id, provider):
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)
    return await world.read_back(run_id, provider=provider)


async def test_flagged_run_that_enters_execute_stops_before_any_model_call(world):
    run_id = await _make_run(world, status="queued")
    provider = ReplayProvider([ScriptedTurn(text="should never be produced")])
    engine = HarnessEngine(catalog=_catalog(), priors=NoPriors())
    engine.cancel(run_id)

    result = await _execute(world, engine, run_id, provider)

    assert result.run.status == "cancelled"
    assert provider.calls == []
    assert run_id not in engine._cancelled  # released by execute()


async def test_terminal_run_is_not_executed_even_without_a_cancel_flag(world):
    run_id = await _make_run(world, status="failed")
    provider = ReplayProvider([ScriptedTurn(text="should never be produced")])
    engine = HarnessEngine(catalog=_catalog(), priors=NoPriors())
    engine.cancel(run_id)  # stale flag from the endpoint's marked path

    # No `read_back`: it waits on the bus for a terminal event, and a run that
    # never executed publishes none.
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)

    async with world.session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "failed"
    assert provider.calls == []
    assert run_id not in engine._cancelled
