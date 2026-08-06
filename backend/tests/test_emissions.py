"""Emissions accounting: PUE, GHG Protocol scopes, the baseline counterfactual.

No network and no DB. The accounting functions take an explicit `settings` (and
optional `catalog`), so every case here is driven by construction rather than by
patching globals; the endpoint tests use the fake-session pattern from
test_analytics.py.

What these tests defend is honesty, not the specific constants
(docs/emissions-methodology.md owns those): the scope total always equals the sum
of its parts, cloud and self-hosted land in different scopes, PUE is resolved from
the deployment rather than borrowed from a hyperscaler, the grid factor carries its
GHG Protocol basis, the uncertainty band never inverts or goes negative and never
calls itself a confidence interval, the carbon and money counterfactuals are both
allowed to be negative, embodied carbon stays opt-in, every constant carries its
provenance, missing estimates stay null instead of becoming zero, and window
rollups are sums of stored figures rather than recomputations at today's settings.

The last group of tests checks the methodology doc against the code, because that
doc is rendered in the product UI: a drifted constant there is a published wrong
number, not a stale comment.
"""
from __future__ import annotations

import json
import uuid
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bench.api import analytics
from bench.api.analytics import _recorded_emissions, emissions
from bench.api.runs import _run_summary
from bench.config import Settings
from bench.db.models import Run, utcnow
from bench.providers.catalog import ModelCatalog, ModelInfo
from bench.services.emissions import (
    DEPLOYMENT_CLOUD,
    DEPLOYMENT_LOCAL,
    GRID_BASES,
    PUE_PROFILE_CLOUD,
    PUE_PROFILE_ONPREM,
    PUE_PROFILE_WORKSTATION,
    amortized_embodied_g_per_run,
    band_factors,
    co2e_grams,
    deployment_for,
    embodied_g_for,
    emission_event_fields,
    emission_summary_fields,
    energy_accounting,
    grid_basis_for,
    grid_factor_for,
    pue_for,
    pue_profile_for,
    resolve_baseline_model,
    scope_split,
)

CATALOG = ModelCatalog()


def _settings(**over) -> Settings:
    return Settings(**over)


def _model(energy_class: str = "L", provider: str = "anthropic", **over) -> ModelInfo:
    base = dict(
        id=f"{provider}/test",
        provider=provider,
        wire_id="test-1",
        display_name="Test",
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class=energy_class,
    )
    base.update(over)
    return ModelInfo(**base)


def _local_model(energy_class: str = "S") -> ModelInfo:
    return _model(
        energy_class,
        provider="local",
        id="local/qwen2.5:14b",
        cost_tier="local",
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
    )


def _account(model: ModelInfo, settings: Settings | None = None, **kw) -> dict:
    """One million input tokens through `model`, unless overridden."""
    tokens = kw.pop("tokens", (1_000_000, 0, 0, 0))
    return energy_accounting(
        model, *tokens, settings=settings or _settings(), catalog=CATALOG, **kw
    )


def _scope_sum(report: dict) -> Decimal:
    s = report["scopes"]
    return (
        Decimal(str(s["scope1_g"])) + Decimal(str(s["scope2_g"])) + Decimal(str(s["scope3_g"]))
    )


# ── the invariant: the total is the sum of its scopes ─────────────────────────
@pytest.mark.parametrize(
    "model, settings",
    [
        (_model("XL"), _settings()),
        (_model("S"), _settings(grid_co2e_g_per_kwh=723.4, datacenter_pue=1.37)),
        (_local_model(), _settings()),
        (_local_model(), _settings(embodied_g_per_run=12.5, local_grid_co2e_g_per_kwh=41.0)),
        (_local_model(), _settings(embodied_g_per_run=0.000003)),
        # The invariant has to survive every new moving part, not just the old
        # ones: the reasoning tier, the on-prem PUE profile, a market-based grid
        # basis, and an asymmetric uncertainty band.
        (_model("R"), _settings()),
        (
            _local_model(),
            _settings(
                local_deployment_profile="onprem_datacenter",
                embodied_g_per_run=7.25,
                local_grid_co2e_g_per_kwh=88.0,
                local_grid_co2e_basis="market_based",
            ),
        ),
        (_model("XL"), _settings(uncertainty_band_low=1.5, uncertainty_band_high=8.0)),
        (_model("S"), _settings(emissions_baseline_model="openrouter/deepseek/deepseek-v4-pro")),
    ],
)
def test_run_total_always_equals_scope1_plus_scope2_plus_scope3(model, settings):
    report = _account(model, settings, tokens=(123_457, 9_871, 55_555, 3_333))
    assert Decimal(str(report["co2e_g"])) == _scope_sum(report)
    assert report["co2e_g"] == pytest.approx(
        report["scopes"]["scope1_g"]
        + report["scopes"]["scope2_g"]
        + report["scopes"]["scope3_g"]
    )


def test_zero_tokens_is_zero_carbon_in_every_scope():
    report = _account(_model("XL"), tokens=(0, 0, 0, 0))
    assert report["co2e_g"] == 0.0
    assert _scope_sum(report) == 0


# ── scope 1 ──────────────────────────────────────────────────────────────────
def test_scope1_is_always_an_explicit_zero_with_an_explanation():
    for model in (_model("L"), _local_model()):
        report = _account(model)
        assert report["scopes"]["scope1_g"] == 0.0  # present, not omitted
        assert "Scope 1 = 0" in report["scopes"]["basis"]
        assert "on-site" in report["scopes"]["basis"]


# ── cloud vs self-hosted ─────────────────────────────────────────────────────
def test_deployment_split_is_only_about_who_buys_the_power():
    assert deployment_for("local") == DEPLOYMENT_LOCAL
    for provider in ("anthropic", "kimi", "openrouter", "something-new"):
        assert deployment_for(provider) == DEPLOYMENT_CLOUD


def test_cloud_inference_is_entirely_scope_3():
    report = _account(_model("L"))
    assert report["deployment"] == "cloud"
    assert report["scopes"]["scope2_g"] == 0.0
    assert report["scopes"]["scope3_g"] == report["co2e_g"] > 0
    assert "purchased goods and services" in report["scopes"]["basis"]


def test_self_hosted_electricity_is_scope_2():
    settings = _settings()
    report = _account(_local_model(), settings)
    assert report["deployment"] == "local"
    # 1 Mtok of *input* at S is 250 x 0.05 = 12.5 Wh, x local PUE 1.05 = 13.125
    # Wh, at 470 g/kWh = 6.16875 g.
    assert report["energy_wh"] == 12.5
    assert report["energy_wh_total"] == 13.125
    assert report["scopes"]["scope2_g"] == pytest.approx(6.16875)
    assert report["scopes"]["scope3_g"] == 0.0  # nothing embodied by default
    assert report["co2e_g"] == report["scopes"]["scope2_g"]


def test_a_local_operator_may_set_their_own_grid_factor():
    assert grid_factor_for(DEPLOYMENT_LOCAL, _settings(grid_co2e_g_per_kwh=400.0)) == 400.0
    settings = _settings(grid_co2e_g_per_kwh=400.0, local_grid_co2e_g_per_kwh=30.0)
    assert grid_factor_for(DEPLOYMENT_LOCAL, settings) == 30.0
    # Cloud keeps the general factor: bench does not know which region served it.
    assert grid_factor_for(DEPLOYMENT_CLOUD, settings) == 400.0

    report = _account(_local_model(), settings)
    assert report["grid_co2e_g_per_kwh"] == 30.0
    assert report["scopes"]["scope2_g"] == pytest.approx(0.39375)  # 13.125 Wh at 30 g/kWh


