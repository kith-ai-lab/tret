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

from unittest.mock import patch

import pytest
from golden_world import _replay_registry, build_world
from replay_provider import ProviderCall, ReplayProvider, ScriptedCall, ScriptedTurn
from sqlalchemy import select
from test_golden_runs import PERIL, SITE, divergence_happy_script

import tret.engine.harness as harness_module
from tret.db.models import Harness, Run
from tret.engine.harness import HarnessEngine
from tret.engine.tools import MAX_DELEGATION_DEPTH
from tret.packs.links import set_harness_packs
from tret.packs.loader import install_pack
from tret.providers.base import TextDelta, ToolCall, ToolCallComplete, TurnComplete, Usage
from tret.providers.catalog import ModelCatalog
from tret.router_llm.priors import NoPriors


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


async def test_a_midstream_failure_books_an_estimated_cost_for_the_streamed_tokens(world):
    """The provider was paid for PARTIAL_TEXT even though the turn never finished.

    The ProviderError branch used to skip the accounting block entirely — a
    run could stream a full page of text, die, and post `cost_usd == 0`, with
    every one of those tokens paid to the provider and never metered. This
    books an ESTIMATE for exactly the turn that died, priced through the same
    catalog path a normal turn uses, and flagged so it is never mistaken for a
    confidently metered figure.
    """
    provider = ReplayProvider(
        [ScriptedTurn(text=PARTIAL_TEXT, provider_error="503 upstream connection reset")]
    )
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Summarise the flood risk for this site."},
    )

    assert result.run.status == "failed"
    assert result.run.cost_usd > 0
    assert result.run.reported_cost_usd is not None
    assert result.run.reported_cost_usd > 0

    # Legible, not silently folded in as if it were metered: the one segment
    # this run used is on the record, and it says it is an estimate.
    timeline = result.run.model_timeline
    assert timeline and timeline[0]["estimated"] is True
    assert timeline[0]["output_tokens"] > 0  # PARTIAL_TEXT's own estimated size
    assert timeline[0]["cost_usd"] == float(result.run.cost_usd)
    # The dying turn never produced a TurnComplete to read a served_by off, and
    # nothing earlier in the segment did either — this is its only turn.
    assert timeline[0]["served_by"] is None

    last = result.run.messages[-1]
    assert last["content"] == PARTIAL_TEXT
    assert last["meta"]["estimated_usage"]["output_tokens"] > 0


