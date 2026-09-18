from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tret.config import Settings
from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.services.emission_factors import build_factor_set
from tret.services.emissions import combine_accountings, energy_accounting


CATALOG = ModelCatalog()


def test_harness_persists_gpu_measurement_boundary():
    from tret.engine.harness import ModelSegment
    from tret.services.energy_meter import MeterReading

    segment = ModelSegment(model=_model(), reason="initial")
    segment.meter_reading = MeterReading(
        wh=Decimal("2"), samples=2, duration_s=5, kind="nvidia_smi",
        note=None, shared_device=True, energy_boundary="gpu",
    )
    report = segment.to_json()["energy_accounting"]
    assert report["energy_boundary"] == report["energy_meter"]["energy_boundary"] == "gpu"
    assert report["energy_boundary_complete"] is False
    assert report["energy_wh"] == 2


def _model(**over) -> ModelInfo:
    values = dict(
        id="anthropic/test-boundary",
        provider="anthropic",
        wire_id="test",
        display_name="Test",
        context_window=100_000,
        input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("2"),
        cost_tier="standard",
        energy_class="L",
    )
    values.update(over)
    return ModelInfo(**values)


def test_facility_measurement_is_not_pue_multiplied_twice():
    report = energy_accounting(
        _model(), 1000, 100, settings=Settings(datacenter_pue=1.8),
        measured_energy_wh=12.5, measured_energy_boundary="facility",
    )
    assert report["energy_wh"] == report["energy_wh_total"] == 12.5
    assert report["configured_pue"] == 1.8
    assert report["pue"] == 1.0
    assert report["pue_applied"] is False
    assert report["energy_boundary"] == "facility"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), -0.01])
def test_measured_energy_rejects_nonfinite_and_negative_values(value):
    with pytest.raises(ValueError, match="finite and >= 0"):
        energy_accounting(_model(), 1, 1, measured_energy_wh=value)


def test_gpu_and_legacy_ladder_disclose_incomplete_boundaries():
    measured = energy_accounting(
        _model(), 1, 1, measured_energy_wh=1, measured_energy_boundary="gpu"
    )
    assert measured["energy_boundary"] == "gpu"
    assert measured["energy_boundary_complete"] is False
    assert "non_gpu_it" in measured["excluded_components"]

    from tret.services.emission_factors import build_factor_set
    legacy = energy_accounting(
        _model(), 1, 1,
        factors=build_factor_set(
            provider="anthropic", run_overrides={"energy_strategy": "class_ladder_v1"}
        ),
    )
    assert legacy["method_id"] == "class_ladder_v1"
    assert legacy["energy_boundary"] == "unknown"
    assert legacy["energy_source_by_component"] == {
        "unresolved_energy_boundary": "modeled"
    }


@pytest.mark.parametrize("boundary", ["facility", "partial", "unknown"])
def test_measured_boundaries_that_cannot_accept_pue_leave_raw_wh_unchanged(boundary):
    report = energy_accounting(
        _model(), 1, 1, settings=Settings(datacenter_pue=1.8),
        measured_energy_wh=6, measured_energy_boundary=boundary,
    )
    assert report["energy_wh_total"] == report["energy_wh"] == 6
    assert report["pue_applied"] is False
    pue = next(c for c in report["uncertainty"]["contributions"] if c["key"] == "pue")
    assert (pue["low_multiplier"], pue["high_multiplier"]) == (1.0, 1.0)
    assert report["method_id"] == "measured_energy_v1"


def test_rollup_keeps_mixed_energy_boundaries_explicit():
    node = energy_accounting(_model(), 1, 1, measured_energy_wh=1)
    facility = energy_accounting(
        _model(id="anthropic/test-2"), 1, 1,
        measured_energy_wh=2, measured_energy_boundary="facility",
    )
    combined = combine_accountings([node, facility])
    assert combined["energy_boundary"] == "mixed"
    assert combined["energy_boundary_complete"] is True


