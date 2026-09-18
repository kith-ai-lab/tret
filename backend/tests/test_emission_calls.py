from decimal import Decimal

from tret.engine.harness import ModelSegment
from tret.providers.catalog import ModelInfo
from tret.providers.base import Usage
from tret.services.emission_calls import account_call_records
from tret.services.emission_factors import build_factor_set, factor_set_for_call


def model():
    return ModelInfo(
        id="anthropic/test", provider="anthropic", wire_id="test", display_name="Test",
        context_window=100_000, input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"), energy_class="L", cost_tier="standard",
    )


MANAGED = {"pue": {"upstreams": {
    "aws": {"value": 1.14, "label": "AWS", "url": "https://aws.amazon.com/sustainability/",
            "as_of": "2025-12-31"},
    "google": {"value": 1.09, "label": "Google",
               "url": "https://www.datacenters.google/efficiency/", "as_of": "2024-12-31"},
}}}


def record(iteration, *, served_by=None, output=10, reasoning=None, semantics=None):
    return {"iteration": iteration, "input_tokens": 100, "output_tokens": output,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "reasoning_tokens": reasoning, "reasoning_accounting": semantics,
            "served_by": served_by, "inference_geo": "us-east",
            "usage_status": "reported"}


def totals(count=3, output=10):
    return {"input_tokens": 100 * count, "output_tokens": output * count,
            "cache_read_tokens": 0, "cache_write_tokens": 0}


def test_per_call_upstream_pue_and_totals_are_conserved():
    factors = build_factor_set(provider="anthropic", managed_settings=MANAGED)
    result = account_call_records(
        model(), [record(1, served_by="aws"), record(2, served_by="google"), record(3)],
        billed_usage=totals(), factors=factors,
    )
    assert [row["pue"] for row in result["call_accountings"]] == [1.14, 1.09, 1.2]
    assert result["tokens"] == {
        "input": 300, "output": 30, "cache_read": 0, "cache_write": 0
    }
    assert result["functional_unit"] == "one_run"
    assert result["call_accountings"][0]["inference_geo"] == "us-east"
    pue_factor = next(factor for factor in result["factors"] if factor["key"] == "pue")
    assert pue_factor["value"] is None
    assert pue_factor["source"] == "mixed"
    assert {variant["value"] for variant in pue_factor["variants"]} == {1.14, 1.09, 1.2}


def test_openrouter_google_vertex_identity_selects_google_disclosure():
    factors = build_factor_set(provider="openrouter", managed_settings=MANAGED)
    result = account_call_records(
        model(), [record(1, served_by="google-vertex")],
        billed_usage=totals(1), factors=factors,
    )
    assert result["pue"] == 1.09
    assert result["call_accountings"][0]["served_by"] == "google-vertex"


def test_explicit_generic_workspace_pue_beats_managed_upstream():
    factors = build_factor_set(
        provider="anthropic", managed_settings=MANAGED,
        workspace_settings={"pue": {"cloud": 1.3, "label": "contract PUE"}},
    )
    call = factor_set_for_call(
        factors, provider="anthropic", model_id="anthropic/test", served_by="aws"
    )
    assert call.pue.value == Decimal("1.3")
    assert call.pue.disclosure is None


def test_flat_embodied_and_baseline_are_allocated_once():
    factors = build_factor_set(
        provider="anthropic",
        workspace_settings={"embodied": {"g_per_run": 2, "label": "allocation"}},
    )
    result = account_call_records(
        model(), [record(1), record(2)], billed_usage=totals(2), factors=factors,
    )
    assert result["embodied_g"] == 2
    assert 2 < result["baseline"]["co2e_g"] < 4
    assert all(caveat["key"] != "multi_model_run" for caveat in result["caveats"])
    assert "calls to one model" in result["basis"]
    assert "run_override" not in result["factor_layers"]
    embodied = next(item for item in result["factors"] if item["key"] == "embodied_hardware")
    assert embodied["value"] == 2
    assert embodied["layer"] == "workspace"


def test_internal_zero_allocation_does_not_invent_an_override_layer():
    result = account_call_records(
        model(), [record(1), record(2)], billed_usage=totals(2),
        factors=build_factor_set(provider="anthropic"),
    )
    assert "run_override" not in result["factor_layers"]
    embodied = next(item for item in result["factors"] if item["key"] == "embodied_hardware")
    assert embodied["value"] == 0
    assert embodied["layer"] == "global_default"


def test_additional_reasoning_changes_energy_but_not_billed_cost():
    factors = build_factor_set(provider="anthropic")
    plain = account_call_records(
        model(), [record(1)], billed_usage=totals(1), factors=factors,
    )
    reasoned = account_call_records(
        model(), [record(1, reasoning=100, semantics="additional")],
        billed_usage=totals(1), factors=factors,
    )
    assert reasoned["energy_wh"] > plain["energy_wh"]
    assert reasoned["cost"]["usd"] == plain["cost"]["usd"]
    assert reasoned["reasoning_coverage"]["known_additional_tokens"] == 100


def test_missing_or_unreconciled_records_return_none():
    factors = build_factor_set(provider="anthropic")
    assert account_call_records(model(), [], billed_usage=totals(0), factors=factors) is None
    assert account_call_records(
        model(), [record(1) | {"usage_status": "unavailable"}],
        billed_usage=totals(1), factors=factors,
    ) is None
    assert account_call_records(
        model(), [record(1)], billed_usage=totals(2), factors=factors,
    ) is None


def test_model_segment_accounts_single_call_with_its_upstream_pue():
    factors = build_factor_set(provider="anthropic", managed_settings=MANAGED)
    segment = ModelSegment(model=model(), reason="test", factors=factors)
    segment.add(
        Usage(input_tokens=100, output_tokens=10),
        1,
        served_by="aws",
        inference_geo="us-east",
    )

    result = segment.accounting()

    assert result["pue"] == 1.14
    assert result["call_accountings"][0]["served_by"] == "aws"
    assert result["call_accountings"][0]["inference_geo"] == "us-east"


def test_long_call_averages_grid_across_hour_boundary_and_preserves_cost():
    from datetime import datetime, timezone

    hourly = "hour_utc,g_per_kwh\n" + "\n".join(
        f"{hour},{100 if hour == 0 else 500}" for hour in range(24)
    )
    workspace = {"grid": {
        "default": {"g_per_kwh": 400, "basis": "location_based", "label": "fallback",
                    "table": "site"},
        "tables": {"site": {"csv": hourly, "basis": "location_based", "label": "site"}},
    }}
    factors = build_factor_set(
        provider="anthropic", workspace_settings=workspace,
        at=datetime(2026, 1, 1, 0, 59, tzinfo=timezone.utc),
    )
    call = record(1) | {"started_at": "2026-01-01T00:59:00+00:00",
                        "ended_at": "2026-01-01T01:01:00+00:00"}
    result = account_call_records(model(), [call], billed_usage=totals(1), factors=factors)
    assert result["grid_co2e_g_per_kwh"] == 300
    assert result["grid_temporal"] == "interval_weighted"
    assert result["baseline"]["grid_co2e_g_per_kwh"] == 300
    assert result["temporal_allocation"] == "constant_power_within_each_call"
    assert result["cost"]["usd"] == float(model().cost_usd(100, 10))


def test_invalid_call_interval_falls_back_without_inventing_timing():
    factors = build_factor_set(provider="anthropic")
    call = record(1) | {"started_at": "2026-01-01T00:59:00", "ended_at": "broken"}
    assert account_call_records(model(), [call], billed_usage=totals(1), factors=factors) is None
