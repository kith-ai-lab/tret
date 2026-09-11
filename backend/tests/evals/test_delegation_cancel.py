"""Cancelling a parent run must stop the child run it delegated to.

`run_harness_task` (engine/tools.py) creates a child `Run` and awaits the
engine's `execute()` on it inline, inside the parent's own tool-call step. A
child has no `parent_run_id` column, so before this fix `POST
/api/runs/{id}/cancel` on the parent flagged only the parent's own id — the
child kept looping to completion, unattended, on the operator's dime.

This drives the real engine through the real `world` fixture (same as
`tests/evals/test_engine_loop.py`'s delegation-depth suite), with one
deliberate wrinkle: `world.run()` builds its own private `HarnessEngine`
instance, but `run_harness_task` always resolves its engine via
`get_harness_engine()` — the process-wide singleton production always uses for
both a parent and its children. The two must be the *same* object for
`cancel()` to reach a child at all, so this file drives `HarnessEngine.execute`
directly (via `_engine`, monkeypatched to a test-local instance) rather than
`world.run()`, mirroring how a real deployment always executes both ends of a
delegation on the one shared engine.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from unittest.mock import patch

from golden_world import _replay_registry, build_world
from replay_provider import ProviderCall
from sqlalchemy import select

import tret.engine.harness as harness_module
from tret.db.models import Run
from tret.engine.harness import HarnessEngine
from tret.providers.base import (
    Provider,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from tret.providers.catalog import ModelCatalog
from tret.router_llm.priors import NoPriors

# A two-task pack rather than the self-delegating one `test_engine_loop.py`
# uses: parent and child are different task types here, so the shared
# provider below can tell which run it is being asked about from the tools it
# was offered, with no need for either run to delegate more than once.
CANCEL_PACK_YAML = """\
pack: cancel-test
version: 0.1.0
display_name: Cancellation Test
description: A delegator task and a specialist task, for cancellation propagation.
doctrine:
  - doctrine/01-rules.md
task_types:
  - slug: delegator_task
    display_name: Delegator task
    shape: freeform
    input_schema:
      subject_id: { type: string, description: "Anything" }
    tools: [run_harness_task]
    output_contract: Free text.
    instructions: Delegate this task to the specialist and report what came back.
  - slug: specialist_task
    display_name: Specialist task
    shape: freeform
    input_schema:
      subject_id: { type: string, description: "Anything" }
    tools: [list_prior_findings]
    output_contract: Free text.
    instructions: Look at prior findings, then report.