async def test_a_midstream_failure_with_nothing_streamed_books_nothing_extra(world):
    """The other side of it: nothing streamed before the crash, nothing to book.

    A `ProviderError` on the very first byte (a connection refused, say) has no
    partial text and no tool calls to estimate from — booking a nonzero cost
    here would be inventing a number, not reading one off what happened.
    """
    provider = ReplayProvider([ScriptedTurn(text="", provider_error="connection refused")])
    result = await world.run(
        provider=provider,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert result.run.cost_usd == 0
    assert result.run.reported_cost_usd is None
    assert not result.run.model_timeline
    # Only the user's own opening message is on the record — no assistant turn
    # to even consider partial, since nothing ever streamed back.
    assert len(result.run.messages) == 1
    assert result.run.messages[0]["role"] == "user"


async def test_a_midstream_failure_after_a_metered_turn_carries_forward_cache_read_tokens(world):
    """The wire prefix a dying turn sent is the same prefix its predecessor
    turn sent (nothing about the conversation-so-far changes except what got
    appended at the very end) — so the predecessor's own *metered*
    cache_read_tokens is the best proxy available for how much of the dying
    turn's prompt the provider actually served from cache too.

    Booking the whole wire as fresh `input_tokens` (cache_read_tokens=0, the
    old behavior) prices a cached prefix at CACHE_READ_MULTIPLIER's 10x markup
    for nothing, and that flows straight into `reported_cost_usd` — the
    billing column. This locks in that the carried-forward figure is used,
    and that it actually lowers the estimated turn's cost against what the
    naive (no-carry) estimate would have booked.
    """
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the vendor score.",
                tool_calls=[_lookup("hazard_scores", site_id=SITE, peril=PERIL)],
                cache_read_tokens=900,
                served_by="Anthropic",
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

    # The dying (second) turn's own estimate, as recorded on the transcript.
    last = result.run.messages[-1]
    est = last["meta"]["estimated_usage"]
    assert est["cache_read_tokens"] > 0, "the prior turn's metered cache reads must carry forward"

    # The segment's served_by survives the estimate: the dying turn has no
    # TurnComplete of its own to read one off, but the first turn's real
    # TurnComplete already set it, and the ProviderError path never clears it.
    timeline = result.run.model_timeline
    assert timeline and timeline[0]["served_by"] == "Anthropic"

    # Same total wire estimate either way — only the split between
    # `input_tokens` and `cache_read_tokens` changes — so pricing the naive
    # split (all of it fresh) against the actual split isolates exactly what
    # the carry-forward buys.
    model = ModelCatalog().get(result.run.model_used)
    naive_input_tokens = est["input_tokens"] + est["cache_read_tokens"]
    naive_cost = model.cost_usd(naive_input_tokens, est["output_tokens"], 0)
    actual_cost = model.cost_usd(est["input_tokens"], est["output_tokens"], est["cache_read_tokens"])
    assert actual_cost < naive_cost


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


async def test_a_harness_tool_with_no_builtin_fails_the_run_rather_than_vanishing(world):
    """A stored tool name the engine cannot resolve is refused, never dropped.

    `api/harnesses.py` now rejects unknown names on write, so this can only be a
    harness row written before that validation existed (or one naming a tool the
    engine has since removed) — which is exactly the case that must not run. The
    engine used to filter the list down to what it could resolve, so the harness
    ran stripped of a capability its author declared and still reported
    `completed`. Created straight in the DB, bypassing the API, to reproduce it.
    """
    harness_id = await world.create_harness(
        tool_names=["read_document", "summarise_everything"], with_pack=False
    )
    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "Anything at all."},
    )

    assert result.run.status == "failed"
    assert "unknown_tool" in result.run.error
    assert "summarise_everything" in result.run.error
    assert "read_document" in result.run.error  # names what IS available
    # Refused before the first token: no model call, no cost, no transcript.
    assert result.provider.turns_played == 0
    assert result.run.messages == []
    assert result.run.cost_usd == 0
    assert result.event_types == ["error"]


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

    async def stream(
        self, *, model, system, messages, tools, max_tokens, temperature, effort=None, session_id=None
    ):
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


async def test_delegation_finished_always_pairs_with_delegation_started(tmp_path, monkeypatch):
    """`run_harness_task` publishes `delegation_started` right after creating
    the child run, then hands it to `engine.execute()`. That call is not
    expected to raise — every ordinary run failure becomes a `failed` Run row
    instead — but the one path outside that (this forces `load_db_keys`,
    called at the very top of `execute()`, to blow up on the child's own call)
    must still leave a matching `delegation_finished` on the record, with
    "unknown" standing in for a status nothing was ever computed for. Before
    this fix, that publish sat after the result-building block, so an
    exception there left `delegation_started` with no finish at all.
    """
    world = await _delegating_world(tmp_path)
    try:
        engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
        monkeypatch.setattr(harness_module, "_engine", engine)

        provider = SelfDelegatingProvider()
        harness_id = await world.create_harness(tool_names=["run_harness_task"])
        run_id = await world.create_run(
            harness_id=harness_id,
            task_type="recursive_task",
            task_input={"subject_id": "anything"},
        )

        import tret.services.credentials as credentials_module

        real_load_db_keys = credentials_module.load_db_keys
        calls = 0

        async def flaky_load_db_keys(db, workspace_id=None):
            nonlocal calls
            calls += 1
            if calls == 2:  # the parent's own call is first; this is the child's
                raise RuntimeError("boom: simulated credential store outage")
            return await real_load_db_keys(db, workspace_id)

        monkeypatch.setattr(credentials_module, "load_db_keys", flaky_load_db_keys)

        with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
            # `execute_tool` (engine/tools.py) catches any tool bug, including
            # `run_harness_task` re-raising this, as an ordinary tool error —
            # so the *parent's* own `execute()` still returns normally.
            await engine.execute(run_id)

        result = await world.read_back(run_id, provider=provider)
        started = [e.data for e in result.events_of("delegation_started")]
        finished = [e.data for e in result.events_of("delegation_finished")]

        assert len(started) == 1
        assert len(finished) == 1
        assert finished[0]["child_run_id"] == started[0]["child_run_id"]
        assert finished[0]["status"] == "unknown"
        # No lineage left dangling despite the raise: `unregister_delegation`
        # runs in the same `finally` as the publish.
        assert not engine._parent_of
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


