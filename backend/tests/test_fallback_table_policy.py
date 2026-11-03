"""Policy guard for `router_llm.fallback.FALLBACK_TABLE` (SOC 2 vendor review, 2026-10-19).

Moonshot AI's own API terms permit using Customer Content to improve its models,
and the shipped OpenRouter provider preferences ignore `moonshotai`, so the
deterministic fallback must never route to a Moonshot model through OpenRouter.
The direct, key-gated `kimi/*` entries are the only sanctioned path: they run
only when the operator supplies their own `TRET_MOONSHOT_API_KEY`.
"""
from __future__ import annotations

from tret.config import OPENROUTER_DEFAULT_PROVIDER_PREFS
from tret.router_llm.fallback import FALLBACK_TABLE


def test_no_fallback_entry_routes_moonshot_through_openrouter():
    offenders = [
        (shape, model)
        for shape, models in FALLBACK_TABLE.items()
        for model in models
        if model.startswith("openrouter/moonshotai/")
    ]
    assert offenders == []


def test_direct_moonshot_byok_entry_is_still_a_fallback():
    assert any("kimi/kimi-k2" in models for models in FALLBACK_TABLE.values())


def test_default_provider_prefs_ignore_moonshot():
    assert "moonshotai" in OPENROUTER_DEFAULT_PROVIDER_PREFS["ignore"]
