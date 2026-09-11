"""Routing objectives: what the router is being asked to optimize for.

An objective is set per harness (`model_policy["objective"]`) and threads through
every part of a routing decision: which candidates the router sees first, the
preference rules in the rendered prompt, the deterministic fallback pick, and the
persisted RoutingDecision. `balanced` is the default and is exactly tret's
historical behavior, so existing harnesses route unchanged.

  quality             the most capable candidate within the cost tier; thrift is
                      secondary.
  balanced            the cheapest model that will do the job well, newer first.
  token_conservation   small-but-sufficient models with disciplined output.
  eco                 the least estimated energy per unit of work.
"""
from __future__ import annotations

from collections.abc import Callable

from tret.providers.catalog import ModelInfo
from tret.router_llm.priors_base import ModelPrior

OBJECTIVES = ("quality", "balanced", "token_conservation", "eco")
DEFAULT_OBJECTIVE = "balanced"

# Provider-neutral reasoning-effort levels. Every provider tret speaks to
# exposes its own effort control on a different wire shape (Anthropic's
# `output_config.effort`, OpenRouter's unified `reasoning.effort`) but the same
# three-way idea, so the router reasons in these and the provider layer
# translates — see `providers/base.py` and `ModelInfo.supports_effort`.
EFFORT_LEVELS = ("low", "medium", "high")

# The harness cost ceiling, as a total order. Lives here rather than in
# router.py because both the router and the deterministic fallback must apply
# the *same* ceiling — a cap enforced on only one of the two paths is not a cap.
# "local" ranks below "economy" so a max_cost_tier cap never excludes it: local
# inference is already zero-cost, so there is nothing for a cost ceiling to
# protect. `max_cost_tier: local` is therefore a confidentiality control (no
# cloud provider may be chosen), not a spending one.
TIER_ORDER = {"local": -1, "economy": 0, "standard": 1, "premium": 2}
DEFAULT_MAX_COST_TIER = "premium"


def within_cost_tier(model: ModelInfo, max_tier: str) -> bool:
    """Whether `model` is at or below the ceiling `max_tier` names."""
    return TIER_ORDER.get(model.cost_tier, 2) <= TIER_ORDER.get(max_tier, 2)

# Objectives that rank models by thrift rather than by capability. These ignore
# the curated-first preference (curation survives only as a tiebreak): if an
# uncurated entry really is cheaper or lower-energy, an objective that asks for
# cheap or low-energy must be allowed to pick it.
THRIFT_OBJECTIVES = ("token_conservation", "eco")


# task_shape -> effort under the `balanced` objective. Only shapes with a
# stated reason to differ from "medium" are listed; everything else — including
# a shape this catalog does not recognize — falls through to "medium" below,
# same as `released_rank`'s "unknown last" and the fallback table's own
# `FALLBACK_TABLE.get(task_shape, FALLBACK_TABLE["freeform"])`.
_BALANCED_SHAPE_EFFORT: dict[str, str] = {
    "extraction": "low",  # pulling fields out of a document is not judgment
    "drafting": "medium",
    "freeform": "medium",
    "verdict": "high",  # the hardest, most consequential call a run makes
    "qa_review": "high",  # grading another run's output is itself a judgment
}


def default_effort(objective: str, task_shape: str) -> str:
    """The effort level a policy gets when nothing more specific overrides it.

    Read twice over: it is what the router prompt shows as *this* task's
    default (see `prompts.render_router_prompt`'s EFFORT section), and it is
    what every non-LLM path (override, single-candidate, fallback) records
    outright, since none of those paths ask a router anything.

    The thrift objectives ignore task shape entirely — `token_conservation`
    and `eco` are optimizing for token/energy spend above all else, so the
    hardest verdict gets the same "low" as the simplest extraction; `quality`
    is the mirror image, always "high" regardless of shape, for the same
    reason a quality-first policy always climbs to the most capable candidate
    within its tier rather than reading the task shape as license to spend
    less. Only `balanced` differentiates by shape, via
    `_BALANCED_SHAPE_EFFORT`.
    """
    if objective == "quality":
        return "high"
    if objective in THRIFT_OBJECTIVES:
        return "low"
    return _BALANCED_SHAPE_EFFORT.get(task_shape, "medium")


def objective_of(model_policy: dict | None) -> str:
    """The objective a policy asks for, defaulting to `balanced`.

    Unknown values fall back to the default here — the API validates on write
    (api/harnesses.py), so a bad objective is rejected at the door rather than
    silently steering a run from inside the engine.
    """
    if not model_policy:
        return DEFAULT_OBJECTIVE
    objective = model_policy.get("objective") or DEFAULT_OBJECTIVE
    return objective if objective in OBJECTIVES else DEFAULT_OBJECTIVE