# ── the GHG Protocol grid basis ───────────────────────────────────────────────
def test_the_grid_basis_is_recorded_because_the_two_kinds_do_not_mix():
    # Location-based (physical grid) and market-based (PPAs, RECs) answer
    # different questions and may not be summed. The shipped default factor is an
    # IEA physical-grid average, so it is location-based.
    settings = _settings()
    assert settings.grid_co2e_basis == "location_based"
    assert grid_basis_for(DEPLOYMENT_CLOUD, settings) == "location_based"
    assert _account(_model("L"), settings)["grid_co2e_basis"] == "location_based"


def test_a_local_market_based_factor_is_labelled_as_such():
    settings = _settings(local_grid_co2e_g_per_kwh=25.0, local_grid_co2e_basis="market_based")
    assert grid_basis_for(DEPLOYMENT_LOCAL, settings) == "market_based"
    assert _account(_local_model(), settings)["grid_co2e_basis"] == "market_based"
    # Cloud still reports the location-based default: the local label describes
    # the local factor only.
    assert _account(_model("L"), settings)["grid_co2e_basis"] == "location_based"


def test_an_operators_own_local_factor_is_unspecified_until_they_say_otherwise():
    settings = _settings(local_grid_co2e_g_per_kwh=25.0)
    assert settings.local_grid_co2e_basis == "unspecified"
    assert grid_basis_for(DEPLOYMENT_LOCAL, settings) == "unspecified"
    # Nonsense is not silently promoted to a real basis.
    junk = _settings(local_grid_co2e_g_per_kwh=25.0, local_grid_co2e_basis="green-ish")
    assert grid_basis_for(DEPLOYMENT_LOCAL, junk) == "unspecified"
    assert set(GRID_BASES) == {"location_based", "market_based", "unspecified"}


def test_an_explicitly_passed_factor_carries_no_basis_claim():
    # bench was handed a number with no provenance; claiming a basis for it would
    # be inventing one.
    report = _account(_model("L"), _settings(), grid_g_per_kwh=123.0)
    assert report["grid_co2e_g_per_kwh"] == 123.0
    assert report["grid_co2e_basis"] == "unspecified"
    grid_factor = next(f for f in report["factors"] if f["key"] == "grid_intensity")
    assert grid_factor["overridden"] is True
    assert grid_factor["basis"] == "unspecified"


def test_an_explicit_grid_override_still_wins_over_both_settings():
    settings = _settings(grid_co2e_g_per_kwh=400.0, local_grid_co2e_g_per_kwh=30.0)
    report = _account(_local_model(), settings, grid_g_per_kwh=700.0)
    assert report["grid_co2e_g_per_kwh"] == 700.0


# ── PUE ──────────────────────────────────────────────────────────────────────
def test_pue_inflates_the_total_and_never_the_compute_figure():
    settings = _settings(datacenter_pue=1.5)
    report = _account(_model("M"), settings)
    # energy_wh keeps its original meaning: compute / IT load only. 1 Mtok of
    # input at M is 1200 x 0.05 = 60 Wh.
    assert report["energy_wh"] == 60.0
    assert report["pue"] == 1.5
    assert report["energy_wh_total"] == 90.0
    # Carbon comes off the *total*, not the compute figure.
    assert report["co2e_g"] == pytest.approx(float(co2e_grams(Decimal("90"), 470.0)))
    assert report["co2e_g"] == pytest.approx(42.3)
    assert report["co2e_g"] != pytest.approx(float(co2e_grams(Decimal("60"), 470.0)))


def test_defaults_are_the_documented_cited_figures():
    settings = _settings()
    # 1.2 is ABOVE every hyperscaler self-report (Google 1.09, AWS 1.15,
    # Microsoft 1.16) and below the 1.56 Uptime Institute industry average:
    # deliberately conservative, which is the safe direction.
    assert settings.datacenter_pue == 1.2
    assert settings.local_pue == 1.05  # a desktop has almost no facility overhead
    assert settings.onprem_pue == 1.56  # Uptime Institute 2024, 879 operators
    assert settings.local_deployment_profile == "workstation"
    assert settings.grid_co2e_g_per_kwh == 470.0  # IEA 2024 global average
    assert settings.grid_co2e_basis == "location_based"
    assert settings.embodied_g_per_run == 0.0
    assert settings.local_grid_co2e_g_per_kwh is None
    assert settings.emissions_baseline_model == ""
    assert (settings.uncertainty_band_low, settings.uncertainty_band_high) == (2.5, 2.5)
    assert pue_for(DEPLOYMENT_CLOUD, settings) == Decimal("1.2")
    assert pue_for(DEPLOYMENT_LOCAL, settings) == Decimal("1.05")


def test_pue_is_resolved_per_deployment_profile_not_borrowed_from_the_cloud():
    workstation = _settings()
    onprem = _settings(local_deployment_profile="onprem_datacenter")
    assert pue_profile_for(DEPLOYMENT_CLOUD, workstation) == PUE_PROFILE_CLOUD
    assert pue_profile_for(DEPLOYMENT_LOCAL, workstation) == PUE_PROFILE_WORKSTATION
    assert pue_profile_for(DEPLOYMENT_LOCAL, onprem) == PUE_PROFILE_ONPREM
    # An on-prem machine room is a small data centre: 1.56, not 1.05 and not 1.2.
    assert pue_for(DEPLOYMENT_LOCAL, onprem) == Decimal("1.56")
    # The cloud profile is unaffected by the local one.
    assert pue_for(DEPLOYMENT_CLOUD, onprem) == Decimal("1.2")
    # An unrecognised profile falls back to the workstation figure rather than
    # picking the largest or the smallest.
    junk = _settings(local_deployment_profile="datacentre-ish")
    assert pue_profile_for(DEPLOYMENT_LOCAL, junk) == PUE_PROFILE_WORKSTATION
    assert pue_for(DEPLOYMENT_LOCAL, junk) == Decimal("1.05")


def test_the_resolved_profile_and_its_source_travel_with_the_run():
    onprem = _account(_local_model(), _settings(local_deployment_profile="onprem_datacenter"))
    assert onprem["pue_profile"] == PUE_PROFILE_ONPREM
    assert onprem["pue"] == 1.56
    # 12.5 Wh compute x 1.56 = 19.5 Wh, at 470 g/kWh = 9.165 g.
    assert onprem["energy_wh_total"] == pytest.approx(19.5)
    assert onprem["scopes"]["scope2_g"] == pytest.approx(9.165)
    factor = next(f for f in onprem["factors"] if f["key"] == "pue")
    assert factor["value"] == 1.56
    assert factor["profile"] == PUE_PROFILE_ONPREM
    assert "Uptime Institute" in factor["source"]
    assert factor["setting"] == "BENCH_ONPREM_PUE"

    cloud = next(f for f in _account(_model("L"))["factors"] if f["key"] == "pue")
    assert cloud["value"] == 1.2
    assert "Google" in cloud["source"]


def test_a_pue_below_one_is_refused_rather_than_shrinking_the_number():
    # Using less than the IT load is not physically possible; a typo must not
    # produce a flattering figure.
    assert pue_for(DEPLOYMENT_CLOUD, _settings(datacenter_pue=0.5)) == Decimal(1)
    assert pue_for(DEPLOYMENT_LOCAL, _settings(local_pue=0.0)) == Decimal(1)


