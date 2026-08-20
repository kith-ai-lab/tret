"""Outcome scoring and transcript signal reading.

Offline and pure: `router_llm.outcomes.score` takes no database, no clock and no
catalog, which is deliberate — the weights that decide which model gets chosen
next have to be arguable from a table of inputs and outputs.
"""
from __future__ import annotations

import pytest

from bench.router_llm.outcomes import (
    BASE_SCORE,
    DELIVERED,
    FAILED,
    HUMAN_WEIGHT,
    NO_OUTPUT,
    OUTCOME_SCORE_VERSION,
    UNSCORED,
    error_kind,
    score,
    size_band,
)
from bench.services.transcript import (
    ENGINE_NUDGE_KEY,
    NUDGE_OUTPUT_BUDGET,
    NUDGE_TERMINAL,
    REPEATED_CALL_KEY,
    read_signals,
    validation_errors_in,
)


def _tool(content: str = "ok", *, error: bool = False, **meta) -> dict:
    return {"role": "tool", "content": content, "meta": {"error": error, **meta}}


def _clean(**over):
    args = {
        "status": "completed",
        "error": None,
        "messages": [],
        "iterations": 3,
        "max_iterations": 24,
    }
    args.update(over)
    return score(**args)


# ── transcript signals ───────────────────────────────────────────────────────
def test_validation_errors_are_counted_with_their_unrecovered_subset():
    messages = [
        _tool("Validation failed (attempt 1/3). Errors: nope", error=True),
        _tool("Validation failed and repair attempts are exhausted. Errors: nope", error=True),
        _tool("fine"),
    ]
    assert validation_errors_in(messages) == (2, 1)


def test_a_successful_tool_result_mentioning_validation_is_not_an_error():
    # The marker is only meaningful on a message the engine flagged as an error;
    # a model quoting the phrase back in a successful result must not count.
    assert validation_errors_in([_tool("Validation failed", error=False)]) == (0, 0)


def test_nudges_are_read_from_meta_not_from_the_wording():
    messages = [
        {"role": "user", "content": "totally different words", "meta": {ENGINE_NUDGE_KEY: NUDGE_TERMINAL}},
        {"role": "user", "content": "also reworded", "meta": {ENGINE_NUDGE_KEY: NUDGE_OUTPUT_BUDGET}},
    ]
    signals = read_signals(messages)
    assert signals.terminal_nudged and signals.output_budget_nudged


def test_a_repeated_call_counts_once_per_message_not_once_per_repetition():
    # The engine stamps the running count (3, then 4, then 5). Summing those
    # would score three flagged messages as nine trips.
    messages = [_tool(**{REPEATED_CALL_KEY: n}) for n in (3, 4, 5)]
    assert read_signals(messages).repeated_call_trips == 3


def test_non_validation_tool_errors_are_separable_from_validation_ones():
    messages = [
        _tool("Validation failed: bad", error=True),
        _tool("boom", error=True),
    ]
    signals = read_signals(messages)
    assert signals.tool_errors == 2
    assert signals.validation_errors == 1
    assert signals.non_validation_tool_errors == 1


def test_malformed_transcript_entries_are_ignored_rather_than_raising():
    assert read_signals(None).tool_calls == 0
    assert read_signals(["not a dict", {"role": "tool"}, {}]).tool_errors == 0


# ── outcome classes ──────────────────────────────────────────────────────────
def test_a_clean_completed_run_scores_the_delivered_base():
    result = _clean()
    assert result.outcome_class == DELIVERED
    assert result.quality_score == BASE_SCORE[DELIVERED]
    assert result.score_version == OUTCOME_SCORE_VERSION
    assert result.error_kind is None


def test_completed_without_output_scores_above_failure_and_well_below_delivery():
    result = _clean(status="completed_without_output")
    assert result.outcome_class == NO_OUTPUT
    # The guardrails worked and the task did not. It must not rank with a
    # provider outage, and it must not read as a near-success either.
    assert 0 < result.quality_score < BASE_SCORE[DELIVERED]


def test_a_cancelled_run_is_not_evidence_about_anything():
    # An operator pressing stop says nothing about the model; scoring it as a
    # failure would teach the router to avoid whichever model people interrupt.
    for status in ("cancelled", "running", "queued"):
        assert score(
            status=status, error=None, messages=[], iterations=1, max_iterations=24
        ).outcome_class == UNSCORED


def test_failures_score_zero_but_keep_their_kind():
    result = _clean(status="failed", error="cost_cap_exceeded: run cost $6 >= cap $5")
    assert result.outcome_class == FAILED
    assert result.quality_score == 0.0
    assert result.error_kind == "cost_cap_exceeded"


@pytest.mark.parametrize(
    "error,expected",
    [
        ("max_iterations (24) reached without completion", "max_iterations"),
        ("output_budget_exceeded: 900 output tokens vs budget 500", "output_budget_exceeded"),
        ("unknown_task_type: 'nope' is not declared", "unknown_task_type"),
        ("[anthropic] 529 overloaded", "provider_error"),
        ("TypeError: something in the engine", "engine_error"),
        ("No candidate models: check provider API keys.", "routing_unavailable"),
    ],
)
def test_failure_kinds_are_told_apart(error, expected):
    # A model that answers badly and a provider that was down are both failures
    # and are not the same lesson.
    assert error_kind("failed", error) == expected