def test_baseline_reuses_workspace_ladder_but_resolves_its_own_model_override():
    baseline = CATALOG.get("anthropic/claude-haiku-4-5")
    assert baseline is not None
    actual = _model()
    workspace = {
        "baseline_model": baseline.id,
        "grid": {"default": {
            "g_per_kwh": 123, "basis": "location_based", "label": "workspace grid",
        }},
        "pue": {"cloud": 1.6, "label": "workspace PUE"},
        "energy_strategy": "active_params",
        "model_overrides": {
            actual.id: {"energy_wh_per_mtok": 10, "label": "actual measured constant"},
            baseline.id: {"energy_wh_per_mtok": 20, "label": "baseline measured constant"},
        },
    }
    factors = build_factor_set(
        provider=actual.provider, settings=Settings(), workspace_settings=workspace,
        model_id=actual.id,
    )
    report = energy_accounting(
        actual, 0, 1_000_000, settings=Settings(), catalog=CATALOG, factors=factors
    )
    counterfactual = report["baseline"]
    assert counterfactual["grid_co2e_g_per_kwh"] == 123
    assert counterfactual["energy_wh"] == 20
    assert counterfactual["energy_wh_total"] == 32
    energy_factor = next(f for f in counterfactual["factors"] if f["key"] == "energy_class")
    assert energy_factor["setting"].endswith(baseline.id)


def test_cross_provider_baseline_resolves_region_and_hourly_grid_at_same_time():
    baseline = CATALOG.get("openrouter/deepseek/deepseek-v4-pro")
    assert baseline is not None
    at = datetime(2025, 6, 1, 5, 30, tzinfo=timezone.utc)
    csv = "hour_utc,g_per_kwh\n" + "\n".join(f"{h},{200 + h}" for h in range(24))
    workspace = {
        "baseline_model": baseline.id,
        "grid": {
            "regions": {"anthropic": "us-east", "openrouter": "eu-west"},
            "providers": {
                "anthropic@us-east": {
                    "g_per_kwh": 50, "basis": "location_based", "label": "actual",
                },
                "openrouter@eu-west": {
                    "g_per_kwh": 80, "basis": "location_based", "label": "baseline",
                    "table": "hourly",
                },
            },
            "tables": {"hourly": {
                "label": "hourly baseline", "basis": "location_based", "csv": csv,
            }},
        },
    }
    factors = build_factor_set(
        provider="anthropic", settings=Settings(), workspace_settings=workspace,
        model_id="anthropic/test-boundary", at=at,
    )
    report = energy_accounting(
        _model(), 1000, 100, settings=Settings(), catalog=CATALOG, factors=factors
    )
    assert report["grid_co2e_g_per_kwh"] == 50
    assert report["baseline"]["grid_co2e_g_per_kwh"] == 205
    baseline_grid = next(
        f for f in report["baseline"]["factors"] if f["key"] == "grid_intensity"
    )
    assert baseline_grid["setting"].endswith("openrouter@eu-west")
    assert baseline_grid["value"] == 205


def test_direct_grid_override_wins_on_both_actual_and_baseline():
    baseline = CATALOG.get("openrouter/deepseek/deepseek-v4-pro")
    assert baseline is not None
    settings = Settings(emissions_baseline_model=baseline.id)
    factors = build_factor_set(
        provider="anthropic", settings=settings, model_id=_model().id,
        run_overrides={"grid_g_per_kwh": 123},
    )
    report = energy_accounting(
        _model(), 1000, 100, settings=settings, catalog=CATALOG,
        factors=factors, grid_g_per_kwh=777,
    )
    assert report["grid_co2e_g_per_kwh"] == 777
    assert report["baseline"]["grid_co2e_g_per_kwh"] == 777