# ── embodied hardware ────────────────────────────────────────────────────────
def test_embodied_carbon_defaults_to_zero_and_only_applies_to_local():
    assert embodied_g_for(DEPLOYMENT_LOCAL, _settings()) == Decimal(0)
    settings = _settings(embodied_g_per_run=8.0)
    assert embodied_g_for(DEPLOYMENT_LOCAL, settings) == Decimal("8.0")
    # Cloud hardware is inside the purchased service, not the operator's Cat. 2.
    assert embodied_g_for(DEPLOYMENT_CLOUD, settings) == Decimal(0)
    assert _account(_model("L"), settings)["embodied_g"] == 0.0


def test_configured_embodied_carbon_lands_in_scope_3_for_local_runs():
    report = _account(_local_model(), _settings(embodied_g_per_run=8.0))
    assert report["embodied_g"] == 8.0
    assert report["scopes"]["scope2_g"] == pytest.approx(6.16875)  # electricity
    assert report["scopes"]["scope3_g"] == 8.0  # capital goods
    assert Decimal(str(report["co2e_g"])) == _scope_sum(report)
    assert "capital goods" in report["scopes"]["basis"]


def test_negative_embodied_setting_cannot_subtract_carbon():
    assert embodied_g_for(DEPLOYMENT_LOCAL, _settings(embodied_g_per_run=-100.0)) == Decimal(0)


# ── the baseline counterfactual ──────────────────────────────────────────────
def test_baseline_auto_selects_the_heaviest_curated_cloud_model_deterministically():
    settings = _settings()
    chosen = resolve_baseline_model(settings, CATALOG)
    heaviest = max(
        m.energy_wh_per_mtok for m in CATALOG.all(curated_only=True) if m.provider != "local"
    )
    assert chosen.energy_wh_per_mtok == heaviest
    assert chosen.provider != "local"
    # Deterministic across calls: ties break on model id.
    assert [resolve_baseline_model(settings, CATALOG).id for _ in range(3)] == [chosen.id] * 3
    tied = sorted(
        m.id for m in CATALOG.all(curated_only=True) if m.energy_wh_per_mtok == heaviest
    )
    assert chosen.id == tied[0]


def test_a_configured_baseline_is_used_verbatim():
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    assert resolve_baseline_model(settings, CATALOG).id == "anthropic/claude-haiku-4-5"
    report = _account(_model("XL"), settings)
    assert report["baseline"]["model"] == "anthropic/claude-haiku-4-5"


def test_an_unknown_baseline_reports_nothing_rather_than_substituting_one():
    report = _account(_model("L"), _settings(emissions_baseline_model="nope/not-a-model"))
    baseline = report["baseline"]
    assert baseline["model"] is None
    assert baseline["co2e_g"] is None
    assert baseline["avoided_co2e_g"] is None  # null, not 0
    assert "not a comparison that came out even" in baseline["basis"]


def test_baseline_is_a_same_token_counterfactual_and_says_so():
    report = _account(_model("M"), _settings(emissions_baseline_model="anthropic/claude-fable-5"))
    baseline = report["baseline"]
    # Same tokens, reasoning class: 1 Mtok of input = 21000 x 0.05 = 1050 Wh
    # compute, x1.2 PUE = 1260 Wh, at 470 g/kWh = 592.2 g.
    assert baseline["energy_class"] == "R"
    assert baseline["energy_wh"] == 1050.0
    assert baseline["co2e_g"] == pytest.approx(592.2)
    assert baseline["avoided_co2e_g"] == pytest.approx(592.2 - report["co2e_g"])
    assert baseline["avoided_co2e_g"] == pytest.approx(558.36)
    assert baseline["avoided_pct"] == pytest.approx(94.286, abs=1e-3)
    for phrase in ("not an offset", "efficiency indicator", "not usable for statutory"):
        assert phrase in baseline["basis"]


def test_a_heavier_than_baseline_run_reports_negative_avoided_carbon():
    # Cheap baseline, expensive run: honest arithmetic gives a negative figure,
    # and clamping it to zero would be a greenwash.
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    report = _account(_model("XL"), settings)
    baseline = report["baseline"]
    assert baseline["co2e_g"] < report["co2e_g"]
    assert baseline["avoided_co2e_g"] < 0
    assert baseline["avoided_pct"] < 0
    assert baseline["avoided_co2e_g"] == pytest.approx(baseline["co2e_g"] - report["co2e_g"])


def test_a_run_on_the_baseline_model_avoided_nothing():
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    model = CATALOG.get("anthropic/claude-haiku-4-5")
    report = _account(model, settings)
    baseline = report["baseline"]
    assert baseline["model"] == model.id
    assert baseline["avoided_co2e_g"] == 0.0
    assert baseline["avoided_pct"] == 0.0
    assert baseline["co2e_g"] == report["co2e_g"]
    assert "baseline model itself" in baseline["basis"]


def test_a_local_run_is_compared_against_a_cloud_baseline_including_embodied():
    settings = _settings(
        emissions_baseline_model="anthropic/claude-fable-5", embodied_g_per_run=5.0
    )
    report = _account(_local_model(), settings)
    baseline = report["baseline"]
    # The run's own total carries the embodied grams; the cloud baseline does not.
    assert report["embodied_g"] == 5.0
    assert baseline["co2e_g"] == pytest.approx(592.2)
    assert report["co2e_g"] == pytest.approx(11.16875)  # 6.16875 electricity + 5
    assert baseline["avoided_co2e_g"] == pytest.approx(581.03125)


# ── the existing contract ────────────────────────────────────────────────────
def test_every_original_key_survives_with_its_original_meaning():
    report = _account(_model("L"), tokens=(100_000, 10_000, 800_000, 100_000))
    assert report["estimated"] is True
    assert report["model"] == "anthropic/test"
    assert report["energy_class"] == "L"
    # Same keys, recalibrated values. energy_wh_per_mtok is now per million
    # OUTPUT-EQUIVALENT tokens, and the chain energy_wh = per_mtok x
    # weighted_tokens / 1e6 is unchanged.
    assert report["energy_wh_per_mtok"] == 2600.0
    # 0.05x100k + 1x10k + 0.005x800k + 0.05x100k = 5,000+10,000+4,000+5,000
    assert report["weighted_tokens"] == 24_000.0
    assert report["cache_read_weight"] == 0.005
    assert report["cache_write_weight"] == 0.05
    assert report["energy_wh"] == 62.4  # compute only, same meaning as before
    assert report["grid_co2e_g_per_kwh"] == 470.0
    assert "estimate, not a measurement" in report["basis"]
    # co2e_g is still the run total — PUE-inclusive and scope-decomposed.
    assert report["co2e_g"] == pytest.approx(62.4 * 1.2 * 0.47)
    assert report["co2e_g"] == pytest.approx(35.1936)


def test_the_additive_keys_are_all_present_and_self_consistent():
    report = _account(_model("L"), tokens=(100_000, 10_000, 800_000, 100_000))
    assert report["input_weight"] == 0.05
    assert report["output_weight"] == 1.0
    assert report["output_to_input_energy_ratio"] == 20.0
    assert report["energy_wh_per_mtok_output"] == 2600.0
    assert report["energy_wh_per_mtok_input"] == 130.0
    assert report["tokens"] == {
        "input": 100_000,
        "output": 10_000,
        "cache_read": 800_000,
        "cache_write": 100_000,
    }
    buckets = report["energy_wh_by_bucket"]
    assert buckets == {
        "input": 13.0,  # 2600 x 0.05 x 100k / 1e6
        "output": 26.0,  # 2600 x 1.00 x  10k / 1e6
        "cache_read": 10.4,  # 2600 x 0.005 x 800k / 1e6
        "cache_write": 13.0,  # 2600 x 0.05 x 100k / 1e6
    }
    assert sum(buckets.values()) == pytest.approx(report["energy_wh"])
    assert report["reasoning_tier"] is False
    assert report["pue_profile"] == "hyperscaler_cloud"
    assert report["grid_co2e_basis"] == "location_based"


