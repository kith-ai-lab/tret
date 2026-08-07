"""Golden runs for the loop's own guardrails, as opposed to a task's output.

`test_golden_runs.py` locks in what a *successful* run produces.  These lock in
what the engine does when a run goes wrong in a way the model cannot fix:

* a provider that dies mid-stream still leaves the turn it was in on the record;
* the iteration ceiling does not throw away a verdict that was already recorded;
* a task type nobody declared is refused instead of quietly run as freeform;
* delegation is bounded, so a pack task that can delegate cannot start an
  unbounded chain of runs.

Each drives the real engine through the real world fixture; only the model and
(for the ceiling scenario) the harness's own limits are scripted.
"""
from __future__ import annotations

import pytest
from golden_world import build_world
from replay_provider import ProviderCall, ReplayProvider, ScriptedCall, ScriptedTurn
from sqlalchemy import select
from test_golden_runs import PERIL, SITE, divergence_happy_script

from bench.db.models import Run
from bench.engine.tools import MAX_DELEGATION_DEPTH
from bench.providers.base import TextDelta, ToolCall, ToolCallComplete, TurnComplete, Usage


def _lookup(dataset: str, **filters) -> ScriptedCall:
    return ScriptedCall("lookup_dataset", {"dataset": dataset, "filters": filters})


# ── (a) a provider failure keeps the turn it happened in ──────────────────────
PARTIAL_TEXT = (
    "The vendor score is stale, so the forward-looking signal is the one to trust here"
)


async def test_a_midstream_provider_failure_keeps_the_streamed_text(world):
    """The failed turn's own words survive in the persisted transcript.

    They were streamed to the watching client, so dropping them left the stored
    transcript ending a turn earlier than what the operator saw — and the
    reasoning that ran into the failure is exactly what an audit of a failed run
    needs to read.
    """
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor score.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            ),
            ScriptedTurn(text=PARTIAL_TEXT, provider_error="503 upstream connection reset"),
        ]
    )
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "failed"
    assert "upstream connection reset" in result.run.error
    assert result.event_types[-1] == "error"

    # The partial turn is on the record, marked as partial and carrying the cause.
    last = result.run.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == PARTIAL_TEXT
    assert last["meta"]["partial"] is True
    assert "upstream connection reset" in last["meta"]["provider_error"]

    # ...and the transcript stays replayable: no tool_call without a result.
    called = {c["id"] for m in result.run.messages for c in (m.get("tool_calls") or [])}
    answered = {m["tool_call_id"] for m in result.run.messages if m["role"] == "tool"}
    assert called == answered

    # Everything committed before the failure is still there, unchanged.
    assert [e.data["tool"] for e in result.events_of("tool_call")] == ["lookup_dataset"]
    assert result.findings == []


# ── (b) the iteration ceiling does not discard a recorded verdict ─────────────
async def test_hitting_the_ceiling_after_recording_the_verdict_still_completed(world):
    """A recorded, validated verdict is output; the ceiling does not unmake it.

    Marking this run `failed` threw away a draft finding that is on disk and
    auditable — the runs list, an operator's filter and `run_harness_task` all
    then reported "this produced nothing" about a run that produced a verdict.
    """
    # A ceiling of 5 with a 5-turn script: the verdict lands on turn 4 and the
    # model keeps calling tools, so the loop runs out of iterations.
    harness_id = await world.create_harness(name="Ceiling Analyst", max_iterations=5)
    provider = ReplayProvider(
        [
            *divergence_happy_script()[:4],
            ScriptedTurn(
                text="Double-checking the score once more.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            ),
        ]
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.iterations == 5  # the ceiling really was reached
    assert result.provider.turns_played == 5
    assert result.run.status == "completed", result.run.error
    assert result.run.error is None
    assert result.finding.payload["verdict"] == "diverge_signal_higher"

    # It is still surfaced as a budget event, so hitting the ceiling is visible.
    warnings = [e.data for e in result.events_of("budget_warning")]
    assert warnings == [{"kind": "iterations", "iterations": 5, "budget": 5}]
    assert result.event_types[-1] == "done"
    assert result.events_of("done")[0].data["status"] == "completed"


async def test_hitting_the_ceiling_with_nothing_recorded_still_fails(world):
    """The other side of it: no verdict at the ceiling is a real failure."""
    harness_id = await world.create_harness(name="Looping Analyst", max_iterations=3)
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the score again.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
            )
        ]
        * 3
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "failed"
    assert result.run.error == "max_iterations (3) reached without completion"
    assert result.findings == []
    assert result.events_of("budget_warning") == []


# ── (c) an undeclared task type is refused, not improvised ────────────────────
async def test_a_task_type_the_pack_never_declared_is_refused(world):
    """No pack declaration, no task: the engine must not invent one.

    It used to fabricate a `{"shape": "freeform"}` config, so a typo'd or
    uninstalled task type ran a full freeform turn — no task instructions, no
    output schema, no terminal tool — and then reported `completed`, which claims
    the requested assessment was performed.
    """
    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        task_type="divergence_assesment",  # one letter short of the real slug
        task_input={"site_id": SITE, "peril": PERIL},
    )

    assert result.run.status == "failed"
    assert "unknown_task_type: 'divergence_assesment'" in result.run.error
    assert "divergence_assessment" in result.run.error  # names what IS declared
    # Refused before the first token: no model call, no cost, no transcript.
    assert result.provider.turns_played == 0
    assert result.run.messages == []
    assert result.run.cost_usd == 0
    assert result.event_types == ["error"]


