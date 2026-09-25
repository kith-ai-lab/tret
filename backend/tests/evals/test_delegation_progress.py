"""`delegation_progress` heartbeats on the parent's bus.

`delegation_started` / `delegation_finished` (engine/tools.py) bracket a
child's whole lifetime but say nothing about what happens in between — a
child that runs for minutes shows only "running" on the parent's SSE stream.
`HarnessEngine._publish_delegation_progress` (engine/harness.py) fills that
gap: one event at the top of every iteration and one per tool call, published
on `run.parent_run_id`'s bus, never the child's own. This drives the real
engine (mirroring
`test_subagent_tools_offered.test_a_delegated_subagent_with_no_grant_is_offered_nothing`'s
pattern: a child `Run` row with `parent_run_id` set by hand, executed directly
through a fresh `HarnessEngine` with a `ReplayProvider`) and reads the
progress events straight off the parent id's bus.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

from golden_world import NoPriors, _replay_registry
from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn

from tret.db.models import Run
from tret.engine.events import get_event_bus
from tret.engine.harness import HarnessEngine
from tret.providers.catalog import ModelCatalog


async def _drain(run_id, timeout: float = 0.2) -> list:
    """Collect whatever is already sitting on `run_id`'s bus and stop.

    A plain `[e async for e in bus.subscribe(run_id)]` never terminates for a
    run that has no terminal (`done`/`error`) event on its bus — which a
    *parent*'s bus never gets from a child's progress events alone, since the
    child publishes its own `done`/`error` on its own bus, not the parent's.
    `subscribe` falls back to a 30s-timeout ping loop once the backlog is
    exhausted, so this drains the backlog (already fully published, since the
    child run has already finished by the time the test subscribes) and bails
    on the first gap rather than waiting out a real ping cycle.
    """
    bus = get_event_bus()
    events = []
    gen = bus.subscribe(run_id)
    try:
        while True:
            try:
                events.append(await asyncio.wait_for(gen.__anext__(), timeout=timeout))
            except (asyncio.TimeoutError, StopAsyncIteration):
                break
    finally:
        await gen.aclose()
    return events


async def test_a_delegated_childs_progress_lands_on_the_parents_bus(world):
    harness_id = await world.create_harness(tool_names=["lookup_dataset"])
    parent_id = await world.create_run(
        harness_id=harness_id, task_type="freeform", task_input={"message": "parent"}
    )
    child_id = await world.create_run(
        harness_id=harness_id, task_type="freeform", task_input={"message": "child"}
    )
    async with world.session_factory() as db:
        child = await db.get(Run, child_id)
        child.parent_run_id = parent_id
        child.root_run_id = parent_id
        child.delegation_kind = "task"
        await db.commit()

    # Two turns: one tool call, then the answer — so both publish sites (the
    # top-of-iteration heartbeat and the per-tool-call one) are exercised.
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Looking it up.",
                tool_calls=[ScriptedCall("lookup_dataset", {"dataset": "sites", "filters": {}})],
            ),
            ScriptedTurn(text="Nothing to report."),
        ]
    )
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(child_id)

    assert provider.violations == []

    parent_events = await _drain(parent_id)
    progress = [e for e in parent_events if e.type == "delegation_progress"]
    assert progress, "expected at least one delegation_progress event on the parent's bus"
    assert any(
        e.data.get("child_run_id") == str(child_id) and e.data.get("iteration") == 1
        for e in progress
    )
    # The per-tool-call heartbeat names the tool; iteration 2's does not.
    assert any(e.data.get("tool") == "lookup_dataset" and e.data.get("iteration") == 1 for e in progress)
    assert any(e.data.get("tool") is None and e.data.get("iteration") == 2 for e in progress)

    # None of it leaked onto the child's own bus — only the parent's.
    child_events = await _drain(child_id)
    assert not [e for e in child_events if e.type == "delegation_progress"]


async def test_a_root_run_publishes_no_delegation_progress_anywhere(world):
    harness_id = await world.create_harness(tool_names=[])
    root_id = await world.create_run(
        harness_id=harness_id, task_type="freeform", task_input={"message": "root"}
    )
    async with world.session_factory() as db:
        run = await db.get(Run, root_id)
        assert run.parent_run_id is None  # sanity: this is genuinely a root run

    provider = ReplayProvider([ScriptedTurn(text="All done.")])
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(root_id)

    assert provider.violations == []

    root_events = [
        e async for e in get_event_bus().subscribe(root_id)
    ]  # safe to fully drain: the root's own bus gets a terminal `done`/`error`.
    assert not [e for e in root_events if e.type == "delegation_progress"]
