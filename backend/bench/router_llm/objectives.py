"""Routing objectives: what the router is being asked to optimize for.

An objective is set per harness (`model_policy["objective"]`) and threads through
every part of a routing decision: which candidates the router sees first, the
preference rules in the rendered prompt, the deterministic fallback pick, and the
persisted RoutingDecision. `balanced` is the default and is exactly bench's
historical behavior, so existing harnesses route unchanged.

  quality             the most capable candidate within the cost tier; thrift is
                      secondary.
  balanced            the cheapest model that will do the job well, newer first.
  token_conservation   small-but-sufficient models with disciplined output.
  eco                 the least estimated energy per unit of work.
"""
from __future__ import annotations

from collections.abc import Callable

from bench.providers.catalog import ModelInfo
from bench.router_llm.priors import ModelPrior

OBJECTIVES = ("quality", "balanced", "token_conservation", "eco")
DEFAULT_OBJECTIVE = "balanced"

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


def _quality_key(m: ModelInfo) -> tuple:
    # Most capable first, newer breaking ties. The cost-tier cap is applied
    # before sorting, so this can never climb past the harness ceiling.
    return (not m.curated, -m.output_price_per_mtok, released_rank(m), m.id)


def _token_conservation_key(m: ModelInfo) -> tuple:
    return (m.output_price_per_mtok, m.energy_wh_per_mtok, not m.curated, m.id)


def _eco_key(m: ModelInfo) -> tuple:
    return (m.energy_wh_per_mtok, m.output_price_per_mtok, not m.curated, m.id)


def _balanced_key(m: ModelInfo) -> tuple:
    return (not m.curated, m.output_price_per_mtok)


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
    stands in for capability throughout: bench has no benchmark score for a
    model, and what a lab charges is the most honest proxy on hand.
    """
    inner = _OBJECTIVE_KEYS.get(objective, _balanced_key)
    if not priors:
        # No evidence: byte-for-byte the ordering bench has always produced. This
        # is the cold-start guarantee — an install with no history routes exactly
        # as it did before any of this existed.
        return inner

    # Evidence leads, the objective decides everything within a tier. Ordering
    # first matters more than it looks: the candidate list is truncated
    # (router.CANDIDATE_LIMIT) and read top-down, so a model that sorts late may
    # never be considered at all — which is how a proven model gets dropped for
    # being expensive under `balanced`, and how a proven-poor one keeps its place.
    return lambda m: (evidence_tier(priors.get(m.id)), *inner(m))
