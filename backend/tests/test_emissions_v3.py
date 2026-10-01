"""Golden and unit tests for the facility_v3 method (Alex method lab, pin a0b831b)."""
from __future__ import annotations

import json
import math

import pytest

from tret.services.emissions_v3 import (
    V3Inputs,
    V3Tokens,
    compute_facility_v3,
    curve_rate_wh_per_mtok,
    load_calibration,
    lookup_classification,
    machines,
)

TOK = V3Tokens(input=8000, output=1200, cache_read=20000, cache_write=2000)


def _job(**kw) -> V3Inputs:
    base = dict(model_id="anthropic/claude-sonnet-5", maker="anthropic", tokens=TOK,
                reasoning_default_enabled=True)
    base.update(kw)
    return V3Inputs(**base)


def test_worked_job_golden():
    r = compute_facility_v3(_job())
    p = r["parts"]
    assert p["weighted_tokens"] == pytest.approx(4056)
    assert p["rate_wh_per_mtok"] == pytest.approx(1072, rel=0.005)
    assert p["node_wh_before_reasoning"] == pytest.approx(4.349, rel=0.005)
    assert p["node_wh"] == pytest.approx(5.654, rel=0.005)
    assert p["facility_wh"] == pytest.approx(7.734, rel=0.005)
    assert p["grid_g_per_kwh"] == pytest.approx(406.0, rel=0.005)
    assert p["operational_g"] == pytest.approx(3.140, rel=0.005)
    assert p["embodied_g"] == pytest.approx(0.3403, rel=0.005)
    assert p["total_g"] == pytest.approx(3.480, rel=0.005)
    b = r["band"]
    assert b["low_div"] == pytest.approx(4.316, rel=0.005)
    assert b["high_mult"] == pytest.approx(5.060, rel=0.005)
    assert b["low_g"] == pytest.approx(0.806, rel=0.005)
    assert b["high_g"] == pytest.approx(17.61, rel=0.005)
    assert b["envelope_low_g"] == pytest.approx(0.101, rel=0.02)
    assert b["envelope_high_g"] == pytest.approx(251, rel=0.02)
    assert r["placement"]["rung"] == 3
    assert r["grid"]["rung"] == "provider_geography"
    assert "The vast majority of the new compute" in r["grid"]["basis"]
    assert r["method_id"] == "facility_v3" and r["pin"] == "a0b831b"


def test_worked_job_shadows():
    sh = {k: v["total_g"] for k, v in compute_facility_v3(_job())["shadows"].items()}
    assert sh["weights_0.05_set"] == pytest.approx(1.545, rel=0.01)
    assert sh["rate_q16"] == pytest.approx(4.923, rel=0.01)
    assert sh["fleet_1.31"] == pytest.approx(3.80, rel=0.01)
    assert sh["reasoning_2.2"] == pytest.approx(5.89, rel=0.01)
    assert sh["grid_world"] == pytest.approx(4.13, rel=0.01)
    assert sh["reasoning_off"] == pytest.approx(2.68, rel=0.01)
    assert sh["host_1.43"] == pytest.approx(3.53, rel=0.01)
    assert "embodied_ratio_0.111" in sh


def test_medium_archetype_golden():
    r = compute_facility_v3(_job(serving_provider="Amazon Bedrock", region_pin=("aws", "us-east-1")))
    assert r["parts"]["pue"] == 1.15
    assert r["grid"]["rung"] == "operator_pin"
    assert r["grid"]["location_based"]["g_per_kwh"] == pytest.approx(384.4)
    assert r["parts"]["td_multiplier"] == pytest.approx(1.0561, rel=1e-3)
    assert r["parts"]["total_g"] == pytest.approx(3.51, rel=0.02)
    assert r["band"]["low_g"] == pytest.approx(1.10, rel=0.02)
    assert r["band"]["high_g"] == pytest.approx(14.3, rel=0.02)
    names = {f["name"] for f in r["band"]["factors"]}
    assert "pue" not in names and "td_losses" in names
    td = next(f for f in r["band"]["factors"] if f["name"] == "td_losses")
    assert td["high_eff"] == 1.0 and td["low_eff"] < 1.0


def test_output_is_json_serialisable():
    json.dumps(compute_facility_v3(_job()))
    json.dumps(compute_facility_v3(_job(model_id="x/unknown", cost_tier="premium")))


def test_machines_rounding():
    assert machines(293, 8) == 8
    assert machines(293, 16) == 16
    assert machines(7, 8) == 1


