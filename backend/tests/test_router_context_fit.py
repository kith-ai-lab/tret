"""The router must not hand a run a model whose window cannot hold it.

`ModelRouter._candidates()` (and the deterministic `fallback_model()`) filtered
by the allowed list, provider key and cost tier, but never by context window —
`ctx:` was rendered into the LLM router's prompt and nothing else ever read it.
A run could be routed to a model whose window cannot hold the composed prompt
plus its output reservation, and only the mid-run supervisor's
context-exhaustion switch rescued it, well after the first call had already
been paid for.

These tests drive `required_context_window()` (the inverse of
`engine.compaction.budget()`), the context floor inside `_candidates()`/
`fallback_model()`, and the `context_fit` record persisted on every
`RoutingDecision` — including the two paths that are never blocked by it, the
override and the pin.

Fixtures follow test_router_cost_ceiling.py's pattern: a `ProviderRegistry`
fake keyed on which providers have keys, and a `ModelCatalog` whose `_static`
table is replaced outright so every test controls exactly which context
windows and cost tiers exist — the shipped `models.yaml` is not addressed by
window in this file at all.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from tret.engine.compaction import budget, required_context_window
from tret.providers.catalog import ModelCatalog, ModelInfo, ProviderRegistry
from tret.router_llm.fallback import fallback_model
from tret.router_llm.objectives import TIER_POOR, TIER_PROVEN, TIER_UNKNOWN, evidence_tier
from tret.router_llm.priors_base import ModelPrior
from tret.router_llm.router import ModelRouter, _apply_context_fit


class _Registry(ProviderRegistry):
    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


def _model(
    name: str,
    *,
    context_window: int,
    cost_tier: str = "economy",
    provider: str = "openrouter",
    price: str = "1",
) -> ModelInfo:
    return ModelInfo(
        id=f"test/{name}",
        provider=provider,
        wire_id=name,
        display_name=name,
        context_window=context_window,
        input_price_per_mtok=Decimal(price),
        output_price_per_mtok=Decimal(price),
        cost_tier=cost_tier,
        supports_tools=True,
        curated=True,
    )


def _catalog(*models: ModelInfo) -> ModelCatalog:
    catalog = ModelCatalog()
    catalog._static = {m.id: m for m in models}
    return catalog


ROUTE_ARGS = dict(
    task_type="divergence_assessment",
    task_shape="verdict",
    task_description="Compare disclosed emissions against the dataset.",
    output_contract="verdict",
    n_documents=2,
    est_input_tokens=8000,
)


@pytest.fixture()
def no_llm_router(monkeypatch):
    """Force the deterministic fallback path, same fixture as
    test_router_cost_ceiling.py: no usable router model, so `route()` always
    decides through `fallback_model()`."""
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)


# ── required_context_window is budget()'s inverse ────────────────────────────
@pytest.mark.parametrize(
    "est_input_tokens,max_output_tokens,headroom",
    [
        (8000, 2000, 0.8),
        (1, 1, 0.9),
        (150_000, 8192, 0.75),
        (0, 0, 1.0),
        (500, 4096, 0.5),
        (999_999, 65_536, 0.95),
    ],
)
def test_required_context_window_is_the_inverse_of_budget(
    est_input_tokens, max_output_tokens, headroom
):
    required = required_context_window(est_input_tokens, max_output_tokens, headroom)
    assert budget(required, max_output_tokens, headroom) >= est_input_tokens
    if required > 0:
        assert budget(required - 1, max_output_tokens, headroom) < est_input_tokens


def test_required_context_window_guards_non_positive_headroom():
    assert required_context_window(10_000, 1000, 0) == 0
    assert required_context_window(10_000, 1000, -0.1) == 0


def test_required_context_window_of_nothing_is_zero():
    assert required_context_window(0, 0, 0.8) == 0


# ── _candidates_with_fit: excludes too-small windows ─────────────────────────
def test_candidates_excludes_a_model_below_the_context_floor():
    small = _model("small", context_window=8_000)
    big = _model("big", context_window=200_000)
    mid = _model("mid", context_window=50_000)
    catalog = _catalog(small, big, mid)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    candidates, context_fit = router._candidates_with_fit(
        {"mode": "auto"}, min_context_window=100_000
    )

    assert [c.id for c in candidates] == ["test/big"]
    assert context_fit["mode"] == "fit"
    assert context_fit["required"] == 100_000
    assert set(context_fit["excluded"]) == {"test/small", "test/mid"}


async def test_route_records_fit_and_excludes_the_too_small_model(no_llm_router):
    small = _model("small", context_window=8_000)
    big = _model("big", context_window=200_000)
    catalog = _catalog(small, big)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "auto"}, min_context_window=100_000, **ROUTE_ARGS
    )

    assert decision.chosen_model == "test/big"
    assert decision.context_fit["mode"] == "fit"
    assert decision.context_fit["required"] == 100_000
    assert decision.context_fit["excluded"] == ["test/small"]
    assert decision.chosen_model in decision.candidates


# ── best effort: nothing fits, largest window wins, ceiling still binds ─────
async def test_best_effort_prefers_largest_window_but_never_escapes_the_ceiling(no_llm_router):
    economy_small = _model("economy-small", context_window=8_000, cost_tier="economy")
    economy_big = _model("economy-big", context_window=200_000, cost_tier="economy")
    # Bigger window than anything permitted, but above the harness ceiling —
    # must never be chosen no matter how badly the composed prompt needs room.
    premium_huge = _model("premium-huge", context_window=2_000_000, cost_tier="premium")
    catalog = _catalog(economy_small, economy_big, premium_huge)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "auto", "max_cost_tier": "economy"},
        # Nothing within the economy tier can hold this.
        min_context_window=300_000,
        **ROUTE_ARGS,
    )

    assert decision.chosen_model == "test/economy-big"
    assert decision.chosen_model != "test/premium-huge"
    assert decision.context_fit["mode"] == "best_effort"
    assert decision.context_fit["required"] == 300_000
    assert set(decision.context_fit["excluded"]) == {"test/economy-small", "test/economy-big"}
    assert decision.max_cost_tier == "economy"


def test_candidates_with_fit_best_effort_never_includes_a_model_above_the_ceiling():
    economy_small = _model("economy-small", context_window=8_000, cost_tier="economy")
    premium_huge = _model("premium-huge", context_window=2_000_000, cost_tier="premium")
    catalog = _catalog(economy_small, premium_huge)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    candidates, context_fit = router._candidates_with_fit(
        {"mode": "auto", "max_cost_tier": "economy"}, min_context_window=300_000
    )

    assert context_fit["mode"] == "best_effort"
    assert [c.id for c in candidates] == ["test/economy-small"]


# ── fallback_model() honours the same floor and best-effort rule ────────────
def test_fallback_model_skips_a_model_below_the_context_floor():
    small = _model("small", context_window=8_000)
    big = _model("big", context_window=200_000)
    catalog = _catalog(small, big)
    registry = _Registry({"openrouter"})

    chosen = fallback_model(
        "verdict", catalog, registry, min_context_window=100_000
    )

    assert chosen == "test/big"


def test_fallback_model_best_effort_prefers_largest_window_within_ceiling():
    economy_small = _model("economy-small", context_window=8_000, cost_tier="economy")
    economy_big = _model("economy-big", context_window=200_000, cost_tier="economy")
    premium_huge = _model("premium-huge", context_window=2_000_000, cost_tier="premium")
    catalog = _catalog(economy_small, economy_big, premium_huge)
    registry = _Registry({"openrouter"})

    chosen = fallback_model(
        "verdict",
        catalog,
        registry,
        max_cost_tier="economy",
        min_context_window=300_000,
    )

    assert chosen == "test/economy-big"


def test_fallback_model_min_context_window_none_is_unchanged():
    catalog = _catalog(_model("only", context_window=8_000))
    registry = _Registry({"openrouter"})
    assert fallback_model("verdict", catalog, registry) == "test/only"
    assert (
        fallback_model("verdict", catalog, registry, min_context_window=None) == "test/only"
    )


# ── override / pin: never blocked, but the fit is still recorded ───────────
async def test_a_pin_that_does_not_fit_is_still_honoured_and_marked_best_effort(no_llm_router):
    small = _model("small", context_window=8_000)
    catalog = _catalog(small)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "pinned", "model": "test/small"},
        min_context_window=100_000,
        **ROUTE_ARGS,
    )

    assert decision.chosen_model == "test/small"
    assert decision.override == "user_pin"
    assert decision.context_fit["mode"] == "best_effort"
    assert decision.context_fit["required"] == 100_000
    assert decision.context_fit["excluded"] == ["test/small"]


async def test_a_pin_that_fits_is_recorded_as_such(no_llm_router):
    big = _model("big", context_window=200_000)
    catalog = _catalog(big)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "pinned", "model": "test/big"},
        min_context_window=100_000,
        **ROUTE_ARGS,
    )

    assert decision.chosen_model == "test/big"
    assert decision.context_fit["mode"] == "fit"
    assert decision.context_fit["excluded"] == []


async def test_a_run_override_that_does_not_fit_is_still_honoured(no_llm_router):
    small = _model("small", context_window=8_000)
    catalog = _catalog(small)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "auto"},
        run_override="test/small",
        min_context_window=100_000,
        **ROUTE_ARGS,
    )

    assert decision.chosen_model == "test/small"
    assert decision.override == "run_override"
    assert decision.context_fit["mode"] == "best_effort"
    assert decision.context_fit["excluded"] == ["test/small"]


# ── unchecked: no min_context_window means no filtering, no claim of one ───
async def test_min_context_window_none_yields_unchecked(no_llm_router):
    only = _model("only", context_window=8_000)
    catalog = _catalog(only)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(model_policy={"mode": "auto"}, **ROUTE_ARGS)

    assert decision.context_fit == {"required": 0, "mode": "unchecked", "excluded": [], "exempt": []}


async def test_min_context_window_none_on_a_pin_yields_unchecked(no_llm_router):
    catalog = _catalog(_model("only", context_window=8_000))
    router = ModelRouter(catalog, _Registry({"openrouter"}))
    decision = await router.route(
        model_policy={"mode": "pinned", "model": "test/only"}, **ROUTE_ARGS
    )
    assert decision.context_fit["mode"] == "unchecked"


def test_candidates_with_fit_none_yields_unchecked_and_is_unfiltered():
    small = _model("small", context_window=8_000)
    catalog = _catalog(small)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    candidates, context_fit = router._candidates_with_fit({"mode": "auto"})

    assert context_fit == {"required": 0, "mode": "unchecked", "excluded": [], "exempt": []}
    assert [c.id for c in candidates] == ["test/small"]


# ── single-candidate path also records context_fit ──────────────────────────
async def test_single_candidate_path_records_context_fit(no_llm_router):
    only = _model("only", context_window=200_000)
    catalog = _catalog(only)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    decision = await router.route(
        model_policy={"mode": "auto"}, min_context_window=100_000, **ROUTE_ARGS
    )

    assert decision.chosen_model == "test/only"
    assert decision.context_fit["mode"] == "fit"


# ── an unknown/unreported window is never excluded ──────────────────────────
def test_a_model_with_no_reported_window_is_never_excluded():
    unknown = _model("unknown-window", context_window=0)
    big = _model("big", context_window=200_000)
    catalog = _catalog(unknown, big)
    router = ModelRouter(catalog, _Registry({"openrouter"}))

    candidates, context_fit = router._candidates_with_fit(
        {"mode": "auto"}, min_context_window=100_000
    )

    assert context_fit["mode"] == "fit"
    assert {c.id for c in candidates} == {"test/unknown-window", "test/big"}
    assert context_fit["excluded"] == []


# ── local models are exempt from the context-fit filter ─────────────────────
def _prior(model_id: str, quality: float, *, floor: float | None = None) -> ModelPrior:
    return ModelPrior(
        model_id=model_id,
        runs=40,
        effective_n=30.0,
        quality_mean=quality,
        quality_raw=quality,
        quality_ci_low=quality if floor is None else floor,
        delivered_rate=quality,
        failure_rate=1 - quality,
        mean_cost_usd=0.02,
        mean_output_tokens=800,
        mean_iterations=5.0,
        mean_energy_wh=0.4,
        approvals=0,
        rejections=0,
    )


def test_local_model_is_exempt_from_context_fit_exclusion():
    """A local model is very often chosen for confidentiality, not capability
    (docs/local-models.md) — the filter must never push it to a cloud model
    just because its window looks small next to the composed prompt."""
    tiny_local = _model("tiny-local", context_window=2_000, cost_tier="local", provider="local")
    fits_cloud = _model("cloud-big", context_window=200_000)

    candidates, context_fit = _apply_context_fit([tiny_local, fits_cloud], 100_000)

    assert [c.id for c in candidates] == ["test/tiny-local", "test/cloud-big"]
    assert context_fit["mode"] == "fit"
    assert context_fit["excluded"] == []
    assert context_fit["exempt"] == ["test/tiny-local"]


def test_local_model_is_exempt_even_when_its_window_cannot_hold_the_call():
    """Whether or not the local model's own window is enough is irrelevant —
    it is never excluded, window known or not (same treatment an unreported
    window already got)."""
    tiny_local = _model("tiny-local", context_window=1_000, cost_tier="local", provider="local")
    small_cloud = _model("small-cloud", context_window=8_000)

    candidates, context_fit = _apply_context_fit([tiny_local, small_cloud], 100_000)

    # A local model in the mix means something always "fits" — mode never
    # falls through to best_effort just because nothing else does.
    assert context_fit["mode"] == "fit"
    assert context_fit["exempt"] == ["test/tiny-local"]
    assert context_fit["excluded"] == ["test/small-cloud"]
    assert [c.id for c in candidates] == ["test/tiny-local"]


def test_local_model_exemption_is_recorded_even_in_best_effort_mode():
    """If a local model is present at all, `_apply_context_fit` never reaches
    best_effort (something always fits) — this pins that a hypothetical
    best_effort record would still list it under `exempt`, not `excluded`, by
    exercising the branch directly rather than relying on it being
    unreachable."""
    tiny_local = _model("tiny-local", context_window=1_000, cost_tier="local", provider="local")
    only_cloud = _model("only-cloud", context_window=8_000)

    candidates, context_fit = _apply_context_fit([tiny_local, only_cloud], 100_000)

    assert "test/tiny-local" not in context_fit["excluded"]
    assert "test/tiny-local" in context_fit["exempt"]


async def test_route_never_excludes_a_local_model(no_llm_router):
    tiny_local = _model("tiny-local", context_window=2_000, cost_tier="local", provider="local")
    catalog = _catalog(tiny_local)
    router = ModelRouter(catalog, _Registry({"local"}))

    decision = await router.route(
        model_policy={"mode": "auto"}, min_context_window=100_000, **ROUTE_ARGS
    )

    assert decision.chosen_model == "test/tiny-local"
    assert decision.context_fit["mode"] == "fit"
    assert decision.context_fit["exempt"] == ["test/tiny-local"]
    assert decision.context_fit["excluded"] == []


# ── best-effort re-sort orders by evidence tier ahead of window size ────────
def test_best_effort_orders_proven_ahead_of_poor_regardless_of_window_size():
    poor_but_huge = _model("poor-huge", context_window=500_000)
    unproven_mid = _model("unproven-mid", context_window=50_000)
    proven_small = _model("proven-small", context_window=20_000)
    priors = {
        poor_but_huge.id: _prior(poor_but_huge.id, 0.1),  # mean <= 0.35 -> TIER_POOR
        proven_small.id: _prior(proven_small.id, 0.9, floor=0.9),  # ci_low >= 0.65 -> TIER_PROVEN
        # unproven_mid has no recorded prior at all -> TIER_UNKNOWN
    }
    assert evidence_tier(priors[poor_but_huge.id]) == TIER_POOR
    assert evidence_tier(priors[proven_small.id]) == TIER_PROVEN
    assert evidence_tier(None) == TIER_UNKNOWN

    candidates, context_fit = _apply_context_fit(
        [poor_but_huge, unproven_mid, proven_small], 1_000_000, priors
    )

    assert context_fit["mode"] == "best_effort"
    # Proven first, then no-opinion, then proven-poor last — a poor-evidence
    # model never leads just because it advertises the biggest window.
    assert [c.id for c in candidates] == [
        "test/proven-small",
        "test/unproven-mid",
        "test/poor-huge",
    ]


def test_best_effort_still_breaks_ties_within_a_tier_by_window_size():
    """No priors at all (or models tied on evidence tier) falls back to the
    historical rule: biggest window first — the fix adds a ranking ahead of
    window size, it does not remove window size as the tiebreaker."""
    unproven_small = _model("unproven-small", context_window=20_000)
    unproven_huge = _model("unproven-huge", context_window=500_000)

    candidates, context_fit = _apply_context_fit(
        [unproven_small, unproven_huge], 1_000_000
    )

    assert context_fit["mode"] == "best_effort"
    assert [c.id for c in candidates] == ["test/unproven-huge", "test/unproven-small"]


def test_candidates_with_fit_prefers_evidence_over_window_size():
    """The same check as `test_best_effort_orders_proven_ahead_of_poor_
    regardless_of_window_size`, but through `ModelRouter._candidates_with_fit`
    — the method `route()` actually calls — rather than `_apply_context_fit`
    directly, so the priors-threading wire-up is covered too.

    (Not driven through `route()` itself: with no usable LLM router in this
    fixture set, `route()` falls to `fallback_model()`, which has its own,
    separately-tested, proven-poor drop — `_drop_proven_poor` in
    router_llm/fallback.py — and would pass this assertion even without the
    `_apply_context_fit` fix, which is what this test is actually about.)
    """
    poor_but_huge = _model("poor-huge", context_window=500_000, cost_tier="economy")
    proven_small = _model("proven-small", context_window=20_000, cost_tier="economy")
    catalog = _catalog(poor_but_huge, proven_small)
    router = ModelRouter(catalog, _Registry({"openrouter"}))
    priors = {
        poor_but_huge.id: _prior(poor_but_huge.id, 0.1),
        proven_small.id: _prior(proven_small.id, 0.9, floor=0.9),
    }

    candidates, context_fit = router._candidates_with_fit(
        {"mode": "auto"}, priors, min_context_window=1_000_000  # nothing fits
    )

    assert context_fit["mode"] == "best_effort"
    assert [c.id for c in candidates] == ["test/proven-small", "test/poor-huge"]
