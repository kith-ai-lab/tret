"""How well did a run go? — the scorer that turns a finished run into evidence.

bench has always recorded everything needed to judge a routing decision after
the fact (status, transcript, findings, approvals, cost, energy) and has never
read any of it back. This module is the one place that judgment is written down.

Three rules shape it:

* **Quality is not thrift.** `quality_score` deliberately ignores cost, tokens
  and energy. Those are what the routing *objective* already trades off
  (router_llm/objectives.py), and folding them in here would mean an `eco`
  harness could never learn that its cheap model keeps failing. Cost and energy
  are recorded alongside the score, as separate numbers, for the router and the
  analytics view to weigh themselves.
* **The human decision outranks the machine's.** An approved finding is the only
  signal in bench that someone with domain judgment looked at the output and
  said it was right. Where approvals exist they dominate; where they do not, the
  automatic signals stand alone rather than being padded out with a guess.
* **The weights are versioned.** `OUTCOME_SCORE_VERSION` is persisted with every
  score. Re-weighting later produces *new* scores under a new version instead of
  silently rewriting what past runs were judged to be worth.

A cancelled run is not scored at all (see `score_run`): an operator stopping a
run says nothing about the model, and counting it as a failure would teach the
router to avoid whichever model people happen to interrupt.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from bench.services.transcript import TranscriptSignals, read_signals

OUTCOME_SCORE_VERSION = "outcome-v1"

# ── outcome classes ──────────────────────────────────────────────────────────
# What kind of ending this was, before any of the finer signals are weighed.
DELIVERED = "delivered"  # ran to completion and produced what the task required
NO_OUTPUT = "no_output"  # `completed_without_output`: guardrails won, task did not
FAILED = "failed"
UNSCORED = "unscored"  # cancelled, or still running — carries no lesson

BASE_SCORE: dict[str, float] = {
    DELIVERED: 0.70,
    # Not zero. The run ended honestly and the guardrails held; what it did not
    # do is land the verdict the task asked for. Scoring it as a flat failure
    # would rank it with a provider outage, which is a different thing entirely.
    NO_OUTPUT: 0.15,
    FAILED: 0.0,
}

# ── why a run failed ─────────────────────────────────────────────────────────
# All score 0, but they are not the same lesson, and the analytics view must be
# able to separate "this model answers badly" from "this provider was down".
ERROR_KINDS = (
    "cost_cap_exceeded",
    "output_budget_exceeded",
    "max_iterations",
    "unknown_task_type",
    "unknown_tool",
    "routing_unavailable",
    "provider_error",
    "engine_error",
)
_ERROR_PREFIXES = (
    ("cost_cap_exceeded", "cost_cap_exceeded"),
    ("output_budget_exceeded", "output_budget_exceeded"),
    ("max_iterations", "max_iterations"),
    ("unknown_task_type", "unknown_task_type"),
    ("unknown_tool", "unknown_tool"),
)

# ── penalties ────────────────────────────────────────────────────────────────
# Each is (per-event cost, maximum total). Every one of these is the engine
# having to work around the model, so each is evidence the route was wrong.
PENALTY_VALIDATION_ERROR = (0.05, 0.25)
PENALTY_UNRECOVERED_VALIDATION = (0.10, 0.20)  # repair budget spent, on top
PENALTY_TOOL_ERROR = (0.02, 0.10)  # non-validation tool failures
PENALTY_REPEATED_CALL = (0.03, 0.09)  # the retrieval-loop breaker fired
PENALTY_TERMINAL_NUDGE = 0.05  # had to be told to record its result
PENALTY_BUDGET_NUDGE = 0.03  # had to be told to stop

# Iterations are penalised only past this fraction of the harness's own ceiling:
# a run that used most of its budget was struggling, but a run that used half of
# a generous budget was not.
ITERATION_FREE_FRACTION = 0.6
PENALTY_ITERATIONS_MAX = 0.15

# How far the human verdict outweighs the automatic signals when one exists.
HUMAN_WEIGHT = 0.6
# Laplace smoothing on the approval rate, so a single approval reads as
# "probably good" (0.67) rather than "certainly perfect" (1.0).
HUMAN_PRIOR_SUCCESSES = 1.0
HUMAN_PRIOR_TRIALS = 2.0


def _capped(count: int, per_event: float, cap: float) -> float:
    return min(count * per_event, cap)


def error_kind(status: str, error: str | None) -> str | None:
    """Which failure this was, from the error string the engine wrote.

    The engine's failures are prefixed (`cost_cap_exceeded: ...`) except for
    provider and engine faults, which arrive as `[provider] message` and
    `TypeError: ...` respectively — so those two are told apart by shape rather
    than by prefix, and anything unrecognised is `engine_error` rather than a
    silent None that would vanish from the analytics split.
    """
    if status != "failed":
        return None
    text = (error or "").strip()
    for prefix, kind in _ERROR_PREFIXES:
        if text.startswith(prefix):
            return kind
    if "no candidate models" in text.lower() or "cost ceiling" in text.lower():
        return "routing_unavailable"
    if text.startswith("["):  # ProviderError renders as "[provider] message"
        return "provider_error"
    return "engine_error"


@dataclass
class OutcomeScore:
    """A run's scorecard: the class, the number, and how the number was reached.

    `components` is not decoration. A score with no derivation is unarguable,
    and this one steers which model gets chosen next time — an operator looking
    at a model's poor track record has to be able to see *what* it was penalised
    for.
    """

    outcome_class: str
    quality_score: float
    score_version: str = OUTCOME_SCORE_VERSION
    error_kind: str | None = None
    components: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        return asdict(self)


def _automatic_score(base: float, signals: TranscriptSignals, iterations: int, max_iterations: int
                     ) -> tuple[float, dict]:
    """The score from the engine's own observations, plus the itemised penalties."""
    penalties = {
        "validation_errors": _capped(signals.validation_errors, *PENALTY_VALIDATION_ERROR),
        "unrecovered_validation_errors": _capped(
            signals.unrecovered_validation_errors, *PENALTY_UNRECOVERED_VALIDATION
        ),
        "tool_errors": _capped(signals.non_validation_tool_errors, *PENALTY_TOOL_ERROR),
        "repeated_calls": _capped(signals.repeated_call_trips, *PENALTY_REPEATED_CALL),
        "terminal_nudge": PENALTY_TERMINAL_NUDGE if signals.terminal_nudged else 0.0,
        "output_budget_nudge": PENALTY_BUDGET_NUDGE if signals.output_budget_nudged else 0.0,
        "iteration_pressure": _iteration_penalty(iterations, max_iterations),
    }
    total = sum(penalties.values())
    return max(0.0, base - total), {k: round(v, 4) for k, v in penalties.items() if v}