# ── (e) delegation searches every linked pack, not just the primary ───────────
SECOND_PACK_YAML = """\
pack: second-pack
version: 0.1.0
display_name: Second Pack
task_types:
  - slug: second_task
    display_name: Second task
    shape: freeform
    output_contract: Free text.
    instructions: Reply with a short confirmation. Do not call any tools.
"""


async def test_delegation_finds_a_harness_whose_second_linked_pack_declares_the_task_type(
    world, tmp_path
):
    """A harness may now link more than one pack (one harness, many packs).
    `run_harness_task` must search every pack a candidate harness links, not
    only its primary one, and the child run it creates must carry the
    *declaring* pack's id — here the harness's second-linked pack, not its
    primary (climate-risk, from `world.create_harness`'s default)."""
    second_pack_dir = tmp_path / "second-pack"
    second_pack_dir.mkdir()
    (second_pack_dir / "pack.yaml").write_text(SECOND_PACK_YAML)
    async with world.session_factory() as db:
        second_pack = await install_pack(db, second_pack_dir, world.workspace_id, world.project_id)

    harness_id = await world.create_harness(tool_names=["run_harness_task"])
    async with world.session_factory() as db:
        harness = await db.get(Harness, harness_id)
        # Primary pack stays climate-risk (position 0, from create_harness);
        # the pack that actually declares `second_task` is linked second.
        await set_harness_packs(db, harness, [world.pack_id, second_pack.id])
        await db.commit()

    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Delegating the second task.",
                tool_calls=[
                    ScriptedCall(
                        "run_harness_task",
                        {"task_type": "second_task", "task_input": {}},
                    )
                ],
            ),
            ScriptedTurn(text="Confirmed."),  # the child run's only turn
            ScriptedTurn(text="The specialist confirmed the second task."),
        ]
    )
    result = await world.run(
        provider=provider,
        harness_id=harness_id,
        task_type="freeform",
        task_input={"message": "Please delegate the second task."},
    )

    assert result.run.status == "completed", result.run.error
    assert result.tool_errors == []

    async with world.session_factory() as db:
        child = (
            await db.execute(select(Run).where(Run.task_type == "second_task"))
        ).scalars().one()
    assert child.harness_id == harness_id
    assert child.pack_id == second_pack.id  # the declaring pack, not the primary
    assert child.status == "completed"


# ── (f) `_cancelled` does not grow without bound ───────────────────────────────
async def test_cancelling_a_run_that_fails_before_start_still_clears_cancelled(world):
    """Before this fix, `_cancelled` was only ever discarded on the loop's
    *normal* finish (`_execute_inner`'s own happy path) — a run cancelled
    while it was still failing a pre-flight check (`_fail_before_start`: an
    unknown task type here) never reached that line, so its id sat in
    `_cancelled` forever. `POST /api/runs/{id}/cancel` (api/runs.py) never
    checks a run's status before calling `cancel()`, so nothing stops an
    operator's cancel click from racing a run that is about to fail this way.

    `world.run()` builds its own private engine per call and never hands it
    back, so this drives `HarnessEngine.execute` directly — same reason
    `test_delegation_cancel.py` does.
    """
    provider = ReplayProvider([ScriptedTurn(text="Should never be asked anything.")])
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    harness_id = await world.create_harness()
    run_id = await world.create_run(
        harness_id=harness_id,
        task_type="divergence_assesment",  # one letter short of the real slug
        task_input={"site_id": SITE, "peril": PERIL},
    )

    engine.cancel(run_id)  # races the run, before it ever reaches the loop
    assert engine._cancelled == {run_id}

    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)

    async with world.session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.status == "failed"
    assert "unknown_task_type" in run.error
    assert provider.calls == []  # refused before the first token, as ever

    assert engine._cancelled == set()


