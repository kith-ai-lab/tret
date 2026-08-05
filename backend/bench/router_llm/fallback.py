"""Deterministic fallback when the LLM router fails or is unavailable.

Keyed on the task *shape* every pack task declares. Ordered preference;
first model whose provider has a key wins.

The shape table encodes the `balanced` objective. Under the thrift objectives
(`token_conservation`, `eco`) the table is not the right answer — it is a list of
capable-first picks — so those objectives rank the *available* catalog by the same
measure the router's candidate ordering uses, and fall back to the table only if
that finds nothing. `quality` keeps the table but climbs it capability-first.
"""
from __future__ import annotations

from bench.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from bench.router_llm.objectives import (
    DEFAULT_OBJECTIVE,
    THRIFT_OBJECTIVES,
    candidate_sort_key,
)

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
) -> str | None:
    usable = _usable(catalog, registry, allowed)
    if not usable:
        return None

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
    # curated). This is what lets bench route with zero cloud provider keys.
    return min(usable, key=lambda m: (not m.curated, m.id)).id


def _usable(
    catalog: ModelCatalog, registry: ProviderRegistry, allowed: list[str] | None
) -> list[ModelInfo]:
    return [
        m
        for m in catalog.all()
        if m.supports_tools
        and registry.has_key(m.provider)
        and (not allowed or m.id in allowed)
    ]