def _iteration_penalty(iterations: int, max_iterations: int) -> float:
    """How hard the run had to push against its own iteration ceiling."""
    if not max_iterations or iterations <= 0:
        return 0.0
    used = iterations / max_iterations
    if used <= ITERATION_FREE_FRACTION:
        return 0.0
    over = (used - ITERATION_FREE_FRACTION) / (1.0 - ITERATION_FREE_FRACTION)
    return round(PENALTY_ITERATIONS_MAX * min(1.0, over), 4)


def _human_rate(approved: int, rejected: int) -> float:
    return (approved + HUMAN_PRIOR_SUCCESSES) / (approved + rejected + HUMAN_PRIOR_TRIALS)


def score(
    *,
    status: str,
    error: str | None,
    messages: list,
    iterations: int,
    max_iterations: int,
    findings_approved: int = 0,
    findings_rejected: int = 0,
) -> OutcomeScore:
    """Score one finished run. Pure: no database, no clock, no catalog."""
    if status == "completed":
        outcome_class = DELIVERED
    elif status == "completed_without_output":
        outcome_class = NO_OUTPUT
    elif status == "failed":
        outcome_class = FAILED
    else:  # queued | running | cancelled
        return OutcomeScore(UNSCORED, 0.0, components={"reason": f"status '{status}'"})

    signals = read_signals(messages)
    auto, penalties = _automatic_score(
        BASE_SCORE[outcome_class], signals, iterations, max_iterations
    )
    components: dict = {
        "base": BASE_SCORE[outcome_class],
        "penalties": penalties,
        "automatic_score": round(auto, 4),
        "signals": asdict(signals),
    }

    quality = auto
    decisions = findings_approved + findings_rejected
    if decisions:
        # A human looked at this. Their verdict leads; the engine's observations
        # still count, because a finding can be approved despite a messy run and
        # the mess is still evidence about the model.
        rate = _human_rate(findings_approved, findings_rejected)
        quality = HUMAN_WEIGHT * rate + (1.0 - HUMAN_WEIGHT) * auto
        components["human"] = {
            "approved": findings_approved,
            "rejected": findings_rejected,
            "smoothed_rate": round(rate, 4),
            "weight": HUMAN_WEIGHT,
        }

    return OutcomeScore(
        outcome_class=outcome_class,
        quality_score=round(min(1.0, max(0.0, quality)), 4),
        error_kind=error_kind(status, error),
        components=components,
    )


# ── input-size bands ─────────────────────────────────────────────────────────
# Evidence is bucketed by how much input the run carried, because "which model
# is best" is not a single question: a model that handles a 2k-token chat turn
# well may be the wrong answer for a 300k-token synthesis. Comparing across
# bands is the main way an aggregate like this misleads.
SIZE_BANDS: tuple[tuple[str, int], ...] = (
    ("xs", 4_000),
    ("s", 16_000),
    ("m", 64_000),
    ("l", 256_000),
    ("xl", 2**63 - 1),
)


def size_band(est_input_tokens: int) -> str:
    for name, ceiling in SIZE_BANDS:
        if est_input_tokens < ceiling:
            return name
    return SIZE_BANDS[-1][0]