# ── (g) document_ids: every document a run touches, not just what it started
# with ───────────────────────────────────────────────────────────────────────
async def test_a_document_materialised_mid_run_lands_on_the_persisted_run(
    world, tmp_path, monkeypatch
):
    """`ctx.document_ids` grows as tools pull new documents in mid-run
    (`read_connected_file`, `fetch_url`'s `store_snapshot`) but nothing ever
    wrote that back onto `run.document_ids` itself — `GET /api/runs/{id}`
    kept reporting only the run's initial attachments, silently dropping
    every document a tool materialised along the way. Drives
    `read_connected_file` through the real engine (real OAuth refresh, real
    Graph calls, respx-mocked) and checks the document it pulls in lands in
    the *persisted* run's `document_ids`, not just `ctx`'s in-memory copy.
    """
    import httpx
    import respx

    from tret.config import get_settings
    from tret.db.models import Document, WorkspaceConnection
    from tret.engine import extensions as extensions_module
    from tret.net import guard as net_guard
    from tret.services.connections import GRAPH_API_BASE
    from tret.services.credentials import get_fernet

    graph = GRAPH_API_BASE
    token_url = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

    extensions_module._registry = None  # a stray registered gate must not leak in from another test
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))

    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)

    async with world.session_factory() as db:
        db.add(
            WorkspaceConnection(
                workspace_id=world.workspace_id,
                provider="m365",
                account_label="person@example.com",
                encrypted_refresh_token=get_fernet().encrypt(b"stored-refresh-token"),
                granted_scopes=["offline_access", "Files.Read.All"],
                selected_resources={
                    "read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]
                },
                status="active",
            )
        )
        await db.commit()

    harness_id = await world.create_harness(tool_names=["read_connected_file"], with_pack=False)
    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Reading the connected budget file.",
                tool_calls=[ScriptedCall("read_connected_file", {"item_ref": "m365:drive-1:item-1"})],
            ),
            ScriptedTurn(text="The Q3 budget is on file, as read from the connected drive."),
        ]
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.post(token_url).mock(
            return_value=httpx.Response(200, json={"access_token": "m365-access-token", "expires_in": 3600})
        )
        mock.get(f"{graph}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "item-1",
                    "name": "Q3 Budget.csv",
                    "size": 8,
                    "eTag": '"etag-1"',
                    "webUrl": "https://contoso.sharepoint.com/Q3%20Budget.csv",
                    "lastModifiedDateTime": "2026-08-01T00:00:00Z",
                    "file": {"mimeType": "text/csv"},
                    "parentReference": {"driveId": "drive-1", "path": "/drives/drive-1/root:/Reports"},
                },
            )
        )
        mock.get(f"{graph}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2\n")
        )
        result = await world.run(
            provider=provider,
            harness_id=harness_id,
            task_type="freeform",
            task_input={"message": "Summarise the Q3 budget from the connected drive."},
        )

    assert result.run.status == "completed", result.run.error
    async with world.session_factory() as db:
        doc = (
            (await db.execute(select(Document).where(Document.project_id == world.project_id)))
            .scalars()
            .one()
        )
    assert doc.source_kind == "connected"
    # The run started with no attachments (`create_run`'s default
    # `document_ids=[]`) — this document was only ever known to
    # `ctx.document_ids` until the harness wrote it back at completion.
    assert doc.id in result.run.document_ids
