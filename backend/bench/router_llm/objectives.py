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


def candidate_sort_key(objective: str) -> Callable[[ModelInfo], tuple]:
    """How an objective orders models, best-first.

    Ordering matters twice over: the LLM router reads its candidate list
    top-down, and the list is truncated, so the tail may never be seen at all.
    The objective therefore has to steer the ordering, not just the prompt rules.

    `balanced` is byte-for-byte the historical ordering — curated first, then
    cheapest — so existing harnesses route exactly as before. Note that price
    stands in for capability throughout: bench has no benchmark score for a
    model, and what a lab charges is the most honest proxy on hand.
    """
    if objective == "quality":
        # Most capable first, newer breaking ties. The cost-tier cap is applied
        # before sorting, so this can never climb past the harness ceiling.
        return lambda m: (not m.curated, -m.output_price_per_mtok, released_rank(m), m.id)
    if objective == "token_conservation":
        return lambda m: (m.output_price_per_mtok, m.energy_wh_per_mtok, not m.curated, m.id)
    if objective == "eco":
        return lambda m: (m.energy_wh_per_mtok, m.output_price_per_mtok, not m.curated, m.id)
    return lambda m: (not m.curated, m.output_price_per_mtok)
