"""Offline tests for the public SDK surface: `from tret import Router`.

No network, no DB. A single stub `Provider` scripts both the router's own
`complete_json` call (the LLM routing decision) and the executed `stream()`
call, and is handed out by a fake registry. A fake catalog is seeded with
`ModelInfo` entries built directly — no dependency on models.yaml — and
injected by monkeypatching the two factories `tret.sdk.Router` calls lazily
(`get_catalog`, `ProviderRegistry`), the same seam a real caller would swap
for a test double.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from decimal import Decimal

import pytest

import tret
import tret.sdk as sdk_module
from tret.providers.base import (
    JsonCompletion,
    Msg,
    Provider,
    ProviderError,
    ProviderEvent,
    TextDelta,
    ToolCall,
    ToolCallComplete,
    TurnComplete,
    Usage,
)
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.router import RoutingUnavailable
from tret.sdk import Receipt, Router, RunResult
from tret.services.emissions import energy_accounting

# ── fixtures: a fake catalog, a fake registry, a scripted provider ─────────

ROUTER_MODEL_ID = "anthropic/claude-haiku-4-5"  # matches Settings.router_model default


def _model(id_: str, **over) -> ModelInfo:
    base = dict(
        id=id_,
        provider="anthropic",
        wire_id=id_.split("/", 1)[1],
        display_name=id_,
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class="M",
    )
    base.update(over)
    return ModelInfo(**base)


ROUTER_MODEL = _model(ROUTER_MODEL_ID, cost_tier="economy", energy_class="S")
TARGET_MODEL = _model("anthropic/claude-sonnet-5")  # what the stub router picks
OTHER_MODEL = _model(
    "anthropic/claude-opus-4-8",
    input_price_per_mtok=Decimal("15"),
    output_price_per_mtok=Decimal("75"),
    cost_tier="premium",
    energy_class="L",
)


class FakeCatalog(ModelCatalog):
    """A catalog seeded with fixed entries. Never touches models.yaml or the
    network: `warm_once()` is made a no-op by pre-marking the catalog warmed."""

    def __init__(self, models: list[ModelInfo]) -> None:
        self._static = {m.id: m for m in models}
        self._dynamic: dict[str, ModelInfo] = {}
        self._dynamic_fetched_at = 0.0
        self._local: dict[str, ModelInfo] = {}
        self._local_fetched_at = 0.0
        self._tool_probe_cache: dict[str, bool] = {}
        self._warmed = True


class FakeRegistry(ProviderRegistry):
    """Hands out one scripted provider for every provider name."""

    def __init__(self, provider: Provider, *, keyed: bool = True) -> None:
        self._provider = provider
        self._keyed = keyed

    def has_key(self, provider: str) -> bool:
        return self._keyed

    def get(self, provider: str) -> Provider:
        return self._provider


@dataclass
class StubProvider(Provider):
    """Scripts the router's `complete_json` pick and the executed `stream()`."""

    name: str = "stub"
    chosen_model_id: str = TARGET_MODEL.id
    reasoning: str = "picked for the test"
    routing_usage: Usage = field(default_factory=lambda: Usage(input_tokens=50, output_tokens=10))
    text_chunks: tuple[str, ...] = ("Hello, ", "world.")
    run_usage: Usage = field(default_factory=lambda: Usage(input_tokens=123, output_tokens=45))
    stop_reason: str = "end_turn"
    fail_complete_json: bool = False
    # A local OpenAI-compat server that ignores stream_options.include_usage
    # never sends a usage-bearing turn-complete event at all.
    emit_turn_complete: bool = True
    # Scripts a stray tool call even though the SDK offers no tools — must be
    # ignored, not acted on.
    emit_unexpected_tool_call: bool = False
    complete_json_calls: list[dict] = field(default_factory=list)
    stream_calls: list[dict] = field(default_factory=list)

    async def complete_json(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        schema: dict,
        tool_name: str = "respond",
        max_tokens: int = 1024,
        timeout: float = 30.0,
    ) -> JsonCompletion:
        self.complete_json_calls.append({"model": model, "prompt": prompt, "schema": schema})
        if self.fail_complete_json:
            raise ProviderError("stub", "routing model unreachable")
        return JsonCompletion(
            payload={
                "model_id": self.chosen_model_id,
                "reasoning": self.reasoning,
                "confidence": "high",
            },
            usage=self.routing_usage,
            model=model,
        )

    async def stream(
        self,
        *,
        model: str,
        system: str,
        messages: list[Msg],
        tools: list,
        max_tokens: int,
        temperature: float,
    ) -> AsyncIterator[ProviderEvent]:
        self.stream_calls.append(
            {"model": model, "system": system, "messages": messages, "tools": tools}
        )
        for chunk in self.text_chunks:
            yield TextDelta(chunk)
        if self.emit_unexpected_tool_call:
            yield ToolCallComplete(ToolCall(id="call-1", name="not_offered", arguments={}))
        if self.emit_turn_complete:
            yield TurnComplete(usage=self.run_usage, stop_reason=self.stop_reason)


