"""Phase 1: what recorded evidence is and is not allowed to do to a route.

The theme of every test here is *bounded power*. Evidence reorders candidates,
demotes proven-poor ones, and is shown to the router model. It may not widen a
policy, may not reach a model with no provider key, and may not change anything
at all on an install with no history.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from tret.adaptive import (
    DEFAULT_ADAPTIVE,
    STATIC_ADAPTIVE,
    adaptive_of,
    validation_error,
)
from tret.api.harnesses import _validate_policy
from tret.providers.base import ProviderError
from tret.providers.catalog import ModelCatalog, ProviderRegistry
from tret.router_llm.fallback import fallback_model
from tret.router_llm.objectives import (
    EVIDENCE_GOOD_FLOOR,
    EVIDENCE_POOR_MEAN,
    TIER_POOR,
    TIER_PROVEN,
    TIER_UNKNOWN,
    candidate_sort_key,
    evidence_tier,
)
from tret.router_llm.priors import ModelPrior, NoPriors
from tret.router_llm.prompts import render_router_prompt
from tret.router_llm.router import ModelRouter, RoutingUnavailable


class _Registry(ProviderRegistry):
    """Keys without reachable providers.

    `has_key` decides candidacy, which is what these tests are about; `get`
    raises, so `route()` exhausts its retries and lands on the deterministic
    fallback. That is deliberate — it is the path a deployment takes whenever the
    router model is unreachable, and the invariants asserted below have to hold
    there just as much as on the LLM path.
    """

    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers

    def get(self, provider: str):
        raise ProviderError(provider, "no provider instance in this test")


class _StubPriors:
    """A fixed track record, so a route's evidence is stated by the test."""

    def __init__(self, priors: dict[str, ModelPrior]):
        self.priors = priors
        self.calls: list[tuple] = []

    async def for_key(self, *, task_shape, objective, size_band=None):
        self.calls.append((task_shape, objective, size_band))
        return self.priors

    def invalidate(self) -> None:
        pass