def released_rank(model: ModelInfo) -> int:
    """Sort-key fragment: newer `released` (YYYY-MM) first, unknown last."""
    try:
        year, month = model.released.split("-")[:2]
        return -(int(year) * 12 + int(month))
    except (AttributeError, ValueError):
        return 0


def _out_price(m: ModelInfo):
    # The price in effect today, not the yaml base price: a scheduled
    # `price_changes` entry must reorder candidates the day it takes effect,
    # the same way `cost_usd` bills it.
    return m.prices_at()[1]


def _quality_key(m: ModelInfo) -> tuple:
    # Most capable first, newer breaking ties. The cost-tier cap is applied
    # before sorting, so this can never climb past the harness ceiling.
    return (not m.curated, -_out_price(m), released_rank(m), m.id)


def _token_conservation_key(m: ModelInfo) -> tuple:
    return (_out_price(m), m.energy_wh_per_mtok, not m.curated, m.id)


def _eco_key(m: ModelInfo) -> tuple:
    return (m.energy_wh_per_mtok, _out_price(m), not m.curated, m.id)


def _balanced_key(m: ModelInfo) -> tuple:
    return (not m.curated, _out_price(m))


_OBJECTIVE_KEYS = {
    "quality": _quality_key,
    "token_conservation": _token_conservation_key,
    "eco": _eco_key,
    "balanced": _balanced_key,
}


# ── evidence tiers ───────────────────────────────────────────────────────────
# A model's recorded track record enters candidate ordering as a three-way
# classification, never as a continuous score, and never ahead of what the
# operator asked for *within* a class. The thresholds are absolute because the
# quality scale has known anchors (router_llm/outcomes.py): a run that delivered
# cleanly scores 0.70, one that ended without its verdict scores 0.15, and one
# that failed scores 0.
#
# Reliably good, judged on the conservative lower bound — promotion has to be
# earned under the pessimistic reading, because the cost of being wrong is
# sending every run of this shape to the wrong model.
EVIDENCE_GOOD_FLOOR = 0.65
# Reliably poor, judged on the mean — demotion should not require certainty. A
# model averaging below this is failing, or being rejected by reviewers, more
# often than not.
EVIDENCE_POOR_MEAN = 0.35

TIER_PROVEN = -1  # sorts first
TIER_UNKNOWN = 0
TIER_POOR = 1  # sorts last


def evidence_tier(prior: ModelPrior | None) -> int:
    """Where a model's record places it: proven, no opinion, or poor.

    `None` — no record, or too little of one to qualify — is `TIER_UNKNOWN`, the
    same tier a thoroughly ordinary model gets. That equivalence is the point: an
    untried model must not be ranked last for being untried, or the first model
    to accumulate a record keeps its lead forever and nothing else is ever tried.
    """
    if prior is None:
        return TIER_UNKNOWN
    if prior.quality_ci_low >= EVIDENCE_GOOD_FLOOR:
        return TIER_PROVEN
    if prior.quality_mean <= EVIDENCE_POOR_MEAN:
        return TIER_POOR
    return TIER_UNKNOWN


def candidate_sort_key(
    objective: str, priors: dict[str, ModelPrior] | None = None
) -> Callable[[ModelInfo], tuple]:
    """How an objective orders models, best-first.

    Ordering matters twice over: the LLM router reads its candidate list
    top-down, and the list is truncated, so the tail may never be seen at all.
    The objective therefore has to steer the ordering, not just the prompt rules.

    `balanced` is byte-for-byte the historical ordering — curated first, then
    cheapest — so existing harnesses route exactly as before. Note that price
    stands in for capability throughout: tret has no benchmark score for a
    model, and what a lab charges is the most honest proxy on hand.
    """
    inner = _OBJECTIVE_KEYS.get(objective, _balanced_key)
    if not priors:
        # No evidence: byte-for-byte the ordering tret has always produced. This
        # is the cold-start guarantee — an install with no history routes exactly
        # as it did before any of this existed.
        return inner

    # Evidence leads, the objective decides everything within a tier. Ordering
    # first matters more than it looks: the candidate list is truncated
    # (router.CANDIDATE_LIMIT) and read top-down, so a model that sorts late may
    # never be considered at all — which is how a proven model gets dropped for
    # being expensive under `balanced`, and how a proven-poor one keeps its place.
    return lambda m: (evidence_tier(priors.get(m.id)), *inner(m))