@pytest.fixture
def wired(monkeypatch):
    """Points `tret.sdk.Router`'s lazy wiring at fakes instead of real ones."""
    catalog = FakeCatalog([ROUTER_MODEL, TARGET_MODEL, OTHER_MODEL])
    provider = StubProvider()
    registry = FakeRegistry(provider)
    monkeypatch.setattr(sdk_module, "get_catalog", lambda: catalog)
    monkeypatch.setattr(sdk_module, "ProviderRegistry", lambda: registry)
    return catalog, registry, provider


# ── 1. arun: scripted text + a receipt that matches direct computation ─────


async def test_arun_returns_scripted_text_and_matching_receipt(wired):
    catalog, registry, provider = wired
    router = Router()

    result = await router.arun("summarize this document for me")

    assert isinstance(result, RunResult)
    assert result.text == "Hello, world."
    assert result.model == TARGET_MODEL.id
    assert result.stop_reason == "end_turn"

    # The routed model actually executed, with no tools offered.
    assert provider.stream_calls[-1]["model"] == TARGET_MODEL.wire_id
    assert provider.stream_calls[-1]["tools"] == []
    assert provider.stream_calls[-1]["messages"][0].content == "summarize this document for me"

    receipt = result.receipt
    assert isinstance(receipt, Receipt)
    expected_usd = float(TARGET_MODEL.cost_usd(123, 45, 0, 0))
    assert receipt.usd == expected_usd

    expected_accounting = energy_accounting(TARGET_MODEL, 123, 45, 0, 0, catalog=catalog)
    assert receipt.co2e_g == expected_accounting["co2e_g"]
    assert receipt.energy_wh == expected_accounting["energy_wh"]
    assert receipt.raw == expected_accounting
    assert receipt.raw["baseline"] == expected_accounting["baseline"]
    assert receipt.baseline_model == expected_accounting["baseline"]["model"]
    assert receipt.avoided_co2e_g == expected_accounting["baseline"]["avoided_co2e_g"]
    assert receipt.avoided_co2e_pct == expected_accounting["baseline"]["avoided_pct"]
    assert receipt.avoided_usd == expected_accounting["cost"]["avoided_usd"]
    assert receipt.avoided_usd_pct == expected_accounting["cost"]["avoided_pct"]

    assert receipt.usage == {
        "input_tokens": 123,
        "output_tokens": 45,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    assert receipt.routing["chosen_model"] == TARGET_MODEL.id
    assert receipt.routing["reasoning"] == "picked for the test"
    assert receipt.routing["fallback_used"] is False
    assert receipt.routing["objective"] == "balanced"
    assert TARGET_MODEL.id in receipt.routing["candidates"]
    assert receipt.overhead is not None  # the router's own LLM call was metered
    assert receipt.overhead["kind"] == "routing"
    assert receipt.overhead["model"] == ROUTER_MODEL.id
    assert receipt.overhead["cost_usd"] is not None

    text = str(receipt)
    assert text.startswith("receipt")
    assert f"${receipt.usd:.4f}" in text
    assert "claude-sonnet-5" in text
    # The routing overhead is surfaced in the headline, keyed off cost_usd
    # (overhead_call's key), not "usd".
    assert f"(+${receipt.overhead['cost_usd']:.4f} routing)" in text


# ── 2. a pinned model executes that model, bypassing the LLM router ────────


async def test_pinned_model_executes_that_model(wired):
    catalog, registry, provider = wired
    router = Router(model=OTHER_MODEL.id)

    result = await router.arun("do something")

    assert result.model == OTHER_MODEL.id
    assert provider.complete_json_calls == []  # pinned mode never consults a router
    assert provider.stream_calls[-1]["model"] == OTHER_MODEL.wire_id
    assert result.receipt.overhead is None  # no router call, nothing to meter
    assert result.receipt.routing["candidates"] == [OTHER_MODEL.id]


# ── 3. sync wrappers ────────────────────────────────────────────────────────


def test_run_works_from_sync_context(wired):
    router = Router()
    result = router.run("summarize x")
    assert result.text == "Hello, world."
    assert result.model == TARGET_MODEL.id


def test_route_works_from_sync_context(wired):
    router = Router()
    decision = router.route("summarize x")
    assert decision.chosen_model == TARGET_MODEL.id


async def test_run_inside_a_running_loop_raises(wired):
    router = Router()
    with pytest.raises(RuntimeError, match="cannot be called from a running event loop"):
        router.run("summarize x")


# ── 4. router LLM unavailable → deterministic fallback still completes ─────


async def test_router_llm_failure_falls_back_deterministically(wired):
    catalog, registry, provider = wired
    provider.fail_complete_json = True
    router = Router()

    decision = await router.aroute("summarize x")
    assert decision.fallback_used is True
    # The shape table's first "freeform" preference that is actually in the
    # catalog and keyed — which happens to be TARGET_MODEL here.
    assert decision.chosen_model == TARGET_MODEL.id

    result = await router.arun("summarize x")
    assert result.model == TARGET_MODEL.id
    assert result.text == "Hello, world."
    assert result.receipt.routing["fallback_used"] is True


# ── 5. null carbon propagates as None, never 0, and __str__ omits it ───────


async def test_none_carbon_propagates_and_str_omits_it(wired, monkeypatch):
    catalog, registry, provider = wired

    def _null_carbon_accounting(model, input_tokens, output_tokens,
                                 cache_read_tokens=0, cache_write_tokens=0,
                                 grid_g_per_kwh=None, *, settings=None, catalog=None):
        return {
            "model": model.id,
            "co2e_g": None,
            "energy_wh": None,
            "cost": {"usd": 0.05, "avoided_usd": None, "avoided_pct": None},
            "baseline": {"model": None, "avoided_co2e_g": None, "avoided_pct": None},
        }

    monkeypatch.setattr(sdk_module, "energy_accounting", _null_carbon_accounting)

    router = Router()
    result = await router.arun("summarize x")

    receipt = result.receipt
    assert receipt.co2e_g is None
    assert receipt.energy_wh is None
    assert receipt.baseline_model is None
    assert receipt.avoided_co2e_g is None
    assert receipt.avoided_co2e_pct is None
    assert receipt.avoided_usd is None
    assert receipt.avoided_usd_pct is None
    # usd is computed independently of energy_accounting, so it still shows up.
    assert receipt.usd is not None

    text = str(receipt)
    assert "gCO₂e" not in text
    assert f"${receipt.usd:.4f}" in text


# ── 6. the public import path ───────────────────────────────────────────────


def test_public_import_path():
    assert tret.Router is Router
    assert tret.Receipt is Receipt
    assert tret.RunResult is RunResult


# ── 7. max_cost_tier / objective are validated at construction ─────────────
# A typo'd max_cost_tier used to normalize silently to "premium" — the *top*
# tier — inside router_llm.router._max_cost_tier, which is a confidentiality
# boundary (docs/local-models.md) failing open. Router.__init__ must reject
# it instead of ever handing it to that permissive normalization.


def test_invalid_max_cost_tier_raises_value_error():
    with pytest.raises(ValueError, match="max_cost_tier"):
        Router(max_cost_tier="Local")  # miscased: not the same string as "local"


def test_unknown_max_cost_tier_raises_value_error():
    with pytest.raises(ValueError, match="max_cost_tier"):
        Router(max_cost_tier="not-a-tier")


def test_invalid_objective_raises_value_error():
    with pytest.raises(ValueError, match="objective"):
        Router(objective="bogus")


def test_valid_local_tier_passes_through_to_policy():
    router = Router(max_cost_tier="local")
    assert router._model_policy()["max_cost_tier"] == "local"


def test_default_construction_still_valid():
    # Defaults must themselves pass the new validation.
    router = Router()
    policy = router._model_policy()
    assert policy["max_cost_tier"] in {"local", "economy", "standard", "premium"}
    assert policy["objective"] in {"quality", "balanced", "token_conservation", "eco"}


# ── 8. RoutingUnavailable propagates out of arun, unwrapped ────────────────


async def test_routing_unavailable_propagates_from_arun(wired):
    catalog, registry, provider = wired
    registry._keyed = False  # no provider has a key anywhere -> no candidates
    router = Router()

    with pytest.raises(RoutingUnavailable):
        await router.arun("summarize x")


# ── 9. no usage reported → an honest, unpriced receipt ─────────────────────


async def test_missing_turn_complete_yields_an_unpriced_receipt(wired):
    catalog, registry, provider = wired
    provider.emit_turn_complete = False  # server never reports usage at all
    router = Router()

    result = await router.arun("summarize x")

    assert result.text == "Hello, world."  # the text still streamed normally
    receipt = result.receipt
    assert receipt.usd is None
    assert receipt.co2e_g is None
    assert receipt.energy_wh is None
    assert receipt.baseline_model is None
    assert receipt.avoided_usd is None
    assert receipt.avoided_usd_pct is None
    assert receipt.avoided_co2e_g is None
    assert receipt.avoided_co2e_pct is None
    assert str(receipt) == "receipt · estimate unavailable · claude-sonnet-5"


async def test_all_zero_usage_also_yields_an_unpriced_receipt(wired):
    catalog, registry, provider = wired
    provider.run_usage = Usage()  # TurnComplete arrives, but with nothing in it
    router = Router()

    result = await router.arun("summarize x")

    assert result.receipt.usd is None
    assert result.receipt.co2e_g is None


# ── 10. a stray tool call, despite offering none, is safely ignored ────────


async def test_unexpected_tool_call_is_ignored(wired):
    catalog, registry, provider = wired
    provider.emit_unexpected_tool_call = True
    router = Router()

    result = await router.arun("summarize x")

    assert result.text == "Hello, world."  # unaffected by the stray tool call
    assert result.stop_reason == "end_turn"
    assert result.receipt.usd is not None  # TurnComplete still arrived normally


# ── 11. raw / overhead are independent copies, not aliases ─────────────────


async def test_raw_and_overhead_are_deep_copies_not_aliases(wired):
    catalog, registry, provider = wired
    router = Router()
    decision = await router.aroute("summarize x")
    assert decision.spend is not None  # the LLM router ran and was metered

    original_cost = decision.spend["cost_usd"]
    usage = Usage(input_tokens=10, output_tokens=5)
    receipt1 = sdk_module._build_receipt(TARGET_MODEL, usage, decision, catalog, True)

    receipt1.overhead["cost_usd"] = -999
    receipt1.raw["co2e_g"] = -999

    # Mutating a receipt must not corrupt the RoutingDecision it was built from.
    assert decision.spend["cost_usd"] == original_cost

    receipt2 = sdk_module._build_receipt(TARGET_MODEL, usage, decision, catalog, True)
    assert receipt2.overhead["cost_usd"] == original_cost
    assert receipt2.raw["co2e_g"] != -999