def test_the_block_is_json_safe_all_the_way_down():
    report = _account(_local_model(), _settings(embodied_g_per_run=1.0))

    def _check(value, path="report"):
        if isinstance(value, dict):
            for k, v in value.items():
                assert isinstance(k, str), path
                _check(v, f"{path}.{k}")
        elif isinstance(value, list):
            for i, v in enumerate(value):
                _check(v, f"{path}[{i}]")
        else:
            assert value is None or isinstance(value, (bool, int, float, str)), (path, value)

    _check(report)
    # And it really round-trips, which is what JSONB storage actually requires.
    assert json.loads(json.dumps(report)) == report


# ── the uncertainty band ──────────────────────────────────────────────────────
def test_the_band_is_monotonic_around_the_central_estimate():
    report = _account(_model("L"), tokens=(100_000, 10_000, 800_000, 100_000))
    band = report["uncertainty"]
    assert band["band_factor_low"] == band["band_factor_high"] == 2.5
    # 35.1936 / 2.5 .. 35.1936 x 2.5
    assert band["co2e_g_low"] == pytest.approx(35.1936 / 2.5)
    assert band["co2e_g_high"] == pytest.approx(35.1936 * 2.5)
    assert band["co2e_g_low"] < report["co2e_g"] < band["co2e_g_high"]
    assert band["energy_wh_low"] < report["energy_wh"] < band["energy_wh_high"]
    assert (
        band["energy_wh_total_low"]
        < report["energy_wh_total"]
        < band["energy_wh_total_high"]
    )


@pytest.mark.parametrize(
    "model, settings, tokens",
    [
        (_model("R"), _settings(), (1_000_000, 500_000, 0, 0)),
        (_model("S"), _settings(uncertainty_band_low=1.0, uncertainty_band_high=1.0), (5, 5, 5, 5)),
        (_model("XL"), _settings(uncertainty_band_low=9.0, uncertainty_band_high=1.1), (1, 0, 0, 0)),
        (_local_model(), _settings(embodied_g_per_run=3.0), (0, 0, 0, 0)),
        (_model("M"), _settings(grid_co2e_g_per_kwh=0.0), (1_000, 1_000, 0, 0)),
    ],
)
def test_the_band_never_goes_negative_or_inverts(model, settings, tokens):
    band = _account(model, settings, tokens=tokens)["uncertainty"]
    for key in ("co2e_g", "energy_wh", "energy_wh_total"):
        low, high = band[f"{key}_low"], band[f"{key}_high"]
        assert low >= 0.0 and high >= 0.0
        assert low <= high


def test_a_band_factor_below_one_is_clamped_rather_than_inverting_the_band():
    # 0.5 would put the "low" end above the central estimate, which is not a
    # band. Clamped to 1, i.e. no band, rather than a nonsense one.
    assert band_factors(_settings(uncertainty_band_low=0.5, uncertainty_band_high=0.2)) == (
        Decimal(1),
        Decimal(1),
    )
    report = _account(_model("L"), _settings(uncertainty_band_low=0.5, uncertainty_band_high=0.2))
    band = report["uncertainty"]
    assert band["co2e_g_low"] == band["co2e_g_high"] == report["co2e_g"]


def test_the_band_refuses_to_call_itself_a_confidence_interval():
    band = _account(_model("L"))["uncertainty"]
    assert band["is_confidence_interval"] is False
    assert band["kind"] == "judgment_band"
    for phrase in ("JUDGMENT BAND, NOT A CONFIDENCE INTERVAL", "not a standard deviation"):
        assert phrase in band["basis"]


def test_per_factor_sensitivity_names_the_dominant_inputs():
    band = _account(_model("L"))["uncertainty"]
    contributions = {c["key"]: c for c in band["contributions"]}
    # Grid intensity and the energy class dominate; that is the expected finding
    # and the reason a regional factor is worth more than any other refinement.
    assert contributions["grid_intensity"]["dominant"] is True
    assert contributions["energy_class"]["dominant"] is True
    for c in band["contributions"]:
        assert c["low_multiplier"] <= 1.0 <= c["high_multiplier"], c["key"]
        assert c["note"]
    # The decomposition is deliberately NOT the headline band: multiplying the
    # sensitivities together is far wider than any published methodology claims.
    product = 1.0
    for c in band["contributions"]:
        product *= c["high_multiplier"]
    assert product > band["band_factor_high"]


def test_hidden_reasoning_tokens_are_a_one_sided_risk_on_the_reasoning_tier():
    plain = {c["key"]: c for c in _account(_model("L"))["uncertainty"]["contributions"]}
    thinking = {c["key"]: c for c in _account(_model("R"))["uncertainty"]["contributions"]}
    assert plain["reasoning_tokens"]["low_multiplier"] == 1.0  # can only be higher
    assert thinking["reasoning_tokens"]["high_multiplier"] > plain["reasoning_tokens"][
        "high_multiplier"
    ]
    assert thinking["reasoning_tokens"]["dominant"] is True


def test_local_runs_carry_the_unbatched_inference_risk():
    local = {c["key"]: c for c in _account(_local_model())["uncertainty"]["contributions"]}
    cloud = {c["key"]: c for c in _account(_model("L"))["uncertainty"]["contributions"]}
    assert "unbatched_local_inference" in local
    assert local["unbatched_local_inference"]["low_multiplier"] == 1.0  # one-sided
    assert "unbatched_local_inference" not in cloud


# ── the reasoning tier ────────────────────────────────────────────────────────
def test_the_reasoning_tier_is_flagged_and_dwarfs_the_rest_of_the_ladder():
    reasoning = _account(_model("R"), tokens=(0, 1_000_000, 0, 0))
    large = _account(_model("L"), tokens=(0, 1_000_000, 0, 0))
    assert reasoning["reasoning_tier"] is True
    assert large["reasoning_tier"] is False
    assert reasoning["energy_wh"] == 21_000.0  # o3's fitted b
    assert large["energy_wh"] == 2_600.0
    assert reasoning["co2e_g"] > 8 * large["co2e_g"]
    # The bias is stated on the run, not left to the docs.
    caveat = next(
        c for c in reasoning["caveats"] if c["key"] == "reasoning_token_accounting"
    )
    assert caveat["direction"] == "understates"
    assert "hidden reasoning tokens" in caveat["note"]
    assert "applies directly" in caveat["note"]
    factor = next(f for f in reasoning["factors"] if f["key"] == "energy_class")
    assert factor["reasoning_tier"] is True
    assert "hidden thinking tokens" in factor["note"]