# ── F3: component lists describe the result, not the coefficient boundary ────
def test_default_v2_cloud_run_includes_facility_overhead_via_pue():
    report = energy_accounting(_model(), 1000, 1000)
    assert report["method_id"] == "class_ladder_v2"
    assert report["pue_applied"] is True
    assert report["component_lists_describe"] == "result"
    assert report["energy_boundary_of_coefficients"] == report["energy_boundary"] == "node_it"
    assert "facility_overhead_via_pue" in report["included_components"]
    assert "facility_overhead" not in report["excluded_components"]
    assert "facility_overhead_via_pue" not in report["excluded_components"]


def test_facility_measured_run_lists_overhead_as_measured_not_excluded():
    report = energy_accounting(
        _model(), 1000, 100, settings=Settings(datacenter_pue=1.8),
        measured_energy_wh=12.5, measured_energy_boundary="facility",
    )
    assert report["pue_applied"] is False
    assert "facility_overhead_measured" in report["included_components"]
    assert "facility_overhead_via_pue" not in report["included_components"]
    assert "facility_overhead" not in report["excluded_components"]


def test_gpu_measured_run_lists_overhead_via_pue_and_stays_incomplete():
    report = energy_accounting(
        _model(), 1, 1, measured_energy_wh=1, measured_energy_boundary="gpu"
    )
    assert report["pue_applied"] is True
    assert "facility_overhead_via_pue" in report["included_components"]
    assert "facility_overhead" not in report["excluded_components"]
    assert report["energy_boundary_complete"] is False


# ── F8: the v1 rollback names its own known defect ────────────────────────────
def test_v1_rollback_carries_the_source_pue_caveat_and_v2_lists_it_as_not_applying():
    v1 = energy_accounting(
        _model(), 1000, 1000,
        factors=build_factor_set(
            provider="anthropic", run_overrides={"energy_strategy": "class_ladder_v1"}
        ),
    )
    v1_caveat = next(c for c in v1["caveats"] if c["key"] == "legacy_source_pue_double_count")
    assert v1_caveat["applies"] is True
    assert v1_caveat["direction"] == "overstates"
    assert "Jegham 2025 Eq. 1" in v1_caveat["note"]

    v2 = energy_accounting(_model(), 1000, 1000)
    v2_caveat = next(c for c in v2["caveats"] if c["key"] == "legacy_source_pue_double_count")
    assert v2_caveat["applies"] is False


# ── A8: the embodied allocator degrades, never raises ─────────────────────────
def test_incomplete_embodied_allocation_does_not_raise_and_reports_unknown():
    allocation = {
        "method_id": "time_resource_share_v1", "complete_total_g": None,
        "covered_subtotal_g": 0.4,
        "components": [
            {"component_id": "gpu", "allocated_g": 0.4, "status": "supplied"},
            {"component_id": "chassis", "allocated_g": None, "status": "unknown"},
        ],
    }
    report = energy_accounting(_model(), 10, 10, embodied_allocation=allocation)
    assert report["embodied_g"] is None
    assert report["embodied_allocation"] == allocation
    hardware = next(
        c for c in report["coverage"]["components"] if c["component_id"] == "serving_hardware"
    )
    assert hardware["status"] == "unknown"
    factor = next(f for f in report["factors"] if f["key"] == "embodied_hardware")
    assert factor["embodied_allocation_covered_subtotal_g"] == 0.4
    assert factor["embodied_allocation_unknown_components"] == ["chassis"]

    # Unknown embodied contributed 0 to co2e_g (arithmetic only): operational
    # carbon is the whole co2e_g figure, not withheld just because embodied
    # is unknown, and embodied_hardware belongs to the coverage record as
    # unknown rather than being declared excluded.
    electricity = next(
        c for c in report["coverage"]["components"] if c["component_id"] == "inference_electricity"
    )
    assert electricity["value"] == pytest.approx(report["co2e_g"])
    assert "embodied_hardware" not in report["excluded_components"]