async def test_freeform_still_runs_without_a_pack_declaration(world):
    """The engine's own task types need no declaration — freeform still works."""
    provider = ReplayProvider([ScriptedTurn(text="Here is what I can say without tools.")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise what you know about the flood book."},
    )
    assert result.run.status == "completed", result.run.error


# ── (d) delegation is bounded ─────────────────────────────────────────────────
DELEGATING_PACK_YAML = """\
pack: delegation-test
version: 0.1.0
display_name: Delegation Test
description: A one-task pack whose task can delegate to itself.
doctrine:
  - doctrine/01-rules.md
task_types:
  - slug: recursive_task
    display_name: Recursive task
    shape: freeform
    input_schema:
      subject_id: { type: string, description: "Anything" }
    tools: [run_harness_task]
    output_contract: Free text.
    instructions: Delegate this task to a specialist and report what came back.
"""


async def _delegating_world(tmp_path):
    pack_dir = tmp_path / "delegation-pack"
    (pack_dir / "doctrine").mkdir(parents=True)
    (pack_dir / "pack.yaml").write_text(DELEGATING_PACK_YAML)
    (pack_dir / "doctrine" / "01-rules.md").write_text("# Rules\n\nDelegate once.\n")
    return await build_world(tmp_path / "delegation.db", pack_dir=pack_dir)


class SelfDelegatingProvider(ReplayProvider):
    """Delegates once per conversation, then reports — at whatever depth it is.

    A fixed script cannot express this. Parent and child runs share one provider
    (the engine rebuilds its registry per run, and the patched registry hands out
    this instance), so their turns interleave and a positional script stops
    lining up. Deriving the turn from the conversation instead means every run in
    the chain behaves identically, which is exactly the runaway shape the depth
    ceiling has to stop.
    """

    def __init__(self) -> None:
        super().__init__([])

    async def stream(self, *, model, system, messages, tools, max_tokens, temperature):
        self.calls.append(
            ProviderCall(
                model=model,
                system=system,
                messages=list(messages),
                tool_names=[t.name for t in tools],
                max_tokens=max_tokens,
                temperature=temperature,
            )
        )
        usage = Usage(input_tokens=900, output_tokens=120)
        already_delegated = any(
            call.name == "run_harness_task" for m in messages for call in m.tool_calls
        )
        if already_delegated:
            yield TextDelta("Reporting what the specialist run returned.")
            yield TurnComplete(usage=usage, stop_reason="end_turn")
            return
        yield TextDelta("Delegating this on to a specialist.")
        yield ToolCallComplete(
            ToolCall(
                id=f"call-{len(self.calls)}",
                name="run_harness_task",
                arguments={
                    "task_type": "recursive_task",
                    "task_input": {"subject_id": "anything"},
                },
            )
        )
        yield TurnComplete(usage=usage, stop_reason="tool_use")


async def test_delegation_cannot_recurse_without_end(tmp_path):
    """A task type that can delegate to itself is bounded by the depth ceiling.

    Refusing chat/freeform task types is not what stops recursion: any pack task
    may list `run_harness_task`, so A can delegate to B, B to A, or — as here — a
    task to itself. Without a ceiling the first delegation starts a chain of runs
    that only ends when every run in it independently hits its own cost cap.
    """
    world = await _delegating_world(tmp_path)
    try:
        provider = SelfDelegatingProvider()
        result = await world.run(
            provider=provider,
            task_type="recursive_task",
            task_input={"subject_id": "anything"},
        )

        assert result.run.status == "completed", result.run.error

        # The chain is MAX_DELEGATION_DEPTH hops deep and then stops.
        async with world.session_factory() as db:
            runs = (await db.execute(select(Run).order_by(Run.created_at))).scalars().all()
        assert len(runs) == MAX_DELEGATION_DEPTH + 1
        assert [r.task_input.get("_delegation_depth", 0) for r in runs] == list(
            range(MAX_DELEGATION_DEPTH + 1)
        )
        assert all(r.status == "completed" for r in runs), [r.error for r in runs]

        # The deepest run was refused, in words it can act on...
        deepest = await world.read_back(runs[-1].id)
        refusals = [d for d in deepest.tool_results("run_harness_task") if d["error"]]
        assert len(refusals) == 1
        assert "Delegation limit reached" in refusals[0]["result"]
        assert f"ceiling is {MAX_DELEGATION_DEPTH}" in refusals[0]["result"]

        # ...and the delegation counter is engine bookkeeping: no prompt in the
        # whole chain shows it to the model.
        assert all(
            "_delegation_depth" not in (m.content or "")
            for call in provider.calls
            for m in call.messages
        )
    finally:
        await world.aclose()


@pytest.mark.parametrize("task_type", ["chat", "freeform"])
async def test_delegation_still_refuses_the_generic_task_types(world, task_type):
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Trying to delegate a chat turn.",
                tool_calls=[
                    ScriptedCall(
                        "run_harness_task",
                        {"task_type": task_type, "task_input": {}},
                    )
                ],
            ),
            ScriptedTurn(text="That is not delegable; answering directly."),
        ]
    )
    harness_id = await world.create_harness(
        name="Chat Harness", tool_names=["run_harness_task"]
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "Delegate this."},
    )
    assert [e["tool"] for e in result.tool_errors] == ["run_harness_task"]
    assert "not chat/freeform" in result.tool_errors[0]["result"]