def _prior(model_id: str, quality: float, *, floor: float | None = None, **over) -> ModelPrior:
    args = dict(
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
    args.update(over)
    return ModelPrior(**args)


def _all_keys() -> _Registry:
    return _Registry({"anthropic", "kimi", "openrouter"})


def _policy(**over) -> dict:
    base = {"mode": "auto", "max_cost_tier": "premium"}
    base.update(over)
    return base


async def _route(router: ModelRouter, policy: dict, **over):
    args = dict(
        model_policy=policy,
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="assess",
        output_contract="one verdict",
        n_documents=1,
        est_input_tokens=20_000,
    )
    args.update(over)
    return await router.route(**args)


# ── the adaptive block ───────────────────────────────────────────────────────
def test_the_defaults_are_on():
    assert adaptive_of(None) == DEFAULT_ADAPTIVE
    assert DEFAULT_ADAPTIVE.learn_from_outcomes is True


def test_the_static_profile_turns_every_adaptive_behavior_off():
    # What the golden-run evals pin, so a replayed run depends on the replay and
    # not on how many outcomes the database happens to hold.
    assert STATIC_ADAPTIVE.learn_from_outcomes is False
    assert STATIC_ADAPTIVE.escalation == "off"
    assert STATIC_ADAPTIVE.compaction == "off"
    assert STATIC_ADAPTIVE.max_switches == 0


def test_a_harness_can_turn_learning_off():
    assert adaptive_of({"adaptive": {"learn_from_outcomes": False}}).learn_from_outcomes is False


@pytest.mark.parametrize(
    "block",
    [
        {"learn_from_outcomes": "yes"},
        {"compaction": "sometimes"},
        {"escalation": "always"},
        {"context_headroom": 1.5},
        {"context_headroom": "0.8"},
        {"max_switches": 99},
        {"max_switches": True},
        {"misspelled": True},
        ["not an object"],
    ],
)
def test_a_bad_adaptive_block_is_refused_at_the_door(block):
    # Never read as a default: a misspelled key that is silently dropped reads as
    # a setting that was applied, and two of these keys let a run change what it
    # is doing mid-flight.
    assert validation_error(block) is not None
    with pytest.raises(HTTPException) as e:
        _validate_policy(_policy(adaptive=block))
    assert e.value.status_code == 422


def test_a_policy_with_no_adaptive_block_still_validates():
    _validate_policy(_policy())
    _validate_policy(_policy(adaptive={"escalation": "off"}))


def test_out_of_range_values_fall_back_rather_than_steering_a_run():
    # The API refuses these; if one somehow reaches the engine it must read as
    # the default, not as whatever nonsense was written.
    assert adaptive_of({"adaptive": {"context_headroom": 12}}) == DEFAULT_ADAPTIVE
    assert adaptive_of({"adaptive": {"compaction": "maybe"}}).compaction == "auto"


# ── evidence tiers ───────────────────────────────────────────────────────────
def test_an_untried_model_sits_with_the_ordinary_ones_not_below_them():
    # The rich-get-richer guard: rank an untried model last and the first model
    # to build a record keeps its lead forever.
    assert evidence_tier(None) == TIER_UNKNOWN
    assert evidence_tier(_prior("m/mid", 0.5)) == TIER_UNKNOWN


def test_promotion_is_judged_on_the_pessimistic_reading():
    # A high mean with a weak floor is not promoted: the cost of being wrong is
    # sending every run of this shape to the wrong model.
    assert evidence_tier(_prior("m/x", 0.9, floor=EVIDENCE_GOOD_FLOOR + 0.01)) == TIER_PROVEN
    assert evidence_tier(_prior("m/x", 0.9, floor=0.2)) == TIER_UNKNOWN


def test_demotion_does_not_require_certainty():
    assert evidence_tier(_prior("m/x", EVIDENCE_POOR_MEAN - 0.01, floor=0.0)) == TIER_POOR


# ── candidate ordering ───────────────────────────────────────────────────────
def test_with_no_evidence_the_ordering_is_byte_for_byte_what_it_always_was():
    catalog = ModelCatalog()
    models = catalog.all(curated_only=True)
    for objective in ("balanced", "quality", "eco", "token_conservation"):
        before = sorted(models, key=candidate_sort_key(objective))
        after = sorted(models, key=candidate_sort_key(objective, {}))
        assert [m.id for m in before] == [m.id for m in after]


def test_a_proven_model_survives_the_truncation_that_would_have_dropped_it():
    # The candidate list is capped and read top-down, so ordering is not
    # cosmetic: a model that sorts late may never be considered at all.
    router = ModelRouter(ModelCatalog(), _all_keys())
    plain = [m.id for m in router._candidates(_policy())]
    laggard = plain[-1]
    promoted = [
        m.id for m in router._candidates(_policy(), {laggard: _prior(laggard, 0.95, floor=0.9)})
    ]
    assert promoted[0] == laggard
    assert plain[0] != laggard


def test_evidence_never_overrides_the_objective_within_a_tier():
    catalog = ModelCatalog()
    models = catalog.all(curated_only=True)
    # Everything proven: the tier is constant, so the objective decides in full.
    priors = {m.id: _prior(m.id, 0.9, floor=0.9) for m in models}
    assert [m.id for m in sorted(models, key=candidate_sort_key("eco", priors))] == [
        m.id for m in sorted(models, key=candidate_sort_key("eco"))
    ]


def test_a_proven_poor_model_sorts_last_but_is_not_removed():
    catalog = ModelCatalog()
    models = catalog.all(curated_only=True)
    leader = sorted(models, key=candidate_sort_key("balanced"))[0].id
    ordered = sorted(models, key=candidate_sort_key("balanced", {leader: _prior(leader, 0.1)}))
    assert ordered[-1].id == leader
    assert len(ordered) == len(models)  # still a candidate, just a last resort


# ── the deterministic fallback ───────────────────────────────────────────────
def test_the_fallback_skips_a_model_with_a_proven_poor_record():
    catalog, registry = ModelCatalog(), _all_keys()
    plain = fallback_model("verdict", catalog, registry)
    demoted = fallback_model("verdict", catalog, registry, priors={plain: _prior(plain, 0.1)})
    assert demoted is not None and demoted != plain


def test_the_fallback_still_returns_something_when_every_model_has_a_poor_record():
    # "These models perform badly here" must not become "this harness is
    # broken" — a much stronger claim than the evidence supports.
    catalog, registry = ModelCatalog(), _all_keys()
    priors = {m.id: _prior(m.id, 0.05) for m in catalog.all()}
    assert fallback_model("verdict", catalog, registry, priors=priors) is not None


def test_evidence_cannot_reach_a_model_the_policy_excluded():
    catalog, registry = ModelCatalog(), _all_keys()
    allowed = [m.id for m in catalog.all(curated_only=True)[:2]]
    outsider = catalog.all(curated_only=True)[5].id
    chosen = fallback_model(
        "verdict", catalog, registry, allowed, priors={outsider: _prior(outsider, 0.99, floor=0.99)}
    )
    assert chosen in allowed


# ── the router prompt ────────────────────────────────────────────────────────
def _render(priors=None) -> str:
    return render_router_prompt(
        task_type="divergence_assessment",
        task_shape="verdict",
        task_description="assess",
        output_contract="one verdict",
        n_documents=2,
        est_input_tokens=1234,
        max_cost_tier="premium",
        candidates=ModelCatalog().all(curated_only=True)[:3],
        priors=priors,
    )


def test_with_no_evidence_the_prompt_is_unchanged():
    assert _render(None) == _render({})
    assert "TRACK RECORD" not in _render(None)


def test_the_track_record_reports_the_numbers_and_the_caveats_together():
    candidate = ModelCatalog().all(curated_only=True)[0].id
    rendered = _render({candidate: _prior(candidate, 0.81, floor=0.76, approvals=31, rejections=2)})
    assert "TRACK RECORD" in rendered
    assert "31 approved / 2 rejected" in rendered
    # Each caveat corrects a specific way this evidence misleads, and they have
    # to travel with the numbers rather than living in a doc nobody renders.
    assert "not a controlled comparison" in rendered
    assert "untried here, not bad" in rendered
    assert "never what it cost" in rendered


def test_candidates_without_a_record_are_named_as_untried():
    models = ModelCatalog().all(curated_only=True)[:3]
    rendered = _render({models[0].id: _prior(models[0].id, 0.8)})
    assert "no record yet" in rendered
    assert models[1].id in rendered.split("no record yet")[1]


def test_priors_for_models_that_are_not_candidates_are_not_shown():
    rendered = _render({"some/model-not-in-the-list": _prior("some/model-not-in-the-list", 0.9)})
    assert "TRACK RECORD" not in rendered


# ── the decision's evidence ──────────────────────────────────────────────────
async def test_a_route_records_the_evidence_it_was_made_against():
    # Snapshotted, not referenced: priors are a moving aggregate, and
    # re-deriving them next month answers a different question.
    catalog = ModelCatalog()
    candidate = catalog.all(curated_only=True)[0].id
    priors = _StubPriors({candidate: _prior(candidate, 0.88, floor=0.8)})
    decision = await _route(ModelRouter(catalog, _all_keys(), priors), _policy())

    assert decision.evidence is not None
    assert candidate in decision.evidence["priors"]
    assert decision.evidence["proven"] == [candidate]
    assert decision.evidence["unrecorded"]


async def test_priors_are_looked_up_under_the_key_the_run_actually_has():
    priors = _StubPriors({})
    await _route(
        ModelRouter(ModelCatalog(), _all_keys(), priors),
        _policy(objective="eco"),
        task_shape="drafting",
        est_input_tokens=200_000,
    )
    assert priors.calls == [("drafting", "eco", "l")]


async def test_a_harness_with_learning_off_reads_no_evidence_at_all():
    priors = _StubPriors({"anything": _prior("anything", 0.9)})
    decision = await _route(
        ModelRouter(ModelCatalog(), _all_keys(), priors),
        _policy(adaptive={"learn_from_outcomes": False}),
    )
    assert priors.calls == []
    assert decision.evidence is None


async def test_an_override_is_decided_without_consulting_evidence():
    # The caller named a model. There is no choice to inform.
    priors = _StubPriors({"anything": _prior("anything", 0.9)})
    pinned = ModelCatalog().all(curated_only=True)[0].id
    decision = await _route(
        ModelRouter(ModelCatalog(), _all_keys(), priors), {"mode": "pinned", "model": pinned}
    )
    assert decision.chosen_model == pinned
    assert decision.evidence is None
    assert priors.calls == []


async def test_evidence_cannot_lift_a_harness_cost_ceiling():
    # The load-bearing invariant of the whole phase: a glowing track record on a
    # premium model changes nothing about a harness capped at economy.
    catalog = ModelCatalog()
    premium = next(m for m in catalog.all(curated_only=True) if m.cost_tier == "premium")
    priors = _StubPriors({premium.id: _prior(premium.id, 0.99, floor=0.99)})
    decision = await _route(
        ModelRouter(catalog, _all_keys(), priors), _policy(max_cost_tier="economy")
    )
    assert catalog.get(decision.chosen_model).cost_tier != "premium"


async def test_evidence_cannot_reach_a_provider_with_no_key():
    catalog = ModelCatalog()
    unreachable = next(m for m in catalog.all(curated_only=True) if m.provider == "anthropic")
    priors = _StubPriors({unreachable.id: _prior(unreachable.id, 0.99, floor=0.99)})
    decision = await _route(
        ModelRouter(catalog, _Registry({"openrouter"}), priors), _policy()
    )
    assert catalog.get(decision.chosen_model).provider == "openrouter"


async def test_a_capped_harness_with_nothing_left_still_fails_loudly():
    # Evidence must not turn "no candidate is permitted" into a quiet escalation.
    priors = _StubPriors({})
    with pytest.raises(RoutingUnavailable):
        await _route(ModelRouter(ModelCatalog(), _Registry(set()), priors), _policy())


async def test_no_priors_leaves_the_decision_free_of_evidence():
    decision = await _route(ModelRouter(ModelCatalog(), _all_keys(), NoPriors()), _policy())
    assert decision.evidence is None