def test_live_curve_reproduces_1087():
    rate, q16 = curve_rate_wh_per_mtok(293, 88, None)
    assert rate == pytest.approx(1087, rel=0.002)
    assert q16 == pytest.approx(rate * math.sqrt(2), rel=1e-9)
    # known precision uses that Q only
    r8, none = curve_rate_wh_per_mtok(293, 88, 8)
    assert none is None and r8 == pytest.approx(rate / math.sqrt(2), rel=1e-9)


def test_live_curve_placement_for_unlisted_model():
    r = compute_facility_v3(V3Inputs("vendor/new-closed", maker="other", total_params_b=293,
                                     active_params_b=88, tokens=TOK))
    assert r["placement"]["rung"] == 3
    assert r["parts"]["rate_wh_per_mtok"] == pytest.approx(1087 * 1.41 / 1.43, rel=0.003)
    exact = compute_facility_v3(V3Inputs("vendor/new-open", maker="other", total_params_b=70,
                                         precision_bits=8, params_exact=True, tokens=TOK))
    assert exact["placement"]["rung"] == 2
    assert "precision assumed" not in exact["placement"]["flags"]


def test_suffix_stripping_in_classification_lookup():
    base = lookup_classification("anthropic/claude-sonnet-5")
    assert base is not None
    assert lookup_classification("anthropic/claude-sonnet-5:batch") == base
    assert lookup_classification("anthropic/claude-sonnet-5:free") == base
    assert lookup_classification("anthropic/claude-sonnet-5:nope") is None
    assert len(load_calibration()["classification"]) == 359


def test_rung5_price_tier_fallback():
    r = compute_facility_v3(V3Inputs("vendor/mystery", maker="other", cost_tier="premium", tokens=TOK))
    assert r["placement"]["rung"] == 5
    assert "class from price" in r["placement"]["flags"]
    assert r["parts"]["rate_wh_per_mtok"] == pytest.approx(1016.0 * 1.41 / 1.43)
    s = compute_facility_v3(V3Inputs("vendor/mystery", maker="other", cost_tier="local", tokens=TOK))
    assert s["parts"]["rate_wh_per_mtok"] == pytest.approx(59.6 * 1.41 / 1.43)
    assert s["parts"]["fleet"] == 1.6 and s["parts"]["pue"] == 1.54  # unknown third-party host


def test_metered_self_host_rung1():
    r = compute_facility_v3(V3Inputs("meta-llama/llama-3.1-8b-instruct", deployment="self_hosted_workstation",
                                     metered_energy_wh=2.0, tokens=TOK, reasoning_mode=True,
                                     region_pin=("aws", "us-east-1")))
    p = r["parts"]
    assert r["placement"]["rung"] == 1 and r["regime"] == "metered self-host"
    assert p["node_wh"] == 2.0 == p["node_wh_before_reasoning"]  # no step, no uplift, no reasoning
    assert p["fleet"] == 1.0 and p["pue"] == 1.05
    assert p["facility_wh"] == pytest.approx(2.1)
    assert r["placement"]["host_uplift"] == 1.0
    names = {f["name"] for f in r["band"]["factors"]}
    assert not names & {"regime_x_hardware", "host_uplift", "fleet", "pue", "reasoning_uplift"}
    assert r["band"]["floor"] is None  # rung 1: no floor
    assert r["band"]["low_div"] < 2.166 and r["band"]["high_mult"] < 2.166


def test_floor_clamps_curve_placed_run():
    # Pinned region + Google host strips most factors; the floor must still hold.
    r = compute_facility_v3(V3Inputs("vendor/m", maker="google", serving_provider="Google Vertex",
                                     cost_tier="premium", tokens=V3Tokens(output=1000),
                                     region_pin=("gcp", "us-central1")))
    b = r["band"]
    assert b["floor"] == 2.166
    assert b["low_div"] == pytest.approx(2.166) or b["low_div"] > 2.166
    assert b["floor_applied"]["low"] or b["floor_applied"]["high"]
    assert b["high_mult"] >= 2.166 and b["low_div"] >= 2.166