# ── money saved ───────────────────────────────────────────────────────────────
def test_money_saved_is_the_same_token_counterfactual_in_dollars():
    settings = _settings(emissions_baseline_model="anthropic/claude-fable-5")
    # 1 Mtok in / 0 out on the test model at $3/Mtok input = $3.00; Fable 5
    # charges $10/Mtok input for the same tokens, so $7.00 is avoided.
    report = _account(_model("L"), settings)
    cost = report["cost"]
    assert cost["usd"] == 3.0
    assert cost["baseline_model"] == "anthropic/claude-fable-5"
    assert cost["baseline_usd"] == 10.0
    assert cost["avoided_usd"] == 7.0
    assert cost["avoided_pct"] == pytest.approx(70.0)
    assert cost["prices_are_exact"] is True
    assert report["baseline"]["avoided_usd"] == 7.0
    for phrase in ("prices are exact", "not booked savings", "signed"):
        assert phrase in cost["basis"]


def test_money_saved_goes_negative_when_the_run_cost_more_than_the_baseline():
    # Cheap baseline, dearer run: a surcharge, reported as one.
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    report = _account(_model("L"), settings)  # $3/Mtok input vs Haiku's $1
    cost = report["cost"]
    assert cost["usd"] == 3.0
    assert cost["baseline_usd"] == 1.0
    assert cost["avoided_usd"] == -2.0
    assert cost["avoided_pct"] == pytest.approx(-200.0)


def test_money_and_carbon_can_disagree_and_are_both_reported():
    # DeepSeek V4 Pro is the cheapest curated model *and* reasoning-tier, so
    # against a premium baseline it saves money while costing more carbon. This
    # is exactly the case a price-as-energy proxy would hide.
    settings = _settings(emissions_baseline_model="anthropic/claude-opus-4-8")
    model = CATALOG.get("openrouter/deepseek/deepseek-v4-pro")
    assert model.energy_class == "R"
    report = _account(model, settings, tokens=(0, 1_000_000, 0, 0))
    assert report["cost"]["avoided_usd"] > 0  # $0.87 vs $25.00 per Mtok output
    assert report["baseline"]["avoided_co2e_g"] < 0  # 21,000 vs 6,000 Wh/Mtok


def test_a_run_on_the_baseline_model_saved_no_money_either():
    settings = _settings(emissions_baseline_model="anthropic/claude-haiku-4-5")
    report = _account(CATALOG.get("anthropic/claude-haiku-4-5"), settings)
    assert report["cost"]["avoided_usd"] == 0.0
    assert report["cost"]["avoided_pct"] == 0.0


def test_no_baseline_means_no_money_comparison_rather_than_zero_saved():
    report = _account(_model("L"), _settings(emissions_baseline_model="nope/not-a-model"))
    assert report["cost"]["usd"] == 3.0  # the run's own cost is still known
    assert report["cost"]["baseline_usd"] is None
    assert report["cost"]["avoided_usd"] is None  # null, not 0
    assert report["baseline"]["avoided_usd"] is None


def test_a_free_local_model_reports_zero_cost_and_a_real_saving():
    report = _account(
        _local_model(), _settings(emissions_baseline_model="anthropic/claude-fable-5")
    )
    assert report["cost"]["usd"] == 0.0  # free in dollars
    assert report["cost"]["avoided_usd"] == 10.0
    assert report["co2e_g"] > 0  # never free in watts


# ── per-factor provenance ─────────────────────────────────────────────────────
_EXPECTED_FACTORS = {
    "energy_class",
    "token_weight_output",
    "token_weight_input",
    "token_weight_cache_read",
    "token_weight_cache_write",
    "pue",
    "grid_intensity",
    "embodied_hardware",
    "training_amortization",
    "token_prices",
    "uncertainty_band",
}


@pytest.mark.parametrize(
    "model, settings",
    [
        (_model("L"), _settings()),
        (_model("R"), _settings()),
        (_model("XL"), _settings(grid_co2e_basis="market_based")),
        (_local_model(), _settings(local_deployment_profile="onprem_datacenter")),
        (_local_model(), _settings(embodied_g_per_run=4.0)),
    ],
)
def test_every_factor_carries_its_provenance(model, settings):
    """The point of the block: "where did this number come from", answerable
    from the stored run alone, with nothing hardcoded in a frontend."""
    factors = _account(model, settings)["factors"]
    assert {f["key"] for f in factors} == _EXPECTED_FACTORS
    # A list, not a dict: JSONB does not preserve object key order, and the
    # provenance table must render the same way every time it is read.
    assert isinstance(factors, list)
    assert len({f["key"] for f in factors}) == len(factors)  # no duplicates
    for f in factors:
        for field in ("key", "label", "unit", "source", "url", "date", "confidence", "note"):
            assert field in f, (f["key"], field)
        assert f["label"] and f["source"] and f["note"], f["key"]
        assert f["confidence"] in {
            "exact",
            "structural",
            "calibrated",
            "low",
            "placeholder",
            "excluded",
        }, (f["key"], f["confidence"])


def test_provenance_values_are_the_values_actually_used():
    settings = _settings(datacenter_pue=1.33, grid_co2e_g_per_kwh=123.0)
    report = _account(_model("XL"), settings)
    factors = {f["key"]: f for f in report["factors"]}
    assert factors["energy_class"]["value"] == report["energy_wh_per_mtok"] == 6000.0
    assert factors["pue"]["value"] == report["pue"] == 1.33
    assert factors["grid_intensity"]["value"] == report["grid_co2e_g_per_kwh"] == 123.0
    assert factors["token_weight_input"]["value"] == report["input_weight"] == 0.05
    assert factors["uncertainty_band"]["value"] == [2.5, 2.5]


def test_the_calibrated_factors_cite_the_dataset_they_were_fitted_to():
    factors = {f["key"]: f for f in _account(_model("L"))["factors"]}
    for key in ("energy_class", "token_weight_input"):
        assert "arxiv.org/abs/2505.09598" in factors[key]["url"]
        assert factors[key]["date"] == "2025-05-14"
        assert factors[key]["confidence"] == "calibrated"
    assert factors["energy_class"]["anchor_model"] == "Claude 3.7 Sonnet"
    assert factors["energy_class"]["measured_anchor"] is True
    # XL is the one class with no measured anchor, and says so.
    xl = next(f for f in _account(_model("XL"))["factors"] if f["key"] == "energy_class")
    assert xl["anchor_model"] is None
    assert xl["measured_anchor"] is False
    assert "No measured anchor" in xl["note"]


def test_embodied_provenance_admits_it_is_a_placeholder_on_a_placeholder():
    factor = next(
        f for f in _account(_local_model())["factors"] if f["key"] == "embodied_hardware"
    )
    assert factor["confidence"] == "placeholder"
    assert factor["value"] == 0.0  # opt-in, and the default is stated as a value
    assert factor["gpu_h100_kg"] == 273.0
    assert factor["server_excluding_gpus_kg"] == 5700.0
    assert factor["lifetime_years"] == 3
    assert factor["batch_size"] == 64
    assert "assumed parity with CPU/RAM" in factor["note"]
    assert "30-50% margin of error" in factor["note"]


def test_training_amortization_is_an_explicit_documented_exclusion():
    factor = next(
        f for f in _account(_model("L"))["factors"] if f["key"] == "training_amortization"
    )
    assert factor["confidence"] == "excluded"
    assert factor["value"] == 0.0
    assert "four orders of magnitude" in factor["note"]
    assert "Inference only" in factor["note"]


def test_prices_are_the_only_factor_marked_exact():
    factors = _account(_model("L"))["factors"]
    exact = [f["key"] for f in factors if f["confidence"] == "exact"]
    assert exact == ["token_prices"]