def test_error_kind_is_none_for_anything_that_did_not_fail():
    assert error_kind("completed", "ignored") is None


# ── penalties ────────────────────────────────────────────────────────────────
def test_validation_failures_cost_quality():
    messy = _clean(messages=[_tool("Validation failed (attempt 1/3).", error=True)] * 2)
    assert messy.quality_score < BASE_SCORE[DELIVERED]
    assert messy.components["penalties"]["validation_errors"] > 0


def test_penalties_are_capped_so_one_signal_cannot_swamp_the_score():
    many = _clean(messages=[_tool("Validation failed (attempt 1/3).", error=True)] * 50)
    assert many.components["penalties"]["validation_errors"] == pytest.approx(0.25)


def test_being_told_to_finish_costs_quality():
    nudged = _clean(
        messages=[{"role": "user", "content": "finish", "meta": {ENGINE_NUDGE_KEY: NUDGE_TERMINAL}}]
    )
    assert nudged.quality_score < BASE_SCORE[DELIVERED]


def test_iterations_are_free_until_the_run_is_pushing_its_own_ceiling():
    # 12 of 24 is comfortable; 23 of 24 is a run that nearly did not finish. The
    # same absolute number against a ceiling of 50 is not the same evidence.
    assert _clean(iterations=12, max_iterations=24).quality_score == BASE_SCORE[DELIVERED]
    assert _clean(iterations=23, max_iterations=24).quality_score < BASE_SCORE[DELIVERED]
    assert _clean(iterations=23, max_iterations=50).quality_score == BASE_SCORE[DELIVERED]


def test_a_score_never_leaves_the_unit_interval():
    worst = _clean(
        status="failed",
        error="max_iterations (24) reached",
        messages=[_tool("Validation failed and repair attempts are exhausted.", error=True)] * 20,
        iterations=24,
    )
    assert worst.quality_score == 0.0


# ── the human verdict ────────────────────────────────────────────────────────
def test_approvals_lift_a_run_and_rejections_sink_it():
    approved = _clean(findings_approved=4)
    rejected = _clean(findings_rejected=4)
    assert approved.quality_score > BASE_SCORE[DELIVERED] > rejected.quality_score


def test_a_single_approval_is_not_a_perfect_score():
    # Laplace smoothing: one approval reads as "probably good", not "certainly
    # perfect". Otherwise the first run on a model pins it at 1.0 forever.
    result = _clean(findings_approved=1)
    assert result.components["human"]["smoothed_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert result.quality_score < 1.0


def test_a_rejected_finding_outweighs_a_technically_clean_run():
    # The whole point of weighting the human verdict: a run can complete without
    # a single validation error and still have produced the wrong answer.
    rejected = _clean(findings_rejected=3)
    assert rejected.quality_score < 0.5
    assert rejected.quality_score < _clean().quality_score
    assert rejected.components["human"]["weight"] == HUMAN_WEIGHT


def test_the_engines_observations_still_count_when_a_human_approved():
    # An approved finding from a run that fought its schema six times is not the
    # same evidence as an approved finding from a clean run.
    clean = _clean(findings_approved=3)
    messy = _clean(
        findings_approved=3,
        messages=[_tool("Validation failed (attempt 1/3).", error=True)] * 6,
    )
    assert messy.quality_score < clean.quality_score


def test_no_human_component_appears_when_nobody_decided():
    assert "human" not in _clean(messages=[]).components


# ── size bands ───────────────────────────────────────────────────────────────
def test_size_bands_are_ordered_and_total():
    assert size_band(0) == "xs"
    assert size_band(10_000) == "s"
    assert size_band(50_000) == "m"
    assert size_band(200_000) == "l"
    assert size_band(5_000_000) == "xl"


# ── a run that changed model produced evidence about both ────────────────────
def test_a_stall_handoff_is_the_strongest_negative_signal_available():
    # A within-task comparison: this model stalled on this specific problem and
    # another one picked it up. No average across different tasks says that.
    from bench.router_llm.outcomes import HANDED_OFF, handoff_score

    result = handoff_score("capability_stall")
    assert result.outcome_class == HANDED_OFF
    assert 0 < result.quality_score < BASE_SCORE[NO_OUTPUT]


def test_running_out_of_context_window_is_not_a_mark_against_a_model():
    # A window is a size, not a failing. Scoring this as poor quality would
    # teach the router that a reliable small-context model is a bad model.
    from bench.router_llm.outcomes import (
        HANDED_OFF_CAPACITY,
        NON_QUALITY_CLASSES,
        handoff_score,
    )

    result = handoff_score("context_exhausted")
    assert result.outcome_class == HANDED_OFF_CAPACITY
    assert result.outcome_class in NON_QUALITY_CLASSES


def test_both_kinds_of_handoff_explain_themselves():
    from bench.router_llm.outcomes import handoff_score

    for reason in ("capability_stall", "context_exhausted"):
        assert handoff_score(reason).components["reason"] == reason
        assert handoff_score(reason).components["note"]