def test_google_served_host_is_1_43():
    r = compute_facility_v3(V3Inputs("google/gemini-x", maker="google", serving_provider="Google Vertex",
                                     cost_tier="standard", tokens=TOK))
    assert r["placement"]["host_uplift"] == 1.43
    assert r["parts"]["rate_wh_per_mtok"] == pytest.approx(363.7)
    assert r["parts"]["pue"] == 1.09
    assert "host_uplift" not in {f["name"] for f in r["band"]["factors"]}
    assert r["grid"]["rung"] == "provider_weighted"
    assert r["grid"]["location_based"]["g_per_kwh"] == 345
    assert r["grid"]["market_based"]["g_per_kwh"] == 94
    assert r["parts"]["td_multiplier"] == 1.0  # no T&D on a provider figure


def test_td_fallback_to_wld_for_eu():
    r = compute_facility_v3(_job(openrouter_data_region="europe"))
    assert r["grid"]["rung"] == "data_region"
    assert r["grid"]["location_based"]["g_per_kwh"] == pytest.approx(209.21)
    assert r["parts"]["td_multiplier"] == pytest.approx(1 / (1 - 0.06506), rel=1e-4)
    assert any("WLD T&D" in f for f in r["placement"]["flags"])


def test_openai_world_and_hidden_reasoning_hedge():
    r = compute_facility_v3(V3Inputs("openai/gpt-x", maker="openai", cost_tier="premium", tokens=TOK,
                                     reasoning_mode=True, reasoning_count_hidden=True))
    assert r["grid"]["location_based"]["g_per_kwh"] == pytest.approx(458.49)
    assert r["parts"]["pue"] == 1.17
    assert r["parts"]["weighted_tokens"] == pytest.approx(4056 + 12000)
    assert "hidden reasoning hedge (tier 3)" in r["placement"]["flags"]


def test_bedrock_claude_without_pin_uses_maker_geography():
    r = compute_facility_v3(_job(serving_provider="Amazon Bedrock"))
    assert r["grid"]["rung"] == "provider_geography"
    assert r["grid"]["location_based"]["geography"] == "USA"
    assert "The vast majority of the new compute" in r["grid"]["basis"]
    assert r["parts"]["pue"] == 1.14  # still the AWS fleet rung
    # a non-Anthropic/OpenAI maker on Bedrock still falls to World
    w = compute_facility_v3(V3Inputs("meta-llama/x", maker="meta", serving_provider="Amazon Bedrock",
                                     cost_tier="standard", tokens=TOK))
    assert w["grid"]["rung"] == "world"


def test_canonical_model_id_rules():
    from tret.services.emissions_v3 import canonical_model_id

    assert canonical_model_id("anthropic/claude-haiku-4-5") == "anthropic/claude-haiku-4.5"
    assert canonical_model_id("anthropic/claude-fable-5-1:batch") == "anthropic/claude-fable-5.1:batch"
    assert canonical_model_id("kimi/kimi-k2") == "moonshotai/kimi-k2"
    assert canonical_model_id("openrouter/openai/gpt-5.6-terra") == "openai/gpt-5.6-terra"
    assert canonical_model_id("anthropic/claude-sonnet-5") == "anthropic/claude-sonnet-5"
    for raw, row in (("anthropic/claude-opus-4-8", "anthropic/claude-opus-4.8"),
                     ("anthropic/claude-fable-5-1", "anthropic/claude-fable-5.1")):
        assert lookup_classification(raw) == lookup_classification(row) is not None


def test_curated_models_resolve_in_the_classification_table():
    """Every curated id with an OpenRouter equivalent resolves; the ids that fall to
    rung 5 are listed so a silent miss shows up as a diff here."""
    from tret.providers.catalog import ModelCatalog
    from tret.services.emissions_v3_wiring import _v3_model_id

    misses = sorted(
        m.id for m in ModelCatalog().all(curated_only=True)
        if lookup_classification(_v3_model_id(m, None)) is None
    )
    # No classification row exists for these (checked against the table, 2026-10-01).
    assert misses == ["anthropic/claude-opus-5-5", "kimi/kimi-latest"]


def test_pinned_region_with_no_ember_country_falls_to_world_not_provider_geography():
    r = compute_facility_v3(_job(serving_provider="Amazon Bedrock", region_pin=("aws", "me-central-1")))
    assert r["grid"]["rung"] == "world"
    assert r["grid"]["location_based"]["geography"] == "WLD"
    assert r["grid"]["location_based"]["g_per_kwh"] == pytest.approx(458.49)
    assert r["parts"]["td_multiplier"] == pytest.approx(
        1.0 / (1.0 - load_calibration()["td"]["WLD"]["loss_pct"] / 100.0))
    assert "pinned region country not in Ember table" in r["placement"]["flags"]