"""


async def _cancel_world(tmp_path):
    pack_dir = tmp_path / "cancel-pack"
    (pack_dir / "doctrine").mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(CANCEL_PACK_YAML)
    (pack_dir / "doctrine" / "01-rules.md").write_text("# Rules\n\nDelegate once.\n")
    return await build_world(tmp_path / "cancel.db", pack_dir=pack_dir)


class DelegationCancelProvider(Provider):
    """Parent delegates once; the child blocks mid-turn until the test resumes
    it, so the test can cancel the parent while the child is provably still
    running.

    Distinguishes the parent's turn from the child's by which tools the engine
    offered — `run_harness_task` only for the delegator, `list_prior_findings`
    only for the specialist — rather than by call order, since a fixed script
    cannot express "block here" (see `SelfDelegatingProvider` in
    test_engine_loop.py for the same reasoning about scripting delegation).
    """

    name = "cancel-test"

    def __init__(self, *, child_started: asyncio.Event, resume_child: asyncio.Event):
        self.calls: list[ProviderCall] = []
        self.child_started = child_started
        self.resume_child = resume_child
        self.child_stream_calls = 0

    async def stream(
        self, *, model, system, messages, tools, max_tokens, temperature, effort=None, session_id=None
    ) -> AsyncIterator[ProviderEvent]:
        offered = [t.name for t in tools]
        self.calls.append(
            ProviderCall(
                model=model,
                system=system,
                messages=list(messages),
                tool_names=offered,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        usage = Usage(input_tokens=400, output_tokens=40)

        if "run_harness_task" in offered:
            # The parent's own turn. It should get exactly one turn: it
            # delegates, and by the time run_harness_task returns, the parent
            # itself should already be cancelled (see the test) and never ask
            # the provider anything again.
            yield TextDelta("Delegating this to the specialist.")
            yield ToolCallComplete(
                ToolCall(
                    id="call-parent-1",
                    name="run_harness_task",
                    arguments={
                        "task_type": "specialist_task",
                        "task_input": {"subject_id": "anything"},
                    },
                )
            )
            yield TurnComplete(usage=usage, stop_reason="tool_use")
            return

        # The child's turn.
        self.child_stream_calls += 1
        if self.child_stream_calls > 1:
            raise AssertionError(
                "the child made a second provider call — cancellation should have "
                "stopped its loop after the first turn's tool call, before this"
            )
        self.child_started.set()
        await self.resume_child.wait()
        yield TextDelta("Checking prior findings.")
        yield ToolCallComplete(ToolCall(id="call-child-1", name="list_prior_findings", arguments={}))
        yield TurnComplete(usage=usage, stop_reason="tool_use")

    async def complete_json(self, **kwargs):  # pragma: no cover - not exercised
        raise AssertionError("complete_json should not be called in this scenario")


async def test_cancelling_the_parent_stops_a_delegated_child_run(tmp_path, monkeypatch):
    world = await _cancel_world(tmp_path)
    try:
        # The engine `run_harness_task` resolves via `get_harness_engine()` must
        # be the SAME instance whose `.execute()` drives the parent, or the
        # parent's `cancel()` and the child's `_is_cancelled()` check two
        # unrelated `_cancelled` sets — see the module docstring.
        engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
        monkeypatch.setattr(harness_module, "_engine", engine)

        child_started = asyncio.Event()
        resume_child = asyncio.Event()
        provider = DelegationCancelProvider(child_started=child_started, resume_child=resume_child)

        harness_id = await world.create_harness(tool_names=["run_harness_task"])
        parent_run_id = await world.create_run(
            harness_id=harness_id,
            task_type="delegator_task",
            task_input={"subject_id": "anything"},
        )

        with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
            task = asyncio.create_task(engine.execute(parent_run_id))
            try:
                await asyncio.wait_for(child_started.wait(), timeout=5)
                # The child is now mid-loop (blocked inside its own first
                # provider call). Cancel the PARENT, then let the child's
                # blocked call return — its own loop must catch the
                # cancellation on its next iteration and stop immediately.
                engine.cancel(parent_run_id)
                resume_child.set()
                await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()

        async with world.session_factory() as db:
            runs = (await db.execute(select(Run).order_by(Run.created_at))).scalars().all()

        assert len(runs) == 2, "expected exactly one delegated child run"
        parent = next(r for r in runs if r.id == parent_run_id)
        child = next(r for r in runs if r.id != parent_run_id)

        assert parent.status == "cancelled"
        assert child.status == "cancelled"
        # Promptly: the child never got a second provider call in after being
        # resumed — its own loop caught the cancellation at the top of the
        # very next iteration, per DelegationCancelProvider's own assertion.
        assert provider.child_stream_calls == 1
        # No lineage left dangling once the child is done: `run_harness_task`
        # unregisters it in a `finally`, regardless of how it finished.
        assert child.id not in engine._parent_of
    finally:
        await world.aclose()


async def test_cancelling_only_the_child_leaves_the_parent_running(tmp_path, monkeypatch):
    """The other direction: a child's own cancellation is not the parent's
    business. The parent keeps going and reports what the (cancelled) child
    returned, same as it would for any other non-`completed` delegation
    result."""
    world = await _cancel_world(tmp_path)
    try:
        engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
        monkeypatch.setattr(harness_module, "_engine", engine)

        child_started = asyncio.Event()
        resume_child = asyncio.Event()
        provider = DelegationCancelProvider(child_started=child_started, resume_child=resume_child)

        harness_id = await world.create_harness(tool_names=["run_harness_task"])
        parent_run_id = await world.create_run(
            harness_id=harness_id,
            task_type="delegator_task",
            task_input={"subject_id": "anything"},
        )

        with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
            task = asyncio.create_task(engine.execute(parent_run_id))
            try:
                await asyncio.wait_for(child_started.wait(), timeout=5)

                async with world.session_factory() as db:
                    child_id = (
                        await db.execute(select(Run.id).where(Run.id != parent_run_id))
                    ).scalar_one()

                # Cancel only the CHILD.
                engine.cancel(child_id)
                assert engine._is_cancelled(parent_run_id) is False

                resume_child.set()
                await asyncio.wait_for(task, timeout=5)
            finally:
                if not task.done():
                    task.cancel()

        async with world.session_factory() as db:
            parent = await db.get(Run, parent_run_id)
            child = await db.get(Run, child_id)

        assert child.status == "cancelled"
        # The parent's own flag was never set, so its loop ran to its natural
        # end rather than stopping — it is free to report the cancelled
        # child's result however `run_harness_task` describes it.
        assert parent.status != "cancelled"
    finally:
        await world.aclose()