def test_embodied_double_add_is_prevented_not_raised():
    allocation = {
        "method_id": "time_resource_share_v1", "complete_total_g": 1.25,
        "covered_subtotal_g": 1.25, "components": [],
    }
    report = energy_accounting(
        _model(), 10, 10, embodied_allocation=allocation,
        supplier_includes_inference_hardware=True,
    )
    assert report["embodied_g"] == 0.0
    assert "embodied_allocation" not in report
    caveat = next(c for c in report["caveats"] if c["key"] == "embodied_double_add_prevented")
    assert caveat["applies"] is True


# ── F4/F6: unknown T&D losses equal unknown under an identical default ────────
def test_grid_signature_treats_unknown_td_losses_as_equal_for_identical_defaults():
    a = energy_accounting(_model(), 1000, 100)
    b = energy_accounting(_model(id="anthropic/test-boundary-2"), 1000, 100)
    assert a["grid_includes_td_losses"] is None
    assert b["grid_includes_td_losses"] is None
    combined = combine_accountings([a, b])
    assert combined["co2e_g"] == pytest.approx(a["co2e_g"] + b["co2e_g"])

    legacy = energy_accounting(
        _model(id="anthropic/test-boundary-3"), 1000, 100,
        settings=Settings(grid_co2e_g_per_kwh=470),
    )
    default = energy_accounting(_model(id="anthropic/test-boundary-4"), 1000, 100)
    mixed = combine_accountings([legacy, default])
    assert mixed["co2e_g"] is None


def test_grid_signature_differs_by_dataset_version():
    from tret.services.emissions import grid_comparison_signature

    base = {
        "grid_co2e_g_per_kwh": 400, "grid_co2e_basis": "location_based",
        "grid_co2e_source": "ember", "grid_co2e_label": "Ember 2025",
        "grid_co2e_layer": "country", "grid_factor_boundary": "generation",
        "grid_gas_coverage": "co2e", "grid_gwp_horizon_years": 100,
        "grid_gwp_assessment_basis": "AR6", "grid_includes_td_losses": True,
        "grid_electricity_mix_basis": "annual_average",
        "grid_dataset_version": "ember-yearly-2025-release",
        "grid_observation_year": 2024,
    }
    other = dict(base, grid_dataset_version="ember-yearly-2026-release")
    assert grid_comparison_signature(base) != grid_comparison_signature(other)


def test_grid_signature_treats_legacy_provenance_as_equal_to_later_provenance():
    """Defect 1: a 2026-08-28 run recorded no `grid_co2e_layer`/`grid_temporal`
    keys at all; a 2026-09-11 run recorded `grid_co2e_layer: "global_default"`.
    Both were priced at 470 gCO2e/kWh, location_based, source global_default —
    provenance (source/label/layer), not identity, so they must compare equal."""
    from tret.services.emissions import grid_comparison_signature

    legacy = {
        "grid_co2e_g_per_kwh": 470.0, "grid_co2e_basis": "location_based",
        "grid_co2e_source": "global_default",
    }
    later = {
        "grid_co2e_g_per_kwh": 470.0, "grid_co2e_basis": "location_based",
        "grid_co2e_source": "global_default", "grid_co2e_label": "Global default",
        "grid_co2e_layer": "global_default", "grid_temporal": "annual_average",
    }
    assert grid_comparison_signature(legacy) == grid_comparison_signature(later)


def test_combine_accountings_included_wins_over_excluded_contradiction():
    a = energy_accounting(_model(), 1000, 100)
    b = energy_accounting(_model(id="anthropic/test-boundary-5"), 1000, 100)
    # Manufacture the contradiction a real pair of segments could disagree on:
    # one segment includes embodied_hardware, the other (independently)
    # excludes it.
    a["included_components"] = list(a["included_components"]) + ["embodied_hardware"]
    a["excluded_components"] = [c for c in a["excluded_components"] if c != "embodied_hardware"]
    b["excluded_components"] = list(b["excluded_components"]) + ["embodied_hardware"]
    combined = combine_accountings([a, b])
    assert "embodied_hardware" in combined["included_components"]
    assert "embodied_hardware" not in combined["excluded_components"]
