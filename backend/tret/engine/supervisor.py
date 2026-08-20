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
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from tret.providers.catalog import ModelInfo
from tret.router_llm.objectives import TIER_POOR, evidence_tier
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

# Multiplier on the estimated re-send cost, to leave room for the turn that
# follows it. A switch that exactly fits the remaining budget buys one message.
SWITCH_COST_SAFETY = Decimal("2.0")

# Reasons, recorded on the timeline and the decision.
REASON_CONTEXT = "context_exhausted"
REASON_STALL = "capability_stall"

KIND_NONE = "none"
KIND_SWITCH = "switch"


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


@dataclass
class Intervention:
    kind: str = KIND_NONE
    target: ModelInfo | None = None
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
        Decimal(state.est_wire_tokens) / Decimal(1_000_000) * target.input_price_per_mtok
    )
    return resend * SWITCH_COST_SAFETY <= remaining


def choose_target(
    candidates: list[ModelInfo],
    current: ModelInfo,
    *,
    need_larger_window: bool,
    priors: dict[str, ModelPrior] | None = None,
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
    """
    others = [m for m in candidates if m.id != current.id]
    if need_larger_window:
        larger = [m for m in others if m.context_window > current.context_window]
        return min(larger, key=lambda m: (m.context_window, m.id)) if larger else None

    priors = priors or {}
    current_floor = priors[current.id].quality_ci_low if current.id in priors else 0.0
    better = [
        (priors[m.id].quality_ci_low, m)
        for m in others
        if m.id in priors
        and evidence_tier(priors[m.id]) != TIER_POOR
        and priors[m.id].quality_ci_low > current_floor
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
        if m.id not in priors and m.output_price_per_mtok > current.output_price_per_mtok
    ]
    return min(stronger, key=lambda m: (m.output_price_per_mtok, m.id)) if stronger else None


def assess(
    state: TurnState,
    *,
    candidates: list[ModelInfo],
    priors: dict[str, ModelPrior] | None = None,
) -> Intervention:
    """Whether to change model before the next iteration, and to what."""
    if state.escalation == "off" or state.max_switches <= 0:
        return Intervention()
    if state.overridden:
        # The caller named a model. Running a different one because tret judged
        # it would be better is the substitution the router refuses to make.
        return Intervention()

    context_pressure = _context_exhausted(state)
    stalled, why = _stalled(state)
    if not context_pressure and not stalled:
        return Intervention()

    reason = REASON_CONTEXT if context_pressure else REASON_STALL
    detail = (
        f"over the context budget ({state.est_wire_tokens} > {state.context_limit} est. "
        "tokens) with nothing left to elide"
        if context_pressure
        else why
    )

    if state.switches_used >= state.max_switches:
        return Intervention(
            reason=reason,
            detail=detail,
            refused=(
                f"already changed model {state.switches_used} time(s); the limit for this "
                f"harness is {state.max_switches}"
            ),
        )

    target = choose_target(
        candidates, state.model, need_larger_window=context_pressure, priors=priors
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
