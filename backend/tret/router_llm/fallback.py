"""Deterministic fallback when the LLM router fails or is unavailable.

Keyed on the task *shape* every pack task declares. Ordered preference;
first model whose provider has a key *and* is within the harness cost ceiling
wins. The ceiling is not advisory: nothing in this module may return a model the
harness policy excludes, and "nothing qualifies" is answered with None so the
caller can fail loudly.

Recorded evidence has exactly one power on this path: it removes models with a
proven-poor record from contention, and nothing else. It does not promote, and it
does not reorder — the shape table and the objective still decide among whatever
survives. That asymmetry is deliberate. This is the path taken when the LLM
router could not be reached, so it is the path a struggling deployment runs on
most; quietly substituting the router's judgment with a statistic there would
replace one opinion with another under the operator's stated objective. Dropping
a candidate that has demonstrably failed this shape over and over is a weaker
claim, and it is the one worth acting on without a router in the loop.

The shape table encodes the `balanced` objective. Under the thrift objectives
(`token_conservation`, `eco`) the table is not the right answer — it is a list of
capable-first picks — so those objectives rank the *available* catalog by the same
measure the router's candidate ordering uses, and fall back to the table only if
that finds nothing. `quality` keeps the table but climbs it capability-first.
"""
from __future__ import annotations

from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.objectives import (
    DEFAULT_MAX_COST_TIER,
    DEFAULT_OBJECTIVE,
    THRIFT_OBJECTIVES,
    TIER_POOR,
    candidate_sort_key,
    evidence_tier,
    within_cost_tier,
)
from tret.router_llm.priors_base import ModelPrior

FALLBACK_TABLE: dict[str, list[str]] = {
    "verdict": [
        "anthropic/claude-sonnet-5",
        "openrouter/openai/gpt-5.6-terra",
        "openrouter/moonshotai/kimi-k3",
        "kimi/kimi-k2",
    ],
    "extraction": [
        "anthropic/claude-sonnet-5",
        "openrouter/openai/gpt-5.6-terra",
        "openrouter/moonshotai/kimi-k3",
        "kimi/kimi-k2",
    ],
    "drafting": [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-sonnet-5",
        "openrouter/google/gemini-3.6-flash",
    ],
    "qa_review": [
        "anthropic/claude-haiku-4-5",
        "openrouter/google/gemini-3.5-flash-lite",
        "openrouter/deepseek/deepseek-v4-pro",
        "kimi/kimi-k2",
    ],
    "freeform": [
        "anthropic/claude-sonnet-5",
        "openrouter/openai/gpt-5.6-luna",
        "kimi/kimi-k2",
    ],
}


def fallback_model(
    task_shape: str,
    catalog: ModelCatalog,
    registry: ProviderRegistry,
    allowed: list[str] | None = None,
    objective: str = DEFAULT_OBJECTIVE,
    max_cost_tier: str = DEFAULT_MAX_COST_TIER,
    priors: dict[str, ModelPrior] | None = None,
) -> str | None:
    """The model this shape falls back to, or None if the policy permits none.

    `max_cost_tier` is the harness ceiling and is enforced here, not only in the
    router's candidate list: this path runs whenever the LLM router is skipped or
    fails, which on a deployment without a key for the configured router model is
    *every* run. Returning None makes the caller raise `RoutingUnavailable`,
    which is the documented contract for a capped harness with nothing to run —
    see docs/local-models.md. Escalating past the ceiling instead would send
    confidential work to a provider the operator excluded on purpose.
    """
    usable = _usable(catalog, registry, allowed, max_cost_tier)
    if not usable:
        return None
    usable = _drop_proven_poor(usable, priors)

    if objective in THRIFT_OBJECTIVES:
        # Ranking the whole usable catalog *is* the fallback here: walking the
        # shape table first would hand a thrift objective a capability-ordered
        # answer and quietly ignore what the harness asked for.
        return min(usable, key=candidate_sort_key(objective)).id

    usable_ids = {m.id for m in usable}
    table = FALLBACK_TABLE.get(task_shape, FALLBACK_TABLE["freeform"])
    prefs = [m for m in table if m in usable_ids]
    if prefs:
        if objective == "quality":
            # Same shape table, climbed rather than walked: the most capable
            # entry the configured keys allow, not merely the first that works.
            return max(prefs, key=lambda mid: catalog.get(mid).output_price_per_mtok)
        return prefs[0]

    # Last resort: any available model at all — curated cloud models first, but
    # falling through to dynamic/local entries (curated_only=True would hide a
    # local-only installation entirely, since discovered local models are never
    # curated). This is what lets tret route with zero cloud provider keys.
    return min(usable, key=lambda m: (not m.curated, m.id)).id


def _drop_proven_poor(
    usable: list[ModelInfo], priors: dict[str, ModelPrior] | None
) -> list[ModelInfo]:
    """Remove candidates with a proven-poor record — unless that is all there is.

    The guard matters more than the filter. A harness whose every permitted model
    has a bad record still has to run: refusing would turn "these models perform
    badly here" into "this harness is broken", which is a much stronger claim
    than the evidence supports and not one a fallback path should be making. So
    when nothing survives, everything survives, and the caller picks as it always
    did.
    """
    if not priors:
        return usable
    kept = [m for m in usable if evidence_tier(priors.get(m.id)) != TIER_POOR]
    return kept or usable


def _usable(
    catalog: ModelCatalog,
    registry: ProviderRegistry,
    allowed: list[str] | None,
    max_cost_tier: str = DEFAULT_MAX_COST_TIER,
) -> list[ModelInfo]:
    return [
        m
        for m in catalog.all()
        if m.supports_tools
        and registry.has_key(m.provider)
        and (not allowed or m.id in allowed)
        and within_cost_tier(m, max_cost_tier)
    ]
