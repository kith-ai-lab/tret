"""Deterministic fallback when the LLM router fails or is unavailable.

Keyed on the task *shape* every pack task declares. Ordered preference;
first model whose provider has a key wins.
"""
from __future__ import annotations

from bench.providers.catalog import ModelCatalog, ProviderRegistry

FALLBACK_TABLE: dict[str, list[str]] = {
    "verdict": [
        "anthropic/claude-sonnet-5",
        "kimi/kimi-k2",
        "openrouter/openai/gpt-4.1",
    ],
    "extraction": [
        "anthropic/claude-sonnet-5",
        "kimi/kimi-k2",
        "openrouter/openai/gpt-4.1",
    ],
    "drafting": [
        "anthropic/claude-opus-4-8",
        "anthropic/claude-sonnet-5",
        "openrouter/google/gemini-2.5-pro",
    ],
    "qa_review": [
        "anthropic/claude-haiku-4-5",
        "kimi/kimi-k2",
        "openrouter/deepseek/deepseek-chat-v3",
    ],
    "freeform": [
        "anthropic/claude-sonnet-5",
        "kimi/kimi-k2",
        "openrouter/openai/gpt-4.1",
    ],
}


def fallback_model(
    task_shape: str,
    catalog: ModelCatalog,
    registry: ProviderRegistry,
    allowed: list[str] | None = None,
) -> str | None:
    prefs = FALLBACK_TABLE.get(task_shape, FALLBACK_TABLE["freeform"])
    for model_id in prefs:
        info = catalog.get(model_id)
        if info is None or not registry.has_key(info.provider):
            continue
        if allowed and model_id not in allowed:
            continue
        return model_id
    # Last resort: any available model at all.
    for info in catalog.all(curated_only=True):
        if registry.has_key(info.provider) and (not allowed or info.id in allowed):
            return info.id
    return None