def test_the_named_biases_travel_with_the_run():
    cloud = {c["key"] for c in _account(_model("L"))["caveats"]}
    local = {c["key"] for c in _account(_local_model())["caveats"]}
    assert "cloud_embodied_excluded" in cloud
    assert "unbatched_local_inference" not in cloud
    assert "unbatched_local_inference" in local
    assert "cloud_embodied_excluded" not in local
    for key in (
        "reasoning_token_accounting",
        "prompt_shape_residual",
        "same_token_counterfactual",
        "training_excluded",
        "out_of_scope_energy",
    ):
        assert key in cloud and key in local
    for caveat in _account(_model("L"))["caveats"]:
        assert caveat["direction"] in {"understates", "overstates", "either"}
        assert caveat["applies"] is True  # only applicable ones are emitted
        assert caveat["note"] and caveat["label"]


def test_the_prompt_shape_residual_is_quantified_not_waved_at():
    caveat = next(
        c for c in _account(_model("L"))["caveats"] if c["key"] == "prompt_shape_residual"
    )
    # Splitting input from output collapses a 4-6x flat-per-token artifact to
    # ~1.04-1.54x, except DeepSeek-R1 at 4.64x. The residual is named with its
    # numbers so a reader can check the claim.
    for phrase in ("2,100 -> 480", "1.04x", "4.64x"):
        assert phrase in caveat["note"]


# ── the embodied amortization helper ─────────────────────────────────────────
def test_the_embodied_helper_matches_the_documented_convention():
    # (1 x 273 kg + 5,700 kg) = 5,973 kg = 5.973e6 g, over 100,000 runs shared
    # at batch 64: 5.973e6 / (100,000 x 64) = 0.93328125 g per run.
    assert amortized_embodied_g_per_run(100_000) == pytest.approx(Decimal("0.93328125"))
    # 8 GPUs, no chassis: 8 x 273 kg = 2.184e6 g / (100,000 x 64).
    assert amortized_embodied_g_per_run(
        100_000, gpus=8, include_server=False
    ) == pytest.approx(Decimal("0.341250"))
    # Nothing here may divide by zero or invent carbon.
    assert amortized_embodied_g_per_run(0) == Decimal(0)
    assert amortized_embodied_g_per_run(100, batch_size=0) == Decimal(0)
    # It is a suggestion, never wired in: the default stays 0.
    assert _account(_local_model())["embodied_g"] == 0.0


def test_scope_split_is_callable_on_its_own():
    cloud = scope_split(DEPLOYMENT_CLOUD, Decimal("10"), Decimal("0"))
    assert (cloud["scope1_g"], cloud["scope2_g"], cloud["scope3_g"]) == (0.0, 0.0, 10.0)
    local = scope_split(DEPLOYMENT_LOCAL, Decimal("10"), Decimal("2"))
    assert (local["scope1_g"], local["scope2_g"], local["scope3_g"]) == (0.0, 10.0, 2.0)


# ── reading a stored block back: nulls stay null ─────────────────────────────
def test_summary_and_event_fields_are_null_when_there_is_no_estimate():
    for accounting in (None, {}, {"estimated": True}):
        summary = emission_summary_fields(accounting)
        assert summary == {
            "co2e_g": None,
            "scope2_g": None,
            "scope3_g": None,
            "avoided_co2e_g": None,
            # Added, and null on a legacy block for the same reason as the rest.
            "avoided_usd": None,
            "co2e_g_low": None,
            "co2e_g_high": None,
        }
        event = emission_event_fields(accounting)
        assert set(event) == {
            "co2e_g",
            "scope2_g",
            "scope3_g",
            "baseline_co2e_g",
            "avoided_co2e_g",
            "avoided_usd",
            "co2e_g_low",
            "co2e_g_high",
        }
        assert all(v is None for v in event.values())


def test_summary_and_event_fields_read_the_stored_values():
    report = _account(_model("L"))
    assert emission_summary_fields(report) == {
        "co2e_g": report["co2e_g"],
        "scope2_g": report["scopes"]["scope2_g"],
        "scope3_g": report["scopes"]["scope3_g"],
        "avoided_co2e_g": report["baseline"]["avoided_co2e_g"],
        "avoided_usd": report["baseline"]["avoided_usd"],
        "co2e_g_low": report["uncertainty"]["co2e_g_low"],
        "co2e_g_high": report["uncertainty"]["co2e_g_high"],
    }
    assert emission_event_fields(report)["baseline_co2e_g"] == report["baseline"]["co2e_g"]
    assert emission_event_fields(report)["avoided_usd"] == report["baseline"]["avoided_usd"]


def _run(**over) -> Run:
    base = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="divergence_assessment",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        iterations=3,
    )
    base.update(over)
    return Run(**base)


def test_run_summary_carries_the_scope_split_and_the_counterfactual():
    report = _account(_model("L"))
    summary = _run_summary(
        _run(cost_usd=Decimal("0.4"), energy_wh=Decimal("1200"), energy_accounting=report)
    )
    assert summary["co2e_g"] == report["co2e_g"]
    assert summary["scope2_g"] == 0.0  # a cloud run really did buy no power
    assert summary["scope3_g"] == report["co2e_g"]
    assert summary["avoided_co2e_g"] == report["baseline"]["avoided_co2e_g"]


def test_run_summary_keeps_missing_estimates_null():
    summary = _run_summary(_run(cost_usd=Decimal("0.4")))
    for key in ("energy_wh", "co2e_g", "scope2_g", "scope3_g", "avoided_co2e_g"):
        assert summary[key] is None, key


def test_a_run_recorded_before_scopes_existed_reports_null_not_zero():
    legacy = {"estimated": True, "energy_wh": 10.0, "co2e_g": 4.0, "grid_co2e_g_per_kwh": 400}
    summary = _run_summary(_run(energy_wh=Decimal("10"), energy_accounting=legacy))
    assert summary["co2e_g"] == 4.0
    assert summary["scope2_g"] is None
    assert summary["scope3_g"] is None
    assert summary["avoided_co2e_g"] is None


# ── the /emissions rollup ────────────────────────────────────────────────────
class _EmissionRows:
    """Returns run rows for the first query, harness names for the rest."""

    def __init__(self, rows):
        self.rows = rows
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return _Result(self.rows if len(self.statements) == 1 else [])


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return self.rows

    def scalars(self):
        return self

    def unique(self):
        return self


def _row(accounting, harness_id=None, model_used="anthropic/test", at=None):
    return (harness_id or uuid.uuid4(), model_used, accounting, at or utcnow())


async def test_rollup_sums_stored_values_and_is_not_recomputed(monkeypatch):
    """The flaw this endpoint exists to avoid: totals at *today's* factors.

    Two runs are recorded at 400 gCO2e/kWh. The operator then corrects the
    setting to 30. The window's totals must not move — they are history.
    """
    recorded = _settings(grid_co2e_g_per_kwh=400.0)
    reports = [
        _account(_model("L"), recorded),
        _account(_model("M"), recorded),
    ]
    expected_co2e = sum(r["co2e_g"] for r in reports)

    monkeypatch.setattr(analytics, "get_settings", lambda: _settings(grid_co2e_g_per_kwh=30.0))
    db = _EmissionRows([_row(reports[0]), _row(reports[1])])
    out = await emissions(project_id=None, days=30, user=None, db=db)

    assert out["totals"]["co2e_g"] == pytest.approx(expected_co2e)
    assert out["totals"]["runs_with_estimate"] == 2
    # Same grid factor on both runs, so the window has one honest basis...
    assert out["factors"]["mixed_factors"] is False
    # ...even though the *current* setting no longer matches what was recorded.
    assert out["factors"]["grid_co2e_g_per_kwh"] == 30.0
    assert out["factors"]["recorded"] == [
        {
            "deployment": "cloud",
            "grid_co2e_g_per_kwh": 400.0,
            "pue": 1.2,
            "grid_co2e_basis": "location_based",
            "runs": 2,
        }
    ]
    assert "for reference only" in out["factors"]["note"]


