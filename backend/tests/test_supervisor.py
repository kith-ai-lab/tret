"""The mid-run supervisor: when a run may change model, and when it may not.

Every test that matters here is about a *bound*. Switching model is easy; the
work is in refusing to do it when doing it would be worse than the stall — out
of policy, against an explicit instruction, or straight into the cost cap.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.engine.supervisor import (
    KIND_EFFORT,
    KIND_NONE,
    KIND_SWITCH,
    QUALITY_REPEATED_CALLS,
    QUALITY_TERMINAL_FAILURES,
    REASON_CONTEXT,
    REASON_QUALITY,
    REASON_STALL,
    STALL_ITERATION_FRACTION,
    STALL_REPEATED_CALLS,
    STALL_TERMINAL_FAILURES,
    TurnState,
    affordable,
    assess,
    choose_target,
    normalize_for_provider,
)
from tret.providers.base import Msg, ToolCall
from tret.providers.catalog import ModelInfo
from tret.router_llm.objectives import EFFORT_LEVELS
from tret.router_llm.priors import ModelPrior


def _model(name: str, *, window: int = 200_000, out: str = "5", inp: str = "1") -> ModelInfo:
    return ModelInfo(
        id=f"test/{name}",
        provider="openrouter",
        wire_id=name,
        display_name=name,
        cost_tier="standard",
        input_price_per_mtok=Decimal(inp),
        output_price_per_mtok=Decimal(out),
        context_window=window,
        supports_tools=True,
        strengths=[],
        curated=True,
        released="2026-01",
    )


SMALL = _model("small", window=100_000, out="1", inp="1")
BIG = _model("big", window=1_000_000, out="3", inp="2")
STRONG = _model("strong", window=100_000, out="20", inp="10")


def _prior(model_id: str, floor: float, mean: float | None = None) -> ModelPrior:
    mean = floor if mean is None else mean
    return ModelPrior(
        model_id=model_id,
        runs=40,
        effective_n=30.0,
        quality_mean=mean,
        quality_raw=mean,
        quality_ci_low=floor,
        delivered_rate=mean,
        failure_rate=1 - mean,
        mean_cost_usd=0.02,
        mean_output_tokens=800,
        mean_iterations=5.0,
        mean_energy_wh=0.4,
        approvals=0,
        rejections=0,
    )


def _prior_with_delivered(model_id: str, *, floor: float, delivered: float) -> ModelPrior:
    """A prior whose `delivered_rate` is set independently of its quality
    fields — `_prior` always ties the two together, which is exactly what the
    delivered-rate guard tests need to *not* be true.
    """
    return ModelPrior(
        model_id=model_id,
        runs=40,
        effective_n=30.0,
        quality_mean=floor,
        quality_raw=floor,
        quality_ci_low=floor,
        delivered_rate=delivered,
        failure_rate=1 - delivered,
        mean_cost_usd=0.02,
        mean_output_tokens=800,
        mean_iterations=5.0,
        mean_energy_wh=0.4,
        approvals=0,
        rejections=0,
    )


def _state(**over) -> TurnState:
    args = dict(
        iteration=4,
        max_iterations=24,
        model=SMALL,
        est_wire_tokens=10_000,
        context_limit=72_000,
        compaction_exhausted=False,
        consecutive_terminal_failures=0,
        repeated_call_trips=0,
        terminal_recorded=False,
        findings_created=0,
        cost_so_far=Decimal("0.10"),
        max_cost_usd=Decimal("5.0"),
        switches_used=0,
        max_switches=1,
        escalation="on_stall",
        overridden=False,
    )
    args.update(over)
    return TurnState(**args)


def _assess(state, candidates=(SMALL, BIG, STRONG), priors=None):
    return assess(state, candidates=list(candidates), priors=priors)


# ── a healthy run is left alone ──────────────────────────────────────────────
def test_a_run_going_fine_is_not_touched():
    assert _assess(_state()).kind == KIND_NONE


def test_a_run_deep_into_its_budget_but_producing_is_not_stalled():
    # Thorough is not the same as stuck. A run that is recording findings is
    # working, however many iterations it takes.
    state = _state(iteration=20, findings_created=3)
    assert _assess(state).kind == KIND_NONE


def test_a_run_that_already_recorded_its_result_is_never_switched():
    state = _state(iteration=20, terminal_recorded=True, consecutive_terminal_failures=9)
    assert _assess(state).kind == KIND_NONE


# ── the hard bounds ──────────────────────────────────────────────────────────
def test_escalation_off_means_off():
    state = _state(escalation="off", consecutive_terminal_failures=STALL_TERMINAL_FAILURES)
    assert _assess(state).kind == KIND_NONE


def test_a_pinned_or_overridden_run_is_never_switched():
    # The caller named a model. Quietly running a different one is the exact
    # substitution the router's override handling exists to prevent.
    state = _state(overridden=True, consecutive_terminal_failures=STALL_TERMINAL_FAILURES)
    assert _assess(state).kind == KIND_NONE


def test_the_switch_limit_is_enforced_and_the_refusal_is_recorded():
    state = _state(
        consecutive_terminal_failures=STALL_TERMINAL_FAILURES, switches_used=1, max_switches=1
    )
    result = _assess(state)
    assert result.kind == KIND_NONE
    assert "already changed model" in result.refused


def test_a_switch_that_would_blow_the_cost_cap_is_refused():
    # A switch re-sends the whole transcript at full input price. Escalating into
    # a guaranteed cost_cap_exceeded reaches the same failure more expensively.
    state = _state(
        consecutive_terminal_failures=STALL_TERMINAL_FAILURES,
        est_wire_tokens=400_000,
        cost_so_far=Decimal("4.99"),
        max_cost_usd=Decimal("5.00"),
    )
    result = _assess(state)
    assert result.kind == KIND_NONE
    assert "cost cap" in result.refused


def test_a_refusal_still_names_what_was_wrong():
    # "The run was stuck and tret chose not to act" is what an operator reading
    # a failed run needs, and it is invisible unless it is written down.
    state = _state(consecutive_terminal_failures=STALL_TERMINAL_FAILURES, switches_used=5)
    result = _assess(state)
    assert result.reason == REASON_STALL
    assert "failed validation" in result.detail


def test_a_switch_can_only_reach_the_candidates_the_policy_permits():
    # `candidates` is the router's own list, so `allowed`, the cost ceiling and
    # the provider-key check are all already applied. Nothing here widens them.
    state = _state(consecutive_terminal_failures=STALL_TERMINAL_FAILURES)
    result = _assess(state, candidates=(SMALL,))
    assert result.kind == KIND_NONE
    assert "no candidate" in result.refused


# ── stalls ───────────────────────────────────────────────────────────────────
def test_repeated_terminal_validation_failures_are_a_stall():
    state = _state(consecutive_terminal_failures=STALL_TERMINAL_FAILURES)
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_STALL


def test_one_validation_failure_is_not_a_stall():
    # The engine gives a model three attempts to repair its own output; using one
    # of them is the mechanism working.
    assert _assess(_state(consecutive_terminal_failures=1)).kind == KIND_NONE


def test_looping_on_the_same_retrieval_is_a_stall():
    state = _state(repeated_call_trips=STALL_REPEATED_CALLS)
    assert _assess(state).kind == KIND_SWITCH


def test_burning_the_iteration_budget_with_nothing_recorded_is_a_stall():
    state = _state(iteration=int(24 * STALL_ITERATION_FRACTION) + 1, findings_created=0)
    assert _assess(state).kind == KIND_SWITCH


# ── context exhaustion ───────────────────────────────────────────────────────
def test_being_over_the_window_is_not_a_stall_while_compaction_can_still_help():
    # Compaction is the cheaper remedy and gets to go first.
    state = _state(est_wire_tokens=90_000, context_limit=72_000, compaction_exhausted=False)
    assert _assess(state).kind == KIND_NONE


def test_over_the_window_with_nothing_left_to_elide_asks_for_a_bigger_one():
    state = _state(est_wire_tokens=90_000, context_limit=72_000, compaction_exhausted=True)
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_CONTEXT
    assert result.target is BIG


def test_context_pressure_takes_the_smallest_window_that_clears_the_problem():
    # A run that outgrew 100k needs room, not the largest window on the menu.
    huge = _model("huge", window=5_000_000)
    mid = _model("mid", window=400_000)
    target = choose_target([SMALL, huge, mid], SMALL, need_larger_window=True)
    assert target is mid


def test_context_pressure_with_no_larger_window_says_so():
    state = _state(est_wire_tokens=90_000, context_limit=72_000, compaction_exhausted=True)
    result = _assess(state, candidates=(SMALL, STRONG))  # STRONG is not larger
    assert result.kind == KIND_NONE
    assert "larger-context" in result.refused


# ── choosing where to go ─────────────────────────────────────────────────────
def test_a_stall_escalates_to_the_best_recorded_candidate():
    priors = {
        SMALL.id: _prior(SMALL.id, 0.30),
        BIG.id: _prior(BIG.id, 0.55),
        STRONG.id: _prior(STRONG.id, 0.82),
    }
    target = choose_target([SMALL, BIG, STRONG], SMALL, need_larger_window=False, priors=priors)
    assert target is STRONG


def test_a_candidate_with_a_proven_poor_record_is_never_the_escalation_target():
    priors = {
        SMALL.id: _prior(SMALL.id, 0.30),
        BIG.id: _prior(BIG.id, 0.0, mean=0.1),  # proven poor
        STRONG.id: _prior(STRONG.id, 0.40),
    }
    target = choose_target([SMALL, BIG, STRONG], SMALL, need_larger_window=False, priors=priors)
    assert target is STRONG


def test_a_stall_does_not_move_to_a_candidate_with_a_worse_record():
    priors = {SMALL.id: _prior(SMALL.id, 0.80), STRONG.id: _prior(STRONG.id, 0.20)}
    assert (
        choose_target([SMALL, STRONG], SMALL, need_larger_window=False, priors=priors) is None
    )


def test_with_no_evidence_a_stall_escalates_by_the_capability_proxy():
    # Price stands in for capability throughout the router (objectives.py), and
    # the ceiling has already been applied, so this cannot climb past the policy.
    target = choose_target([SMALL, BIG, STRONG], SMALL, need_larger_window=False)
    assert target is BIG  # the next step up, not the most expensive one


def test_a_run_already_on_the_strongest_permitted_model_has_nowhere_to_go():
    assert choose_target([SMALL, STRONG], STRONG, need_larger_window=False) is None


# ── affordability ────────────────────────────────────────────────────────────
def test_a_run_with_room_can_afford_a_switch():
    assert affordable(_state(est_wire_tokens=10_000), BIG) is True


def test_a_run_with_its_cap_spent_cannot():
    assert affordable(_state(cost_so_far=Decimal("5.0"), max_cost_usd=Decimal("5.0")), BIG) is False


def test_affordability_is_priced_on_the_transcript_the_switch_will_re_send():
    # The cache is void after a switch, so the next turn pays full input price on
    # everything, not just on the new tokens.
    cheap = _state(est_wire_tokens=1_000, cost_so_far=Decimal("4.9"), max_cost_usd=Decimal("5.0"))
    dear = _state(est_wire_tokens=500_000, cost_so_far=Decimal("4.9"), max_cost_usd=Decimal("5.0"))
    assert affordable(cheap, BIG) is True
    assert affordable(dear, BIG) is False


# ── the switch stays replayable ──────────────────────────────────────────────
def test_normalizing_for_a_new_provider_keeps_every_call_paired_with_its_result():
    # The invariant that makes a mid-run switch safe at all: Msg/ToolCall is
    # provider-neutral and both adapters pass ids through verbatim, so the new
    # provider sees ids it never issued — which is fine only while every call
    # still has its answer.
    messages = [
        Msg(role="user", content="go"),
        Msg(role="assistant", tool_calls=[ToolCall("c1", "read_document", {})]),
        Msg(role="tool", content="text", tool_call_id="c1"),
    ]
    out = normalize_for_provider(messages, "anthropic")
    called = {c.id for m in out if m.role == "assistant" for c in m.tool_calls}
    answered = {m.tool_call_id for m in out if m.role == "tool"}
    assert called == answered


@pytest.mark.parametrize("provider", ["anthropic", "openrouter", "kimi", "local"])
def test_normalizing_never_loses_a_message(provider):
    messages = [Msg(role="user", content="go"), Msg(role="assistant", content="ok")]
    assert normalize_for_provider(messages, provider) == messages


def test_an_untried_candidate_is_still_eligible_when_the_recorded_ones_are_worse():
    # Consistent with the rule everywhere else: untried is untried, not bad. A
    # model with a record that says "worse than the incumbent" has been ruled
    # out; a model with no record has not.
    priors = {SMALL.id: _prior(SMALL.id, 0.80), BIG.id: _prior(BIG.id, 0.20)}
    target = choose_target([SMALL, BIG, STRONG], SMALL, need_larger_window=False, priors=priors)
    assert target is STRONG


def test_a_recorded_worse_candidate_is_not_reached_by_the_price_fallback():
    priors = {SMALL.id: _prior(SMALL.id, 0.80), STRONG.id: _prior(STRONG.id, 0.20)}
    assert choose_target([SMALL, STRONG], SMALL, need_larger_window=False, priors=priors) is None


# ── the quality trigger (escalation == "on_quality") ─────────────────────────
# `on_quality` is a strict superset of `on_stall`: everything above must keep
# passing unmodified (it does — none of it sets `escalation="on_quality"`),
# and the tests below are additive, not replacements.
def test_the_quality_trigger_never_fires_under_on_stall():
    # Two failures and one repeated-call trip both clear the *quality*
    # thresholds but not `_stalled`'s own — under `on_stall` neither may do
    # anything, because that mode must never change behaviour by adding this
    # feature.
    assert _assess(_state(escalation="on_stall", consecutive_terminal_failures=2)).kind == KIND_NONE
    assert _assess(_state(escalation="on_stall", repeated_call_trips=1)).kind == KIND_NONE


def test_two_consecutive_terminal_failures_trigger_under_on_quality():
    state = _state(escalation="on_quality", consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES)
    result = _assess(state)
    assert result.kind != KIND_NONE
    assert result.reason == REASON_QUALITY


def test_one_terminal_failure_does_not_trigger_the_quality_signal():
    # The repair loop working as designed, same as it is for `_stalled`.
    state = _state(escalation="on_quality", consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES - 1)
    assert _assess(state).kind == KIND_NONE


def test_one_repeated_call_trip_triggers_under_on_quality():
    state = _state(escalation="on_quality", repeated_call_trips=QUALITY_REPEATED_CALLS)
    result = _assess(state)
    assert result.kind != KIND_NONE
    assert result.reason == REASON_QUALITY


def test_context_pressure_still_outranks_the_quality_signal():
    # A run that is genuinely out of room gets the concrete remedy (a bigger
    # window), not effort or a quality-driven switch to a same-size model.
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        est_wire_tokens=90_000,
        context_limit=72_000,
        compaction_exhausted=True,
    )
    result = _assess(state)
    assert result.reason == REASON_CONTEXT


def test_a_run_that_already_recorded_its_result_ignores_the_quality_signal_too():
    state = _state(
        escalation="on_quality",
        terminal_recorded=True,
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
    )
    assert _assess(state).kind == KIND_NONE


# ── rung 1: raise effort before ever pricing a switch ────────────────────────
def test_the_effort_rung_is_chosen_before_a_switch():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="low",
    )
    result = _assess(state)
    assert result.kind == KIND_EFFORT
    assert result.target == "medium"
    assert result.reason == REASON_QUALITY
    # No model changes hands at this rung.
    assert "from_effort" in result.evidence and "to_effort" in result.evidence


def test_the_effort_rung_treats_no_effort_as_low():
    # `effort=None` means either nothing was ever requested (a decision from
    # before effort existed) or the field was never populated — not "this
    # model is already at the top". Rung 1 must still get its shot, raising
    # to "medium" exactly as it would from an explicit "low".
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort=None,
    )
    result = _assess(state)
    assert result.kind == KIND_EFFORT
    assert result.target == "medium"
    # The evidence records the true starting point, not the "low" stand-in
    # used only to find the next rung.
    assert result.evidence["from_effort"] is None


def test_the_effort_rung_climbs_one_level_at_a_time():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="medium",
    )
    result = _assess(state)
    assert result.kind == KIND_EFFORT
    assert result.target == "high"


def test_the_effort_rung_is_skipped_when_the_model_does_not_support_it():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=False,
        effort=None,
    )
    result = _assess(state)
    # Falls straight through to rung 2, exactly like the stall path.
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_QUALITY


def test_the_effort_rung_is_skipped_once_already_at_the_top():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="high",
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH


def test_the_effort_rung_fires_at_most_once_per_run():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="low",
        effort_raised=True,
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH


def test_the_effort_rung_needs_no_affordability_check():
    # Unlike a switch, raising effort never re-sends the transcript, so it
    # must go through even when the run's remaining budget could not afford a
    # switch's re-send.
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="low",
        est_wire_tokens=400_000,
        cost_so_far=Decimal("4.99"),
        max_cost_usd=Decimal("5.00"),
    )
    assert _assess(state).kind == KIND_EFFORT


# ── rung 2: escalation can degrade (Signed Rescue Routing) ───────────────────
def test_the_delivered_rate_guard_refuses_a_target_that_finishes_this_shape_less_often():
    priors = {
        SMALL.id: _prior_with_delivered(SMALL.id, floor=0.30, delivered=0.80),
        STRONG.id: _prior_with_delivered(STRONG.id, floor=0.50, delivered=0.20),
    }
    # Without the guard — the stall path's own selection — the
    # higher-confidence candidate wins, same as today.
    assert (
        choose_target([SMALL, STRONG], SMALL, need_larger_window=False, priors=priors)
        is STRONG
    )
    # The quality path's guard refuses it anyway: however confident that
    # record is, it finishes this shape of work less often than the
    # incumbent, and escalating there would be the rescue making things worse.
    assert (
        choose_target(
            [SMALL, STRONG],
            SMALL,
            need_larger_window=False,
            priors=priors,
            guard_delivered_rate=True,
        )
        is None
    )


def test_a_quality_triggered_switch_records_the_guards_refusal():
    priors = {
        SMALL.id: _prior_with_delivered(SMALL.id, floor=0.30, delivered=0.80),
        STRONG.id: _prior_with_delivered(STRONG.id, floor=0.50, delivered=0.20),
    }
    state = _state(escalation="on_quality", consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES)
    result = _assess(state, candidates=(SMALL, STRONG), priors=priors)
    assert result.kind == KIND_NONE
    assert result.reason == REASON_QUALITY
    assert result.refused


def test_a_stall_triggered_switch_is_not_subject_to_the_delivered_rate_guard():
    # Byte-identical to today: `on_stall`'s own selection never passes
    # `guard_delivered_rate`, so the same priors that refuse a quality-driven
    # switch still let a plain stall through.
    priors = {
        SMALL.id: _prior_with_delivered(SMALL.id, floor=0.30, delivered=0.80),
        STRONG.id: _prior_with_delivered(STRONG.id, floor=0.50, delivered=0.20),
    }
    state = _state(escalation="on_stall", consecutive_terminal_failures=STALL_TERMINAL_FAILURES)
    result = _assess(state, candidates=(SMALL, STRONG), priors=priors)
    assert result.kind == KIND_SWITCH
    assert result.target is STRONG


def test_effort_levels_are_the_shared_ladder():
    # Guards against the ladder drifting out of sync with the router's own
    # `EFFORT_LEVELS` (router_llm/objectives.py) without either side noticing.
    assert EFFORT_LEVELS == ("low", "medium", "high")


# ── the grace window after a raise ───────────────────────────────────────────
# Without this, the very counters that caused the raise are still sitting at
# or past threshold on the next assess() and force a switch immediately — the
# rung buys the raised effort level zero iterations to prove itself.
def test_a_raise_buys_the_next_iteration_when_the_trip_count_does_not_grow():
    state = _state(
        escalation="on_quality",
        iteration=5,
        repeated_call_trips=1,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
        trips_at_raise=1,
    )
    assert _assess(state).kind == KIND_NONE


def test_a_second_trip_after_the_raise_does_switch():
    state = _state(
        escalation="on_quality",
        iteration=5,
        repeated_call_trips=2,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
        trips_at_raise=1,
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_QUALITY


def test_a_raise_buys_the_next_iteration_when_failures_do_not_grow():
    state = _state(
        escalation="on_quality",
        iteration=5,
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
        failures_at_raise=QUALITY_TERMINAL_FAILURES,
    )
    assert _assess(state).kind == KIND_NONE


def test_new_terminal_failures_after_the_raise_do_switch():
    # `failures_at_raise` stale-high, as if the raw counter had reached this
    # value once before an intervening successful terminal call reset
    # `consecutive_terminal_failures` to 0 (engine/harness.py resets the raw
    # counter on a success, but nothing resets `failures_at_raise` to match
    # outside of a model switch — see the self-healing baseline this is
    # exercising). Two *fresh* failures since that reset must fire the
    # quality trigger despite the raw count never reaching
    # `STALL_TERMINAL_FAILURES` — a plain `current - failures_at_raise`
    # subtraction floors at 0 for any current at or below the stale baseline,
    # silently swallowing genuinely new failures until the raw counter climbs
    # past it, which can mean waiting for the full stall threshold and losing
    # `REASON_QUALITY`'s delivered-rate guard on the way.
    state = _state(
        escalation="on_quality",
        iteration=5,
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
        failures_at_raise=STALL_TERMINAL_FAILURES,
    )
    assert state.consecutive_terminal_failures < STALL_TERMINAL_FAILURES
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_QUALITY


def test_the_grace_window_needs_a_full_completed_iteration():
    # Same iteration the raise happened at — no turn has run under the raised
    # level yet — must never retrigger, even though the raw count alone (1)
    # already clears `QUALITY_REPEATED_CALLS`. Kept below `STALL_REPEATED_
    # CALLS` too, so this is testing the grace window and not the plain stall
    # path underneath it.
    state = _state(
        escalation="on_quality",
        iteration=4,
        repeated_call_trips=1,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
        trips_at_raise=0,
    )
    assert _assess(state).kind == KIND_NONE


def test_a_dropped_terminal_failure_count_is_clamped_rather_than_going_negative():
    # `consecutive_terminal_failures` can reset to 0 on an intervening success;
    # the delta must floor at 0 rather than go negative and mask a genuine new
    # trip on the other counter.
    state = _state(
        escalation="on_quality",
        iteration=5,
        consecutive_terminal_failures=0,
        failures_at_raise=QUALITY_TERMINAL_FAILURES,
        repeated_call_trips=2,
        trips_at_raise=1,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=4,
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_QUALITY


def test_a_single_trip_refires_the_quality_trigger_once_baselines_are_reset():
    # What a state looks like right after engine/harness.py's switch block
    # clears `failures_at_raise`/`trips_at_raise` for the model the run just
    # landed on (see test_model_switch.py's `test_a_switch_resets_the_
    # quality_triggers_baselines` for the engine side): the very next trip on
    # the new model must count as fresh, not get compared against a snapshot
    # left over from the model the run just left.
    state = _state(
        escalation="on_quality",
        iteration=6,
        repeated_call_trips=QUALITY_REPEATED_CALLS,
        supports_effort=True,
        effort="medium",
        effort_raised=True,
        effort_raised_at=5,
        trips_at_raise=0,
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_QUALITY


def test_on_stall_thresholds_are_unaffected_by_the_grace_window_fields():
    # `effort_raised_at`/`failures_at_raise`/`trips_at_raise` are quality-only
    # machinery — `on_stall` must behave exactly as if they were never set.
    state = _state(
        escalation="on_stall",
        consecutive_terminal_failures=STALL_TERMINAL_FAILURES,
        effort_raised_at=1,
        failures_at_raise=STALL_TERMINAL_FAILURES,
    )
    result = _assess(state)
    assert result.kind == KIND_SWITCH
    assert result.reason == REASON_STALL


# ── max_switches: 0 disables switching, not the effort rung ─────────────────
def test_max_switches_zero_still_allows_the_effort_rung():
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="low",
        max_switches=0,
    )
    result = _assess(state)
    assert result.kind == KIND_EFFORT
    assert result.target == "medium"


def test_max_switches_zero_still_refuses_an_actual_switch():
    # The rung is already spent (effort maxed), so this falls through to
    # rung 2 — which a 0 limit must refuse exactly as it would refuse any
    # other switch. The wording says this is a policy choice, not a budget
    # this run happened to spend: "already changed model 0 time(s)" would
    # misread as a switch having already happened.
    state = _state(
        escalation="on_quality",
        consecutive_terminal_failures=QUALITY_TERMINAL_FAILURES,
        supports_effort=True,
        effort="high",
        max_switches=0,
    )
    result = _assess(state)
    assert result.kind == KIND_NONE
    assert result.refused == "this harness does not allow model switches"


def test_max_switches_zero_also_refuses_a_plain_stall_switch():
    state = _state(
        escalation="on_stall", consecutive_terminal_failures=STALL_TERMINAL_FAILURES, max_switches=0
    )
    result = _assess(state)
    assert result.kind == KIND_NONE
    assert result.refused == "this harness does not allow model switches"


# ── the delivered-rate guard needs both sides to have a prior to refuse ──────
def test_guard_delivered_rate_does_not_refuse_when_the_current_model_has_no_prior():
    # No prior for the incumbent means no delivered_rate to compare against —
    # the guard must not manufacture a refusal out of one side of a comparison
    # that does not exist.
    priors = {STRONG.id: _prior_with_delivered(STRONG.id, floor=0.50, delivered=0.20)}
    target = choose_target(
        [SMALL, STRONG],
        SMALL,
        need_larger_window=False,
        priors=priors,
        guard_delivered_rate=True,
    )
    assert target is STRONG
