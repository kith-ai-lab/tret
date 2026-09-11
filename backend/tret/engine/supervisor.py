"""Watching a run and deciding whether to change the model under it.

The engine binds a model once, before the first token, and lives with the
consequences for up to fifty iterations. Two ways that goes wrong:

* The run outgrows the window. `engine/compaction.py` handles most of this, but
  compaction has a floor — retrieved values and recorded results are never
  elidable — and a run can be over budget with nothing left to give.
* The model turns out not to be up to the work. It fails the output schema over
  and over, or loops on the same retrieval, or burns through its iteration
  budget without ever landing a result. Today that run grinds to `failed` while
  a model that could have finished it sits one tier away.

`assess()` is the decision, and it is **deterministic — it makes no model call**.
That is not a shortcut. It runs between every pair of iterations, so an LLM judge
here would add a call per iteration to every run in the system; `router.py`
already refuses the equivalent trade for the router itself ("spending premium
tokens to save premium tokens is not a trade worth making silently"). The
judgment it needs is already available for free: the recorded track record from
`router_llm/priors.py` says which model actually finishes work of this shape.

Every intervention is bounded, and each bound exists because the unbounded
version is worse than not intervening at all:

* **Never outside the policy.** A switch is held to the same `allowed` list and
  `max_cost_tier` as the original route. Escalating out of a harness's ceiling
  because a run was struggling would make that ceiling advisory, and for a
  `max_cost_tier: local` harness the ceiling is a confidentiality control.
* **Never against an explicit choice.** A run whose model was pinned or
  overridden is never switched. The caller named a model; quietly running a
  different one is the failure `_assert_override_within_policy` already exists to
  prevent.
* **Never into a certain overrun.** A switch voids the prompt cache and re-sends
  the whole transcript, so the next turn re-pays full input price. If that will
  not fit in what is left of the cost cap, the switch is refused and the refusal
  is recorded — escalating into a guaranteed `cost_cap_exceeded` is worse than
  not escalating.
* **Rarely.** `max_switches` defaults to one. A run that has already changed
  model once and is still stuck is not usually one change away from succeeding.

`escalation == "on_quality"` (the default, `tret/adaptive.py`) adds a second,
earlier trigger and a cheaper first move. The trigger fires on 2 consecutive
terminal-validation failures or 1 repeated-call trip — both short of the
`on_stall` thresholds a plain stall waits for — because those are the earliest
points a deterministic signal can say "this is not converging" without also
being able to say so about ordinary, unhurried work (see `QUALITY_*` and
`_quality_trigger`). The first move is not a switch: `assess` raises reasoning
effort on the *same* model (`KIND_EFFORT`) when it is not already at "high"
and the model accepts the control, because that avoids the transcript re-send
a switch forces — it only falls through to a switch when effort is maxed,
unsupported, or already raised once this run. A switch reached this way
(`REASON_QUALITY`) additionally refuses a target with a lower recorded
`delivered_rate` for this shape than the current model's, even if its
`quality_ci_low` looks better — escalating into a model recorded as finishing
this shape of work *less* often is the rescue making things worse (Signed
Rescue Routing; see `choose_target`'s `guard_delivered_rate`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tret.providers.catalog import ModelInfo
from tret.router_llm.objectives import EFFORT_LEVELS, TIER_POOR, evidence_tier
from tret.router_llm.priors_base import ModelPrior

# ── stall signals ────────────────────────────────────────────────────────────
# Consecutive failures of the run's terminal tool. Three is the engine's own
# repair budget (`RunContext.max_repair_attempts`), so reaching it means the
# model has exhausted every chance to fix its own output for one payload.
STALL_TERMINAL_FAILURES = 3
# The retrieval-loop breaker has already told the model twice that the result is
# unchanged. A model still repeating itself after that is not converging.
STALL_REPEATED_CALLS = 2
# How far into the iteration budget a run may get with nothing recorded before
# it counts as stuck. Below this it may simply be doing thorough work.
STALL_ITERATION_FRACTION = 0.6

# ── quality signals (escalation == "on_quality" only) ───────────────────────
# The same two counters `_stalled` reads, at thresholds reached earlier and
# cheaper to act on than a confirmed stall:
#
# * **2, not 1.** One validation failure is the repair loop working as
#   designed (`_stalled` already treats it as unremarkable) — a single
#   malformed payload is often a formatting slip a model corrects on its own
#   next attempt. Two in a row is the point where an operator reading the
#   transcript would start to doubt the *third* attempt is coming, and it is
#   also the last iteration this signal can still change anything: three ends
#   the run (`STALL_TERMINAL_FAILURES`), so waiting for that would mean acting
#   after there is nothing left to rescue.
# * **1, not 2.** The repeated-call breaker's warning is itself already the
#   model's second chance — it fires once the *same* call has come back
#   unchanged, which the engine already treats as worth interrupting the
#   model to say so. A model that keeps calling after being told costs
#   nothing to react to immediately; waiting for `STALL_REPEATED_CALLS` (a
#   second trip) buys nothing but a more expensive rescue later.
QUALITY_TERMINAL_FAILURES = 2
QUALITY_REPEATED_CALLS = 1

# Multiplier on the estimated re-send cost, to leave room for the turn that
# follows it. A switch that exactly fits the remaining budget buys one message.
SWITCH_COST_SAFETY = Decimal("2.0")

# Reasons, recorded on the timeline and the decision.
REASON_CONTEXT = "context_exhausted"
REASON_STALL = "capability_stall"
REASON_QUALITY = "quality_signal"

KIND_NONE = "none"
KIND_SWITCH = "switch"
# Raising effort on the model already running, rather than switching to a
# different one. Cheaper than a switch: no transcript re-send, because the
# model does not change — only OpenRouter and Anthropic ever see the higher
# effort level, and even on Anthropic, where a top-level effort change still
# voids the prompt cache, that is one re-priced turn rather than a switch's
# full re-send at full input price on every turn after it.
KIND_EFFORT = "effort"


@dataclass
class TurnState:
    """Everything `assess` is allowed to look at, gathered by the engine."""

    iteration: int
    max_iterations: int
    model: ModelInfo
    est_wire_tokens: int
    context_limit: int
    compaction_exhausted: bool
    consecutive_terminal_failures: int
    repeated_call_trips: int
    terminal_recorded: bool
    findings_created: int
    cost_so_far: Decimal
    max_cost_usd: Decimal
    switches_used: int
    max_switches: int
    escalation: str
    overridden: bool
    # The current segment's reasoning-effort level, or None if either nothing
    # was requested or `model` does not accept the control — mirrors
    # `ModelSegment.effort` (see engine/harness.py), which is where this is
    # read from. Only meaningful for the quality trigger's effort rung.
    effort: str | None = None
    supports_effort: bool = False
    # True once this run has already raised effort — the rung fires at most
    # once per run, same as a switch is bounded by `max_switches`, so a model
    # that is still struggling after being given more room falls through to a
    # switch rather than being asked a second time.
    effort_raised: bool = False
    # The iteration a raise happened at, and the two stall counters' values at
    # that moment — None/0/0 until a raise occurs, set once by the engine and
    # never again this run (mirrors `effort_raised` itself). `_quality_trigger`
    # reads these to require a full turn under the raised effort, with fresh
    # evidence beyond what was already true when it was raised, before it may
    # retrigger — without this the very counters that caused the raise would
    # still be sitting at or past threshold on the very next assess() and force
    # an immediate switch, spending the rung's one shot for nothing.
    effort_raised_at: int | None = None
    failures_at_raise: int = 0
    trips_at_raise: int = 0
    # How many times, this run, a chat/freeform reply failed the grounding
    # check (engine/grounding.py) — always 0 on a run with a terminal tool,
    # since grounding is only checked where there is none. `_quality_trigger`
    # folds this into `consecutive_terminal_failures`'s own threshold: see its
    # comment for why the two counts are safe to add together.
    grounding_nudges: int = 0


@dataclass
class Intervention:
    kind: str = KIND_NONE
    # A `ModelInfo` for `KIND_SWITCH`; the effort level to move to (a string
    # from `EFFORT_LEVELS`) for `KIND_EFFORT`.
    target: ModelInfo | str | None = None
    reason: str = ""
    detail: str = ""
    # Why a switch that looked warranted was not made. Recorded rather than
    # dropped: "the run was stuck and tret chose not to act" is exactly what an
    # operator reading a failed run needs to know, and it is invisible otherwise.
    refused: str | None = None
    evidence: dict = field(default_factory=dict)

    @property
    def switching(self) -> bool:
        return self.kind == KIND_SWITCH


def _context_exhausted(state: TurnState) -> bool:
    """Over the window with nothing left that may be elided."""
    return (
        bool(state.context_limit)
        and state.est_wire_tokens > state.context_limit
        and state.compaction_exhausted
    )


def _stalled(state: TurnState) -> tuple[bool, str]:
    """Is this run failing at the work, rather than merely taking a while?"""
    if state.terminal_recorded:
        return False, ""
    if state.consecutive_terminal_failures >= STALL_TERMINAL_FAILURES:
        return True, (
            f"the terminal tool failed validation {state.consecutive_terminal_failures} "
            "times in a row"
        )
    if state.repeated_call_trips >= STALL_REPEATED_CALLS:
        return True, (
            f"the repeated-call breaker fired {state.repeated_call_trips} times without "
            "the run converging"
        )
    if (
        state.max_iterations
        and state.iteration >= state.max_iterations * STALL_ITERATION_FRACTION
        and not state.findings_created
    ):
        return True, (
            f"iteration {state.iteration} of {state.max_iterations} with nothing recorded"
        )
    return False, ""


def _quality_trigger(state: TurnState) -> tuple[bool, str]:
    """The earlier, cheaper signal `on_quality` adds on top of `_stalled`.

    Only ever consulted when `state.escalation == "on_quality"` — under
    `on_stall` this must never be called, so that mode's behaviour cannot
    change by so much as which function ran. See the `QUALITY_*` constants
    above for why these two thresholds and not `_stalled`'s own.

    After a raise (`state.effort_raised_at is not None`) this does not simply
    re-check the raw counters: `consecutive_terminal_failures` and
    `repeated_call_trips` are running totals that already tripped the
    thresholds once, to cause the raise, and would still be sitting at or past
    them on the very next call for no other reason than that they never went
    back down. The rung would then buy the raised effort level zero
    iterations — the stale count retriggers before it ever gets a turn to
    prove itself. So once a raise has happened, only what has accrued *since*
    counts, and only once at least one full iteration has completed under the
    raised level (`iteration - effort_raised_at >= 1`) — the grace window that
    gives the raise a turn to actually run before anything can act on it
    again.

    The baseline each counter is compared against is self-healing rather than
    a fixed snapshot: `consecutive_terminal_failures` resets to 0 on an
    intervening successful terminal call, and a model switch (engine/
    harness.py) resets both raw counters for the *new* segment while
    `effort_raised` itself stays true (the rung fires at most once per run,
    not once per model). Either way the raw counter can end up *below* the
    snapshot taken at raise time — comparing against the stale, higher
    snapshot would then require more fresh failures/trips than actually
    happened before the signal can retrigger, or in the switch case mask a
    single new trip entirely (a raw count that lands exactly on the stale
    snapshot subtracts to zero). So each baseline is dropped back to 0 the
    moment the raw counter it is compared against is no longer at or above it
    — at that point whatever caused the drop (a success, a switch) has already
    made the snapshot meaningless, and every count from here is "since".
    """
    if state.escalation != "on_quality" or state.terminal_recorded:
        return False, ""
    if state.effort_raised_at is not None:
        if state.iteration - state.effort_raised_at < 1:
            return False, ""
        base_failures = (
            state.failures_at_raise
            if state.consecutive_terminal_failures >= state.failures_at_raise
            else 0
        )
        base_trips = (
            state.trips_at_raise if state.repeated_call_trips >= state.trips_at_raise else 0
        )
        new_failures = max(0, state.consecutive_terminal_failures - base_failures)
        new_trips = max(0, state.repeated_call_trips - base_trips)
        if new_failures >= QUALITY_TERMINAL_FAILURES:
            return True, (
                f"the terminal tool failed validation {new_failures} more time(s) since "
                "the effort raise"
            )
        if new_trips >= QUALITY_REPEATED_CALLS:
            return True, (
                f"the repeated-call breaker fired {new_trips} more time(s) since the "
                "effort raise without the run converging"
            )
        return False, ""
    # `consecutive_terminal_failures` and `grounding_nudges` are mutually
    # exclusive per run: the former only moves around a call to
    # `ctx.terminal_tool` (engine/harness.py), which a grounding-checked run
    # has none of, and grounding is only ever checked on a run with no
    # terminal tool. Summing them lets a chat/freeform model that keeps
    # fabricating figures trip the same escalation a terminal tool stuck
    # failing validation would, without a second threshold to keep in sync.
    terminal_failures = state.consecutive_terminal_failures + state.grounding_nudges
    if terminal_failures >= QUALITY_TERMINAL_FAILURES:
        return True, (
            f"the terminal tool failed validation {state.consecutive_terminal_failures} "
            "times in a row"
            if state.consecutive_terminal_failures
            else f"the grounding check caught unsupported figures {state.grounding_nudges} "
            "times in a row"
        )
    if state.repeated_call_trips >= QUALITY_REPEATED_CALLS:
        return True, (
            f"the repeated-call breaker fired {state.repeated_call_trips} time(s) without "
            "the run converging"
        )
    return False, ""


def affordable(state: TurnState, target: ModelInfo) -> bool:
    """Can this run pay for the re-send a switch forces?

    Changing model voids the prompt cache, so the next turn re-pays the full
    input price on the whole transcript. A switch that guarantees
    `cost_cap_exceeded` two turns later has spent the run's remaining budget to
    reach the same failure by a more expensive route.
    """
    remaining = state.max_cost_usd - state.cost_so_far
    if remaining <= 0:
        return False
    resend = (
        Decimal(state.est_wire_tokens) / Decimal(1_000_000) * target.prices_at()[0]
    )
    return resend * SWITCH_COST_SAFETY <= remaining


def choose_target(
    candidates: list[ModelInfo],
    current: ModelInfo,
    *,
    need_larger_window: bool,
    priors: dict[str, ModelPrior] | None = None,
    guard_delivered_rate: bool = False,
) -> ModelInfo | None:
    """The model to move to, from the candidates the policy already permits.

    `candidates` is the router's own list, so the harness's `allowed` list, its
    cost ceiling and the provider-key check have all been applied before this is
    reached. Nothing here can widen any of them.

    For context exhaustion the requirement is concrete — a bigger window, the
    smallest one that clears the current model, because a run that outgrew 128k
    needs room and not the largest window money can buy. For a stall the
    requirement is judgment, and the recorded track record is the only evidence
    tret has: prefer the candidate that most reliably finishes work of this
    shape, and never one with a demonstrably poor record.

    `guard_delivered_rate` — set only by the quality trigger's Rung 2, never by
    a plain stall — additionally refuses any candidate whose recorded
    `delivered_rate` for this shape is *below* the current model's, when both
    have a prior. `quality_ci_low` already orders candidates by confidence in
    their mean, but a model can have a higher, less-certain mean while
    finishing strictly less often than the incumbent — and escalating on a
    quality signal into a model that is recorded as finishing *less* of this
    shape of work is the rescue making things worse (Signed Rescue Routing).
    Left off `on_stall`'s call so that mode's candidate selection cannot change
    by adding this parameter.
    """
    others = [m for m in candidates if m.id != current.id]
    if need_larger_window:
        larger = [m for m in others if m.context_window > current.context_window]
        return min(larger, key=lambda m: (m.context_window, m.id)) if larger else None

    priors = priors or {}
    current_floor = priors[current.id].quality_ci_low if current.id in priors else 0.0
    current_delivered = priors[current.id].delivered_rate if current.id in priors else None
    better = [
        (priors[m.id].quality_ci_low, m)
        for m in others
        if m.id in priors
        and evidence_tier(priors[m.id]) != TIER_POOR
        and priors[m.id].quality_ci_low > current_floor
        and not (
            guard_delivered_rate
            and current_delivered is not None
            and priors[m.id].delivered_rate < current_delivered
        )
    ]
    if better:
        return max(better, key=lambda pair: (pair[0], pair[1].id))[1]

    # No recorded candidate beats the incumbent. Fall back to the more capable
    # model, using price as the capability proxy the rest of the router already
    # uses (objectives.py) — the ceiling has been applied before this point, so
    # it cannot climb past what the harness allows.
    #
    # Candidates with a record are excluded from this fallback: their record has
    # already been consulted and did not recommend them, and switching to a model
    # the evidence says is *worse* pays a full transcript re-send to make the run
    # less likely to finish. Unrecorded candidates stay eligible, consistent with
    # the rule everywhere else that an untried model is untried, not bad.
    stronger = [
        m
        for m in others
        if m.id not in priors and m.prices_at()[1] > current.prices_at()[1]
    ]
    return min(stronger, key=lambda m: (m.prices_at()[1], m.id)) if stronger else None


def assess(
    state: TurnState,
    *,
    candidates: list[ModelInfo],
    priors: dict[str, ModelPrior] | None = None,
) -> Intervention:
    """Whether to change model before the next iteration, and to what."""
    if state.escalation == "off":
        return Intervention()
    if state.overridden:
        # The caller named a model. Running a different one because tret judged
        # it would be better is the substitution the router refuses to make.
        return Intervention()

    context_pressure = _context_exhausted(state)
    # Only ever non-trivial under `on_quality` — see `_quality_trigger`'s own
    # guard. Checked ahead of `_stalled` so a run that qualifies for both (the
    # common case: quality's thresholds are strictly earlier than stall's own)
    # reports the earlier, cheaper reason.
    quality_triggered, quality_why = _quality_trigger(state)
    stalled, stall_why = _stalled(state)
    if not context_pressure and not quality_triggered and not stalled:
        return Intervention()

    if context_pressure:
        reason = REASON_CONTEXT
        detail = (
            f"over the context budget ({state.est_wire_tokens} > {state.context_limit} est. "
            "tokens) with nothing left to elide"
        )
    elif quality_triggered:
        reason = REASON_QUALITY
        detail = quality_why
    else:
        reason = REASON_STALL
        detail = stall_why

    # Rung 1: raise effort on the model already running before ever pricing a
    # switch. Only reachable via the quality trigger — a plain stall goes
    # straight to Rung 2, unchanged from today. No `affordable()` check: unlike
    # a switch, this never re-sends the transcript, so there is nothing here
    # for the cost cap to guard against beyond the one (cheaper) re-priced turn
    # noted below. Deliberately not gated on `max_switches` either: raising
    # effort spends no switch, so a harness with `max_switches: 0` still gets
    # this rung — it is Rung 2 below, not this one, that a zero limit refuses.
    if (
        reason == REASON_QUALITY
        and state.supports_effort
        # A model with no recorded effort yet (an existing run from before the
        # control existed, or a switch onto a model whose decision predates
        # it) is treated as already at "low" for the rung's own purposes —
        # nothing about that model's own default should read as "already at
        # the top" and skip straight to a switch.
        and (state.effort is None or state.effort in ("low", "medium"))
        and not state.effort_raised
    ):
        current_effort = state.effort or "low"
        next_effort = EFFORT_LEVELS[EFFORT_LEVELS.index(current_effort) + 1]
        return Intervention(
            kind=KIND_EFFORT,
            target=next_effort,
            reason=reason,
            detail=detail,
            evidence={
                "model": state.model.id,
                "from_effort": state.effort,
                "to_effort": next_effort,
                "note": (
                    "same model, no transcript re-send; on Anthropic a "
                    "top-level effort change still invalidates the prompt "
                    "cache, but re-pricing one turn is cheaper than the full "
                    "re-send a model switch forces"
                ),
            },
        )

    # Also where `max_switches: 0` actually bites: 0 switches used is never
    # less than a limit of 0, so this refuses every switch for such a harness
    # without needing its own separate check — the rung above already got its
    # chance regardless of this same limit.
    if state.switches_used >= state.max_switches:
        return Intervention(
            reason=reason,
            detail=detail,
            refused=(
                # A 0 limit is a policy choice ("no model changes, ever"), not
                # a budget this run happened to exhaust — the wording says so
                # rather than reporting "already changed model 0 time(s)",
                # which reads as if a switch had already happened.
                "this harness does not allow model switches"
                if state.max_switches == 0
                else (
                    f"already changed model {state.switches_used} time(s); the limit for this "
                    f"harness is {state.max_switches}"
                )
            ),
        )

    target = choose_target(
        candidates,
        state.model,
        need_larger_window=context_pressure,
        priors=priors,
        # Under on_quality the delivered-rate guard also covers a stall reached
        # after the effort rung: the post-raise grace window means the third
        # terminal failure lands on the stall threshold before the quality
        # delta can, and that switch must not escalate into a model recorded
        # as finishing this shape less often either.
        guard_delivered_rate=(
            reason == REASON_QUALITY
            or (state.escalation == "on_quality" and reason == REASON_STALL)
        ),
    )
    if target is None:
        return Intervention(
            reason=reason,
            detail=detail,
            refused=(
                "no larger-context model is available within this harness's policy"
                if context_pressure
                else "no candidate within this harness's policy has a better record"
            ),
        )
    if not affordable(state, target):
        return Intervention(
            reason=reason,
            detail=detail,
            refused=(
                f"switching to {target.id} would re-send the transcript at full input "
                f"price, which does not fit the ${state.max_cost_usd - state.cost_so_far} "
                "left of this run's cost cap"
            ),
        )

    return Intervention(
        kind=KIND_SWITCH,
        target=target,
        reason=reason,
        detail=detail,
        evidence={
            "from": state.model.id,
            "to": target.id,
            "from_context_window": state.model.context_window,
            "to_context_window": target.context_window,
            "prior": priors[target.id].to_json() if priors and target.id in priors else None,
        },
    )


def normalize_for_provider(messages: list, provider: str) -> list:
    """Adapt a transcript written against one provider for another.

    `Msg`/`ToolCall` is already provider-neutral and both adapters pass
    tool-call ids through verbatim (providers/anthropic.py,
    providers/openai_compat.py), so a mid-run switch needs no rewriting today —
    the new provider simply sees ids it did not issue, which both accept as
    long as every call is paired with its result.

    This exists as the named seam for the day that stops being true, and as the
    place `test_model_switch.py` asserts the pairing invariant that makes a
    switch safe at all. It deliberately returns the same list rather than a copy:
    there is nothing to change, and pretending otherwise would hide that fact.
    """
    return messages