async def test_rollup_flags_a_window_recorded_under_differing_factors():
    cloud = _account(_model("L"), _settings(grid_co2e_g_per_kwh=400.0))
    greener = _account(_model("L"), _settings(grid_co2e_g_per_kwh=30.0))
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(cloud), _row(greener)])
    )
    assert out["factors"]["mixed_factors"] is True
    assert "MIXES RECORDING BASES" in out["disclaimer"]
    assert len(out["factors"]["recorded"]) == 2


async def test_a_window_mixing_cloud_and_self_hosted_is_also_flagged_and_split():
    settings = _settings()
    cloud = _account(_model("L"), settings)
    local = _account(_local_model(), settings)
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(cloud), _row(local)])
    )
    totals = out["totals"]
    assert totals["scope1_g"] == 0.0
    assert totals["scope2_g"] == pytest.approx(local["scopes"]["scope2_g"])
    assert totals["scope3_g"] == pytest.approx(cloud["scopes"]["scope3_g"])
    assert totals["co2e_g"] == pytest.approx(
        totals["scope1_g"] + totals["scope2_g"] + totals["scope3_g"]
    )
    # Cloud and local use different factors by design, so the window has no
    # single factor behind it and says so.
    assert out["factors"]["mixed_factors"] is True


async def test_runs_without_an_estimate_are_excluded_and_counted():
    report = _account(_model("L"))
    db = _EmissionRows([_row(report), _row(None), _row({"estimated": True}), _row("junk")])
    out = await emissions(project_id=None, days=30, user=None, db=db)
    totals = out["totals"]
    assert totals["runs"] == 4
    assert totals["runs_with_estimate"] == 1
    assert totals["runs_without_estimate"] == 3
    assert totals["co2e_g"] == pytest.approx(report["co2e_g"])
    assert totals["energy_wh"] == pytest.approx(report["energy_wh_total"])
    assert totals["energy_wh_compute"] == pytest.approx(report["energy_wh"])


async def test_legacy_runs_keep_their_carbon_but_not_a_fabricated_scope_split():
    legacy = {"estimated": True, "energy_wh": 10.0, "co2e_g": 4.0, "grid_co2e_g_per_kwh": 400}
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([_row(legacy)]))
    totals = out["totals"]
    assert totals["co2e_g"] == 4.0
    assert totals["runs_without_scope_split"] == 1
    assert totals["runs_without_baseline"] == 1
    assert (totals["scope1_g"], totals["scope2_g"], totals["scope3_g"]) == (0.0, 0.0, 0.0)
    assert totals["baseline_co2e_g"] == 0.0
    assert "predate it" in out["disclaimer"]
    # A row with no PUE recorded contributes its compute figure, not nothing.
    assert totals["energy_wh"] == 10.0


async def test_rollup_sums_money_and_the_band_alongside_carbon():
    settings = _settings(emissions_baseline_model="anthropic/claude-fable-5")
    reports = [
        _account(_model("L", id="anthropic/heavy"), settings),
        _account(_model("M", id="anthropic/light"), settings),
    ]
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(r) for r in reports])
    )
    totals = out["totals"]
    assert totals["avoided_usd"] == pytest.approx(
        sum(r["baseline"]["avoided_usd"] for r in reports)
    )
    # Bands are summed low-with-low: the same class table and grid factor are
    # being applied to every run, so they are wrong together, not independently.
    assert totals["co2e_g_low"] == pytest.approx(
        sum(r["uncertainty"]["co2e_g_low"] for r in reports)
    )
    assert totals["co2e_g_high"] == pytest.approx(
        sum(r["uncertainty"]["co2e_g_high"] for r in reports)
    )
    assert totals["co2e_g_low"] < totals["co2e_g"] < totals["co2e_g_high"]
    assert totals["runs_without_money_comparison"] == 0
    assert totals["runs_without_uncertainty_band"] == 0
    assert out["by_model"][0]["avoided_usd"] == pytest.approx(
        max(reports, key=lambda r: r["co2e_g"])["baseline"]["avoided_usd"]
    )


async def test_a_legacy_run_contributes_its_central_figure_to_both_band_ends():
    legacy = {"estimated": True, "energy_wh": 10.0, "co2e_g": 4.0, "grid_co2e_g_per_kwh": 400}
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([_row(legacy)]))
    totals = out["totals"]
    assert totals["runs_without_uncertainty_band"] == 1
    assert totals["runs_without_money_comparison"] == 1
    # Not zero: a run recorded before the band existed still emitted something,
    # so it stands at its central figure rather than collapsing the window.
    assert totals["co2e_g_low"] == totals["co2e_g_high"] == 4.0
    assert totals["avoided_usd"] == 0.0


async def test_a_window_mixing_ghg_protocol_grid_bases_is_flagged():
    # Location-based and market-based factors are not summable under the GHG
    # Protocol even when the numbers happen to match, so the basis is part of the
    # factor key rather than an annotation.
    location = _account(_local_model(), _settings(local_grid_co2e_g_per_kwh=100.0))
    market = _account(
        _local_model(),
        _settings(local_grid_co2e_g_per_kwh=100.0, local_grid_co2e_basis="market_based"),
    )
    out = await emissions(
        project_id=None, days=30, user=None, db=_EmissionRows([_row(location), _row(market)])
    )
    assert out["factors"]["mixed_factors"] is True
    assert sorted(r["grid_co2e_basis"] for r in out["factors"]["recorded"]) == [
        "market_based",
        "unspecified",
    ]
    assert "location-based with market-based" in out["disclaimer"]


async def test_rollup_points_at_per_run_provenance_rather_than_its_own_settings():
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([]))
    factors = out["factors"]
    assert factors["grid_co2e_basis"] == "location_based"
    assert factors["onprem_pue"] == 1.56
    assert factors["uncertainty_band_low"] == factors["uncertainty_band_high"] == 2.5
    assert "energy_accounting.factors" in factors["provenance_note"]
    for phrase in ("JUDGMENT BAND", "per-token prices are exact"):
        assert phrase in out["disclaimer"]


async def test_rollup_groups_by_model_harness_and_day():
    settings = _settings()
    heavy = _account(_model("XL", id="anthropic/heavy"), settings)
    light = _account(_model("M", id="anthropic/light"), settings)
    harness = uuid.uuid4()
    yesterday = utcnow() - timedelta(days=1)
    db = _EmissionRows(
        [
            _row(heavy, harness_id=harness, at=yesterday),
            _row(light, harness_id=harness, at=utcnow()),
        ]
    )
    out = await emissions(project_id=None, days=30, user=None, db=db)

    assert [m["model"] for m in out["by_model"]] == ["anthropic/heavy", "anthropic/light"]
    assert out["by_model"][0]["energy_class"] == "XL"
    assert out["by_model"][0]["co2e_g"] == pytest.approx(heavy["co2e_g"])
    assert out["by_model"][0]["avoided_co2e_g"] == pytest.approx(
        heavy["baseline"]["avoided_co2e_g"]
    )
    assert len(out["by_harness"]) == 1
    assert out["by_harness"][0]["runs"] == 2
    assert out["by_harness"][0]["harness_name"] == "(deleted harness)"
    assert [d["date"] for d in out["by_day"]] == sorted(d["date"] for d in out["by_day"])
    assert len(out["by_day"]) == 2


