"""router_llm/families.py: author prefixes, aliases and name hints become family labels."""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.providers.catalog import ModelInfo
from tret.router_llm.families import UNKNOWN_FAMILY, family_of


@pytest.mark.parametrize(
    "mid,family",
    [
        ("openrouter/openai/gpt-5.6-luna", "openai"),
        ("openrouter/meta-llama/llama-4", "meta"),
        ("openrouter/meta/llama-4", "meta"),
        ("openrouter/~anthropic/claude-x", "anthropic"),
        ("openrouter/x-ai/grok-4.5", "xai"),
        ("openrouter/moonshotai/kimi-k3", "moonshot"),
        ("openrouter/mistralai/mistral-large", "mistral"),
        ("openrouter/qwen/qwen3", "alibaba"),
        ("openrouter/z-ai/glm-5", "zhipu"),
        ("openrouter/google/gemini-3.6-flash:free", "google"),
        ("anthropic/claude-sonnet-5", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("kimi-k2-0711-preview", "moonshot"),
        ("local/llama3.1:8b", "meta"),
        ("", UNKNOWN_FAMILY),
        ("kimi/kimi-k2", "moonshot"),
        ("openrouter/bytedance-seed/seed-2", "bytedance"),
        ("local/olmo3", "allenai"),  # not "o3"
        ("local/dolphin3", UNKNOWN_FAMILY),  # not "phi"
        ("local/magistral-small", "mistral"),
        ("local/devstral", "mistral"),
        ("local/codestral:22b", "mistral"),
        ("local/ministral-8b", "mistral"),
        ("local/gpt-oss:20b", "openai"),
        ("local/nemotron-70b", "nvidia"),  # derived models keep the publisher's family
    ],
)
def test_family_of_ids(mid, family):
    assert family_of(mid) == family


def _info(mid, wire, provider):
    return ModelInfo(
        id=mid, provider=provider, wire_id=wire, display_name=wire, context_window=1000,
        input_price_per_mtok=Decimal(1), output_price_per_mtok=Decimal(1), cost_tier="economy",
    )


def test_family_of_model_info_uses_wire_id_then_provider():
    assert family_of(_info("openrouter/openai/x", "openai/x", "openrouter")) == "openai"
    # a direct-provider model has no author in its wire id: the name, then the provider decide
    assert family_of(_info("anthropic/claude-opus-5", "claude-opus-5", "anthropic")) == "anthropic"
    assert family_of(_info("kimi/zzz", "zzz", "kimi")) == "moonshot"
    assert family_of(_info("local/zzz", "zzz", "local")) == UNKNOWN_FAMILY


def test_same_lab_through_two_providers_is_one_family():
    a = _info("anthropic/claude-sonnet-5", "claude-sonnet-5", "anthropic")
    b = _info("openrouter/anthropic/claude-sonnet-5", "anthropic/claude-sonnet-5", "openrouter")
    assert family_of(a) == family_of(b) == "anthropic"


def test_label_does_not_depend_on_input_type():
    assert family_of("kimi/kimi-k2") == family_of(_info("kimi/kimi-k2", "kimi-k2", "kimi")) == "moonshot"
    # a single-lab provider decides before any name hint
    assert family_of(_info("anthropic/phi-x", "phi-x", "anthropic")) == "anthropic"
