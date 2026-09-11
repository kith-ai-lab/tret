"""Compaction: keeping a long run inside its window without editing the record.

Two invariants carry this whole feature, and most of the tests below exist to
defend one of them:

1. **The transcript is untouched.** `runs.messages` stays the complete record of
   what happened; compaction produces a separate wire view.
2. **Structure survives.** No message is dropped, so every tool call keeps the
   result that answers it — a transcript that loses that pairing is unreplayable,
   and the damage surfaces as a provider error nowhere near its cause.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from tret.engine.compaction import (
    ELIDABLE_TOOLS,
    KEEP_RECENT_ITERATIONS,
    MIN_ELIDABLE_CHARS,
    PROTECTED_TOOLS,
    CompactionState,
    apply_plan,
    budget,
    elided_source_text,
    estimate_wire_tokens,
    over_budget,
    plan_compaction,
    summarize,
    trim_history,
    wire_view,
)
import tret.engine.harness as harness_module
from tret.engine.harness import HarnessEngine
from tret.engine.tools import get_builtin_tools
from tret.providers.base import JsonCompletion, Msg, ProviderError, ToolCall, ToolSpec, Usage
from tret.providers.catalog import ModelCatalog

BULK = "x" * (MIN_ELIDABLE_CHARS * 4)


def _turn(iteration: int, tool: str, *, content: str = BULK) -> list[Msg]:
    """One assistant tool call and the result answering it."""
    call_id = f"call-{iteration}-{tool}"
    return [
        Msg(
            role="assistant",
            content="working",
            tool_calls=[ToolCall(call_id, tool, {"q": "x"})],
            meta={"iteration": iteration},
        ),
        Msg(role="tool", content=content, tool_call_id=call_id, meta={"iteration": iteration}),
    ]


def _transcript(*tools: str, start: int = 1) -> list[Msg]:
    messages = [Msg(role="user", content="the task")]
    for offset, tool in enumerate(tools):
        messages += _turn(start + offset, tool)
    return messages


def _plan(messages, *, iteration=None, state=None, terminal_tool=None):
    last = iteration or max((m.meta or {}).get("iteration", 0) for m in messages)
    return plan_compaction(
        messages,
        state=state or CompactionState(),
        current_iteration=last + KEEP_RECENT_ITERATIONS + 1,
        terminal_tool=terminal_tool,
    )


# ── the classification is exhaustive ─────────────────────────────────────────
def test_every_builtin_tool_is_deliberately_classified():
    # A tool nobody decided about defaults to protected, which is the safe
    # direction — but it should be a decision, not an oversight. Adding a tool
    # fails here until someone says which side it is on.
    unclassified = sorted(set(get_builtin_tools()) - ELIDABLE_TOOLS - PROTECTED_TOOLS)
    assert unclassified == [], f"classify these in engine/compaction.py: {unclassified}"


def test_the_two_classes_do_not_overlap():
    assert not (ELIDABLE_TOOLS & PROTECTED_TOOLS)


def test_the_deterministic_lane_is_protected():
    # The load-bearing one. A model may only cite values it retrieved, and
    # citations are validated against what the tool literally returned — elide a
    # lookup_dataset result and every finding citing it fails validation, so
    # compaction would manufacture the failure it was invoked to prevent.
    assert "lookup_dataset" in PROTECTED_TOOLS
    assert "run_method" in PROTECTED_TOOLS


def test_the_recording_tools_are_protected():
    for tool in ("record_verdict", "record_finding", "draft_section"):
        assert tool in PROTECTED_TOOLS


# ── planning ─────────────────────────────────────────────────────────────────
def test_bulk_retrieval_is_what_gets_elided():
    plan = _plan(_transcript("read_document", "search_documents"))
    assert len(plan.elide) == 2
    assert sorted(set(plan.elided_tools)) == ["read_document", "search_documents"]


def test_a_protected_result_is_never_elided():
    messages = _transcript("lookup_dataset", "read_document")
    plan = _plan(messages)
    assert len(plan.elide) == 1
    assert plan.elided_tools == ["read_document"]


def test_the_tasks_terminal_tool_is_never_elided():
    messages = _transcript("read_document", "run_harness_task")
    plan = _plan(messages, terminal_tool="run_harness_task")
    assert plan.elided_tools == ["read_document"]


def test_recent_iterations_are_kept_verbatim():
    # Compacting into the turns where the model's current reasoning lives is how
    # a run loses its thread and starts over.
    messages = _transcript(*(["read_document"] * 6))
    plan = plan_compaction(
        messages, state=CompactionState(), current_iteration=6, terminal_tool=None
    )
    elided_iterations = {(messages[i].meta or {})["iteration"] for i in plan.elide}
    assert max(elided_iterations) <= 6 - KEEP_RECENT_ITERATIONS


def test_a_short_result_is_left_alone():
    # The marker would cost nearly as much as the content it replaced.
    plan = _plan(_transcript("read_document")[:1] + _turn(1, "read_document", content="tiny"))
    assert plan.empty


def test_an_unclassified_tool_is_treated_as_protected():
    # Wrongly eliding fails silently; wrongly keeping only costs a larger prompt.
    plan = _plan(_transcript("some_future_tool"))
    assert plan.empty


def test_already_elided_messages_are_not_elided_again():
    messages = _transcript("read_document", "search_documents")
    state = CompactionState()
    first = _plan(messages, state=state)
    apply_plan(state, first, 9)
    assert _plan(messages, state=state).empty


def test_planning_never_mutates_the_transcript():
    messages = _transcript("read_document", "search_documents")
    before = [m.to_json() for m in messages]
    _plan(messages)
    assert [m.to_json() for m in messages] == before


# ── the wire view ────────────────────────────────────────────────────────────
def _compacted(messages, *, iteration=9):
    state = CompactionState()
    apply_plan(state, _plan(messages), iteration)
    return state


def test_the_transcript_is_never_what_gets_compacted():
    # The decision the whole feature rests on: the audit record is what an
    # approver reads when deciding whether to trust a finding.
    messages = _transcript("read_document", "search_documents")
    before = [m.to_json() for m in messages]
    wire_view(messages, _compacted(messages))
    assert [m.to_json() for m in messages] == before
    assert all(len(m.content or "") > MIN_ELIDABLE_CHARS for m in messages if m.role == "tool")


def test_no_message_is_ever_dropped_so_every_call_keeps_its_result():
    messages = _transcript("read_document", "search_documents")
    wire = wire_view(messages, _compacted(messages))
    assert len(wire) == len(messages)
    call_ids = {c.id for m in wire if m.role == "assistant" for c in m.tool_calls}
    result_ids = {m.tool_call_id for m in wire if m.role == "tool"}
    assert call_ids == result_ids


def test_an_elided_result_says_what_was_there_and_when():
    messages = _transcript("read_document")
    wire = wire_view(messages, _compacted(messages, iteration=9))
    elided = next(m for m in wire if m.role == "tool")
    assert "elided by tret at iteration 9" in elided.content
    assert "read_document" in elided.content
    assert str(len(BULK)) in elided.content


def test_compaction_actually_frees_tokens():
    messages = _transcript("read_document", "search_documents")
    tools: list[ToolSpec] = []
    before = estimate_wire_tokens("sys", messages, tools)
    after = estimate_wire_tokens("sys", wire_view(messages, _compacted(messages)), tools)
    assert after < before / 2


def test_an_uncompacted_run_sends_exactly_what_it_always_did():
    messages = _transcript("read_document")
    assert wire_view(messages, CompactionState()) is messages


def test_the_summary_note_lands_on_an_iteration_boundary():
    # Never between a tool call and the result answering it — the one placement
    # that would make the conversation invalid.
    messages = _transcript(*(["read_document"] * 6))
    state = CompactionState()
    plan = plan_compaction(messages, state=state, current_iteration=6, terminal_tool=None)
    apply_plan(state, plan, 6)
    state.summary = "what the elided material said"
    wire = wire_view(messages, state)
    note_index = next(i for i, m in enumerate(wire) if m.content and "CONTEXT NOTE" in m.content)
    assert wire[note_index + 1].role == "assistant"
    assert wire[note_index - 1].role == "tool"


def test_the_summary_tells_the_model_not_to_cite_from_it():
    messages = _transcript("read_document")
    state = _compacted(messages)
    state.summary = "a summary"
    note = next(m for m in wire_view(messages, state) if "CONTEXT NOTE" in (m.content or ""))
    assert "retrieve it again rather than relying" in note.content


# ── the budget ───────────────────────────────────────────────────────────────
def test_the_output_reservation_is_subtracted_not_absorbed():
    # The room for the answer is already spoken for; it is not slack.
    assert budget(100_000, 8_000, 0.8) == 72_000


def test_a_model_with_no_declared_window_is_left_exactly_as_it_was():
    # A discovered local model may report nothing. Unknown is not a licence to
    # assume infinity, and it is not something to guess at either.
    assert budget(0, 8_000, 0.8) == 0
    assert over_budget(10**9, budget(0, 8_000, 0.8)) is False


def test_over_budget_is_only_true_against_a_real_limit():
    assert over_budget(100, 50) is True
    assert over_budget(10, 50) is False


# ── exact token counting near the boundary ───────────────────────────────────
# `EXACT_COUNT_THRESHOLD` and the decision it drives live in `engine/harness.py`
# (`_near_context_limit`), next to `over_budget` it feeds — not here, since
# unlike everything else in this module it is not pure with respect to the
# transcript, it decides whether to make a provider call. These tests cover
# the boundary condition in isolation; `tests/evals/test_engine_loop.py`
# covers what the engine actually does with the answer.
def test_below_the_threshold_an_exact_count_is_not_worth_asking_for():
    assert harness_module.EXACT_COUNT_THRESHOLD == 0.85
    assert harness_module._near_context_limit(8_499, 10_000) is False


def test_at_the_threshold_an_exact_count_is_worth_asking_for():
    assert harness_module._near_context_limit(8_500, 10_000) is True
    assert harness_module._near_context_limit(10_000, 10_000) is True


def test_an_unenforceable_limit_is_never_near_itself():
    # `budget()`'s own "unknown context window" signal — there is no boundary
    # to be near, the same reasoning `over_budget(x, 0)` already applies.
    assert harness_module._near_context_limit(10**9, 0) is False


# ── history trimming ─────────────────────────────────────────────────────────
def _history(n: int) -> list[Msg]:
    return [Msg(role="user" if i % 2 == 0 else "assistant", content="y" * 4000) for i in range(n)]


def test_a_long_thread_is_trimmed_to_fit_the_model_that_will_run_it():
    # The bug this fixes: api/chat.py hands over the last N turns with no idea
    # how large they are or which model will hold them, so a long thread on a
    # small-window model failed on its first call.
    kept, dropped = trim_history(
        _history(20), system="sys", user_message="now this", tools=[], limit=2_000
    )
    assert dropped > 0
    assert estimate_wire_tokens("sys", [*kept, Msg(role="user", content="now this")], []) <= 2_000


def test_trimming_drops_the_oldest_turns_first():
    history = [Msg(role="user", content=f"turn {i} " + "y" * 4000) for i in range(10)]
    kept, _ = trim_history(history, system="", user_message="x", tools=[], limit=3_000)
    assert kept == history[len(history) - len(kept):]


def test_a_thread_that_already_fits_is_untouched():
    history = _history(2)
    kept, dropped = trim_history(
        history, system="", user_message="x", tools=[], limit=1_000_000
    )
    assert dropped == 0 and kept == history


def test_this_turns_own_task_is_never_trimmed_away():
    # Only conversation that preceded this run is at risk. If even an empty
    # history cannot fit, the trim stops rather than eating the request.
    kept, dropped = trim_history(
        _history(6), system="", user_message="y" * 100_000, tools=[], limit=10
    )
    assert kept == [] and dropped == 6


# ── the summarizer is optional ───────────────────────────────────────────────
class _DeadProvider:
    async def complete_json(self, **kwargs):
        raise ProviderError("openrouter", "503")


class _LiveProvider:
    async def complete_json(self, **kwargs):
        return JsonCompletion(
            payload={"summary": "  the documents established X  "},
            usage=Usage(input_tokens=5000, output_tokens=120),
            model=kwargs.get("model", ""),
        )


def _summarizer_model():
    return next(m for m in ModelCatalog().all(curated_only=True) if m.supports_tools)


@pytest.mark.asyncio
async def test_a_summarizer_that_cannot_be_reached_costs_detail_not_the_run():
    # The elision has already freed the space; the summary is an improvement on
    # top of it, never a precondition.
    text, spend = await summarize(_DeadProvider(), _summarizer_model(), "some text")
    assert text is None and spend is None


@pytest.mark.asyncio
async def test_a_summary_is_returned_stripped():
    text, _ = await summarize(_LiveProvider(), _summarizer_model(), "some text")
    assert text == "the documents established X"


@pytest.mark.asyncio
async def test_the_summarizer_call_is_metered_against_its_own_model():
    # It runs on a cheap model resolved separately from the run's, so its energy
    # class and its provider's grid factor are its own.
    model = _summarizer_model()
    _, spend = await summarize(_LiveProvider(), model, "some text")
    assert spend["kind"] == "compaction_summary"
    assert spend["model"] == model.id
    assert spend["input_tokens"] == 5000
    assert spend["cost_usd"] > 0
    assert spend["energy_accounting"]["model"] == model.id


@pytest.mark.asyncio
async def test_an_unusable_summary_is_still_paid_for():
    # The tokens were spent whatever came back. Dropping the cost of a bad
    # answer is exactly how these numbers would flatter themselves.
    class _Empty:
        async def complete_json(self, **kwargs):
            return JsonCompletion(
                payload={"summary": "   "}, usage=Usage(input_tokens=4000, output_tokens=5)
            )

    text, spend = await summarize(_Empty(), _summarizer_model(), "some text")
    assert text is None
    assert spend is not None and spend["input_tokens"] == 4000


@pytest.mark.asyncio
async def test_nothing_to_summarize_makes_no_provider_call():
    class _Exploding:
        async def complete_json(self, **kwargs):
            raise AssertionError("should not be called")

    assert await summarize(_Exploding(), _summarizer_model(), "   ") == (None, None)


def test_the_summarizer_is_shown_only_what_was_elided():
    messages = _transcript("read_document", "lookup_dataset")
    plan = _plan(messages)
    source = elided_source_text(messages, plan)
    assert BULK in source
    assert len(plan.elide) == 1  # the dataset result was protected, so not sent


# ── forcing a pass ahead of a model switch ──────────────────────────────────
# `HarnessEngine._compact` (engine/harness.py) is what the engine calls for
# both the ordinary over-budget path and the pass a supervisor switch forces
# on the new model's first turn even when that turn is not, on its own, over
# budget (`compact_before_next_turn`). The elision rules are identical either
# way — the same `plan_compaction` tested above — so what these tests defend
# is that a forced pass elides exactly what a budget-triggered one would,
# never touches a protected result, is honest when there is nothing left, and
# says which of the two caused it via the `trigger` it records.
def _iteration_after(messages: list[Msg]) -> int:
    last = max((m.meta or {}).get("iteration", 0) for m in messages)
    return last + KEEP_RECENT_ITERATIONS + 1


async def _forced_pass(messages: list[Msg], state: CompactionState, *, before_tokens: int = 1) -> dict:
    """Call `_compact` the way a forced switch pass does: `trigger="model_switch"`,
    on an engine whose router is stubbed to skip the summarizer entirely — the
    summary is an optional improvement on top of the elision (see `_compact`'s
    own docstring), never a precondition for it, so a unit test of the elision
    itself has no need to reach for a real provider.
    """
    engine = HarnessEngine()
    with patch.object(engine.router, "_resolve_router_model", return_value=None):
        return await engine._compact(
            run=None,
            messages=messages,
            state=state,
            iteration=_iteration_after(messages),
            before_tokens=before_tokens,
            system="sys",
            tool_specs=[],
            terminal_tool=None,
            max_tier="premium",
            overhead=[],
            emissions=None,
            trigger="model_switch",
        )


@pytest.mark.asyncio
async def test_a_forced_pass_elides_eligible_results_even_under_budget():
    messages = _transcript("read_document", "search_documents")
    state = CompactionState()
    record = await _forced_pass(messages, state, before_tokens=1)

    assert record["kind"] == "elision"
    assert record["trigger"] == "model_switch"
    assert sorted(record["elided_tools"]) == ["read_document", "search_documents"]
    wire = wire_view(messages, state)
    assert sum("elided by tret" in (m.content or "") for m in wire) == 2


@pytest.mark.asyncio
async def test_a_forced_pass_never_elides_a_protected_result():
    messages = _transcript("lookup_dataset", "read_document")
    state = CompactionState()
    record = await _forced_pass(messages, state)

    assert record["elided_tools"] == ["read_document"]
    wire = wire_view(messages, state)
    protected = next(m for m in wire if (m.tool_call_id or "").endswith("lookup_dataset"))
    assert protected.content == BULK  # untouched, not replaced by a marker


@pytest.mark.asyncio
async def test_a_forced_pass_with_nothing_left_records_a_no_op_honestly():
    # Only protected content — nothing eligible, forced or not.
    messages = _transcript("lookup_dataset")
    state = CompactionState()
    record = await _forced_pass(messages, state, before_tokens=42)

    assert record["kind"] == "no_op"
    assert record["trigger"] == "model_switch"
    assert record["before_est_tokens"] == 42
    assert record["after_est_tokens"] == 42
    assert not state.active  # nothing was ever elided
    # A forced pass is not the budget path: this run may be nowhere near its
    # window (a `model_switch` pass runs regardless), so the note must not
    # borrow the budget path's "over the context budget" wording.
    assert record["note"] == (
        "model switch: nothing eligible to elide — every result still on the "
        "transcript is protected, recent, or too short to be worth a marker"
    )
    assert "context budget" not in record["note"]