async def test_avoided_pct_is_signed_and_safe_when_there_is_no_baseline():
    empty = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([]))
    assert empty["totals"] == {
        "runs": 0,
        "runs_with_estimate": 0,
        "runs_without_estimate": 0,
        "runs_without_scope_split": 0,
        "runs_without_baseline": 0,
        "runs_without_money_comparison": 0,
        "runs_without_uncertainty_band": 0,
        "energy_wh": 0.0,
        "energy_wh_compute": 0.0,
        "co2e_g": 0.0,
        "scope1_g": 0.0,
        "scope2_g": 0.0,
        "scope3_g": 0.0,
        "baseline_co2e_g": 0.0,
        "avoided_co2e_g": 0.0,
        "avoided_pct": 0.0,
        "avoided_usd": 0.0,
        "co2e_g_low": 0.0,
        "co2e_g_high": 0.0,
    }
    heavier = _account(_model("XL"), _settings(emissions_baseline_model="anthropic/claude-haiku-4-5"))
    out = await emissions(project_id=None, days=30, user=None, db=_EmissionRows([_row(heavier)]))
    assert out["totals"]["avoided_co2e_g"] < 0
    assert out["totals"]["avoided_pct"] < 0


async def test_rollup_declares_its_scan_bound_and_never_claims_measurement():
    out = await emissions(project_id=None, days=7, user=None, db=_EmissionRows([]))
    assert out["window_days"] == 7
    assert out["estimated"] is True
    assert out["scan"] == {
        "limit": analytics.EMISSIONS_RUN_SCAN_LIMIT,
        "rows_scanned": 0,
        "truncated": False,
    }
    for phrase in (
        "ESTIMATES, NOT MEASUREMENTS",
        "NOT recomputed at current settings",
        "not an offset",
        "not usable for statutory",
    ):
        assert phrase in out["disclaimer"]


async def test_rollup_query_is_bounded_and_filtered_without_json_predicates():
    from sqlalchemy.dialects import postgresql

    db = _EmissionRows([])
    await emissions(project_id=uuid.uuid4(), days=30, user=None, db=db)
    compiled = str(db.statements[0].compile(dialect=postgresql.dialect()))
    assert "LIMIT" in compiled
    assert "runs.created_at >=" in compiled
    assert "runs.project_id =" in compiled
    assert "->" not in compiled  # no dialect-specific JSON access


def test_recorded_reader_ignores_anything_that_is_not_a_block():
    for junk in (None, "nope", 42, [], {}, {"co2e_g": None}):
        assert _recorded_emissions(junk) is None


# ── the methodology doc must match the code ──────────────────────────────────
# This doc is rendered in the product UI, so a drifted constant in it is a
# published wrong number, not a stale comment.
_METHODOLOGY = Path(__file__).resolve().parents[2] / "docs" / "emissions-methodology.md"


def _methodology_text() -> str:
    """The doc with whitespace flattened, so a hard-wrapped line still matches."""
    return " ".join(_METHODOLOGY.read_text().split())


def _documents(text: str, value) -> bool:
    """Is this constant written in the doc, in prose form or with a `.0` tail?"""
    if isinstance(value, float) and value.is_integer():
        return f"{int(value)}" in text or f"{int(value):,}" in text or str(value) in text
    return str(value) in text


def test_the_doc_carries_every_class_constant_and_its_anchor():
    from bench.services.emissions import ENERGY_CLASS_CALIBRATION, ENERGY_CLASS_WH_PER_MTOK

    text = _methodology_text()
    for cls, value in ENERGY_CLASS_WH_PER_MTOK.items():
        rendered = f"{int(value):,}"
        assert rendered in text, f"class {cls} value {rendered} missing from the doc"
        anchor = ENERGY_CLASS_CALIBRATION[cls]["anchor_model"]
        if anchor:
            assert anchor in text, f"class {cls} anchor {anchor} missing from the doc"
            fitted = ENERGY_CLASS_CALIBRATION[cls]["fitted_wh_per_mtok"]
            assert f"{fitted:,}" in text, f"fitted b {fitted} for {cls} missing from the doc"
        else:
            assert "no measured anchor" in text.lower()


def test_the_doc_reproduces_the_regression_inputs():
    from bench.services.emissions import JEGHAM_2025

    text = _methodology_text()
    assert JEGHAM_2025["url"] in text
    assert "2505.09598" in text
    # Every measured point a reader would need to redo the fit themselves.
    for values in JEGHAM_2025["wh_per_query"].values():
        for wh in values:
            assert f"{wh:.2f}" in text, f"data point {wh} missing from the doc"
    for model in JEGHAM_2025["wh_per_query"]:
        assert model in text
    # And the normal-equation sums, so the arithmetic is checkable by hand.
    for total in ("101,010,000", "16,030,000", "3,340,000"):
        assert total in text


def test_the_doc_carries_every_other_default_the_code_uses():
    from bench.services.emissions import (
        EMBODIED_REFERENCE,
        ENERGY_TOKEN_WEIGHTS,
        PUE_REFERENCE,
        UNCERTAINTY_BAND_FACTOR_DEFAULT,
    )

    text = _methodology_text()
    settings = _settings()
    for weight in ENERGY_TOKEN_WEIGHTS.values():
        assert str(weight) in text, f"token weight {weight} missing from the doc"
    for key in ("datacenter_pue", "local_pue", "onprem_pue", "grid_co2e_g_per_kwh"):
        assert _documents(text, getattr(settings, key)), f"{key} default missing from the doc"
    for name, ref in PUE_REFERENCE.items():
        assert _documents(text, ref["value"]), f"published PUE for {name} missing from the doc"
    assert f"{int(EMBODIED_REFERENCE['gpu_h100_kg'])} kgCO2eq" in text
    assert f"{int(EMBODIED_REFERENCE['server_excluding_gpus_kg']):,} kgCO2eq" in text
    assert str(UNCERTAINTY_BAND_FACTOR_DEFAULT) in text
    # The bases, spelled the way the JSON spells them.
    for basis in GRID_BASES:
        assert basis in text


def test_the_doc_states_the_exclusions_and_refuses_to_overclaim():
    text = _methodology_text()
    for phrase in (
        "NOT a confidence interval",
        "Training is not allocated",
        "Cloud embodied hardware is not counted",
        "Reasoning tokens may not be in the counted output",
        "placeholder resting on a placeholder",
        "four orders of magnitude",
        "not reportable",
        "Anthropic publishes nothing",
        "Replacing a default with your own factor",
    ):
        assert phrase in text, f"the doc no longer says: {phrase}"


# ── auth ─────────────────────────────────────────────────────────────────────
def test_emissions_requires_authentication():
    app = FastAPI()
    app.include_router(analytics.router)
    client = TestClient(app)
    assert client.get("/api/analytics/emissions").status_code == 401


def test_emissions_is_readable_by_any_authenticated_user():
    from bench.api.auth import current_user
    from bench.db.engine import get_db

    app = FastAPI()
    app.include_router(analytics.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: _EmissionRows([])
    client = TestClient(app)
    body = client.get("/api/analytics/emissions?days=7").json()
    assert body["window_days"] == 7
    assert body["totals"]["runs"] == 0
