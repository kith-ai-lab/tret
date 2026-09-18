"""Phase 3: region-pinned provider keys, hourly grid tables, hardware
profiles for embodied carbon, and an evidence-narrowed uncertainty band.

Each feature has its own pure module (`grid_regions.py`, `grid_tables.py`,
`embodied_profiles.py`, `uncertainty_derivation.py`) already covered by its
own docstring contract; this suite is the cross-cutting one — that
`emission_factors.py`/`emissions.py` actually wire each of them into
`build_factor_set`/`energy_accounting` the way `docs/emissions-methodology.md`
describes. See `tests/test_emission_factors.py` for the ladder itself and
`tests/evals/test_runner_factor_layers.py` / `tests/test_emissions_whatif.py`
for the two integration paths (a real run, the what-if recompute) this file
does not re-derive.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from tret.providers.catalog import ModelCatalog, ModelInfo
from tret.services.embodied_profiles import EmbodiedProfile, grams_per_run
from tret.services.emission_factors import EmissionsOverrides, build_factor_set
from tret.services.emissions import energy_accounting
from tret.services.uncertainty_derivation import Evidence

CATALOG = ModelCatalog()


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


def _local_model(**over) -> ModelInfo:
    return _model(
        "S", provider="local", id="local/x", cost_tier="local",
        input_price_per_mtok=Decimal("0"), output_price_per_mtok=Decimal("0"), **over
    )


def _account(model: ModelInfo, **kw) -> dict:
    tokens = kw.pop("tokens", (1_000_000, 0, 0, 0))
    return energy_accounting(model, *tokens, catalog=CATALOG, **kw)


def _scope_sum(report: dict) -> Decimal:
    s = report["scopes"]
    return Decimal(str(s["scope1_g"])) + Decimal(str(s["scope2_g"])) + Decimal(str(s["scope3_g"]))


# ── Feature A: region-pinned provider keys ───────────────────────────────────
def test_pinned_region_picks_the_regional_key_over_the_bare_one_in_the_same_layer():
    doc = {
        "grid": {
            "regions": {"anthropic": "us-east"},
            "providers": {
                "anthropic": {"g_per_kwh": 400, "basis": "location_based", "label": "bare"},
                "anthropic@us-east": {
                    "g_per_kwh": 90, "basis": "location_based", "label": "regional",
                },
            },
        }
    }
    fs = build_factor_set(provider="anthropic", workspace_settings=doc)
    assert fs.grid.value == Decimal("90")
    assert fs.grid.source == "workspace:provider:anthropic@us-east"
    assert fs.grid.region == "us-east"


def test_a_workspace_region_pin_applies_to_a_managed_layer_entry_lookup():
    workspace = {"grid": {"regions": {"anthropic": "us-east"}}}
    managed = {
        "grid": {
            "providers": {
                "anthropic@us-east": {
                    "g_per_kwh": 77, "basis": "location_based", "label": "managed regional",
                }
            }
        }
    }
    fs = build_factor_set(
        provider="anthropic", workspace_settings=workspace, managed_settings=managed
    )
    assert fs.grid.value == Decimal("77")
    assert fs.grid.layer == "managed"
    assert fs.grid.region == "us-east"


def test_a_malformed_provider_key_is_rejected_and_names_it():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            grid={
                "providers": {
                    "Anthropic@": {"g_per_kwh": 1, "basis": "location_based", "label": "x"}
                }
            }
        )
    assert "grid.providers.Anthropic@" in str(exc.value)


def test_a_malformed_region_pin_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(grid={"regions": {"anthropic": "US EAST"}})
    assert "grid.regions" in str(exc.value)


# ── Feature B: hourly grid tables ─────────────────────────────────────────────
def _diurnal_csv() -> str:
    rows = "\n".join(f"{h},{100 + h}" for h in range(24))
    return "hour_utc,g_per_kwh\n" + rows


def _diurnal_doc() -> dict:
    return {
        "grid": {
            "default": {
                "g_per_kwh": 50, "basis": "location_based", "label": "annual fallback",
                "table": "diurnal",
            },
            "tables": {
                "diurnal": {
                    "label": "test diurnal profile", "basis": "location_based",
                    "csv": _diurnal_csv(),
                }
            },
        }
    }


def test_a_diurnal_table_yields_the_hours_value_when_at_is_given():
    at = datetime(2025, 6, 1, 5, 30, tzinfo=timezone.utc)
    fs = build_factor_set(provider="anthropic", workspace_settings=_diurnal_doc(), at=at)
    assert fs.grid.value == Decimal("105")  # hour 5 -> 100 + 5
    assert fs.grid.temporal == "hourly"
    assert fs.grid.table == "diurnal"
    assert fs.grid.table_summary["row_count"] == 24
    assert fs.grid.table_miss is False


def test_without_at_a_table_reference_stays_annual_but_is_still_named():
    fs = build_factor_set(provider="anthropic", workspace_settings=_diurnal_doc())
    assert fs.grid.value == Decimal("50")
    assert fs.grid.temporal == "annual_average"
    assert fs.grid.table == "diurnal"


def test_a_series_table_gap_falls_back_to_the_entrys_value_and_flags_table_miss():
    csv_text = "timestamp_utc,g_per_kwh\n2025-01-01T00:00:00Z,200\n"
    doc = {
        "grid": {
            "default": {
                "g_per_kwh": 60, "basis": "location_based", "label": "annual", "table": "series1",
            },
            "tables": {
                "series1": {"label": "series", "basis": "location_based", "csv": csv_text},
            },
        }
    }
    at = datetime(2025, 6, 1, tzinfo=timezone.utc)  # far past the row's lookup gap
    fs = build_factor_set(provider="anthropic", workspace_settings=doc, at=at)
    assert fs.grid.value == Decimal("60")
    assert fs.grid.temporal == "annual_average"
    assert fs.grid.table_miss is True


def test_a_naive_at_raises():
    with pytest.raises(ValueError):
        build_factor_set(provider="anthropic", at=datetime(2025, 1, 1))


def test_a_malformed_table_csv_is_rejected_and_names_the_table():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            grid={
                "tables": {
                    "bad": {
                        "label": "x", "basis": "location_based",
                        "csv": "not,a,valid,header\n1,2,3,4",
                    }
                }
            }
        )
    assert "grid.tables.bad.csv" in str(exc.value)


def test_a_table_reference_to_an_unknown_table_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            grid={
                "default": {
                    "g_per_kwh": 1, "basis": "location_based", "label": "x", "table": "nope",
                }
            }
        )
    assert "grid.default.table references unknown table" in str(exc.value)


# ── B1: caps on grid.tables as a whole ────────────────────────────────────────
def _table_entry() -> dict:
    # A single-row series table — content is irrelevant to the two tests
    # below, since both caps are checked (and raise) before any table's CSV
    # is ever handed to the parser.
    return {
        "label": "t", "basis": "location_based",
        "csv": "timestamp_utc,g_per_kwh\n2025-01-01T00:00:00Z,100\n",
    }


def test_more_than_8_tables_is_rejected_naming_the_rule():
    doc = {"grid": {"tables": {f"t{i}": _table_entry() for i in range(9)}}}
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(**doc)
    assert "grid.tables: at most 8 tables per document" in str(exc.value)


def test_combined_csv_over_the_cap_is_rejected_naming_the_rule():
    # Each table's CSV is under the 600,000-char per-table cap, but the 8 of
    # them together are over the 2,000,000-char combined cap.
    per_table_csv = "x" * 250_001
    doc = {
        "grid": {
            "tables": {
                f"t{i}": {"label": "t", "basis": "location_based", "csv": per_table_csv}
                for i in range(8)
            }
        }
    }
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(**doc)
    assert "grid.tables: combined csv exceeds 2000000 characters" in str(exc.value)


# ── B2: a table reference's basis must match the referencing entry ───────────
def test_a_default_table_reference_with_a_mismatched_basis_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            grid={
                "default": {
                    "g_per_kwh": 50, "basis": "location_based", "label": "x", "table": "t",
                },
                "tables": {
                    "t": {"label": "table", "basis": "market_based", "csv": _diurnal_csv()},
                },
            }
        )
    assert (
        "grid.default.table 't' has basis market_based but the entry is location_based"
        in str(exc.value)
    )


def test_a_provider_table_reference_with_a_mismatched_basis_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            grid={
                "providers": {
                    "anthropic": {
                        "g_per_kwh": 50, "basis": "market_based", "label": "x", "table": "t",
                    },
                },
                "tables": {
                    "t": {"label": "table", "basis": "location_based", "csv": _diurnal_csv()},
                },
            }
        )
    assert (
        "grid.providers.anthropic.table 't' has basis location_based but the entry is "
        "market_based" in str(exc.value)
    )


def test_grid_temporal_and_region_are_visible_at_the_top_level():
    at = datetime(2025, 6, 1, 5, 30, tzinfo=timezone.utc)
    fs = build_factor_set(provider="anthropic", workspace_settings=_diurnal_doc(), at=at)
    report = _account(_model("L"), factors=fs)
    assert report["grid_temporal"] == "hourly"
    assert report["grid_region"] is None
    grid_factor = next(f for f in report["factors"] if f["key"] == "grid_intensity")
    assert grid_factor["temporal"] == "hourly"
    assert grid_factor["table"] == "diurnal"
    assert grid_factor["table_summary"]["row_count"] == 24


# ── Feature C: hardware profiles for embodied carbon ──────────────────────────
def test_profile_embodied_value_matches_grams_per_run():
    profile_dict = {"gpus": 4, "runs_over_lifetime": 100_000, "label": "our box"}
    fs = build_factor_set(
        provider="local", workspace_settings={"embodied": {"profile": profile_dict}}
    )
    expected = grams_per_run(EmbodiedProfile(gpus=4, runs_over_lifetime=100_000, label="our box"))
    assert fs.embodied_g.value == expected


def test_embodied_g_per_run_and_profile_together_is_rejected():
    with pytest.raises(ValidationError) as exc:
        EmissionsOverrides(
            embodied={
                "g_per_run": 1.0,
                "profile": {"gpus": 1, "runs_over_lifetime": 1000},
                "label": "x",
            }
        )
    assert "embodied: set g_per_run or profile, not both" in str(exc.value)


def test_the_embodied_factor_record_carries_the_profile_summary():
    doc = {"embodied": {"profile": {"gpus": 2, "runs_over_lifetime": 50_000, "label": "site box"}}}
    fs = build_factor_set(provider="local", workspace_settings=doc)
    report = _account(_local_model(), factors=fs)
    record = next(f for f in report["factors"] if f["key"] == "embodied_hardware")
    assert record["profile"]["gpus"] == 2
    assert record["confidence"] == "placeholder"


def test_an_explicit_profile_applies_to_cloud_deployment():
    doc = {"embodied": {"profile": {"gpus": 2, "runs_over_lifetime": 50_000, "label": "site box"}}}
    fs = build_factor_set(provider="anthropic", workspace_settings=doc)
    assert fs.embodied_g.value > Decimal(0)
    assert fs.embodied_g.layer == "workspace"


# ── Feature D: evidence-narrowed band ─────────────────────────────────────────
def _evidenced_doc() -> dict:
    return {
        "band": {"derived": True},
        "pue": {"cloud": 1.15, "label": "site metered PUE"},
        "grid": {
            "default": {
                "g_per_kwh": 400, "basis": "location_based", "label": "sourced dated grid",
                "as_of": "2025-01-01",
            }
        },
    }


def test_a_fully_evidenced_run_narrows_the_band_to_the_dominant_contribution():
    fs = build_factor_set(provider="anthropic", workspace_settings=_evidenced_doc())
    report = _account(
        _model("L"), factors=fs, measured_energy_wh=5.0,
        validated_evidence=Evidence(
            energy_measured=True, pue_metered=True, grid_sourced_dated=True
        ),
    )
    uncertainty = report["uncertainty"]
    assert uncertainty["band_factor_low"] == pytest.approx(1.4286, abs=2e-3)
    assert uncertainty["band_factor_high"] == 1.3
    assert uncertainty["derivation"]["rule"] == "dominant_contribution"
    assert uncertainty["is_confidence_interval"] is False
    assert uncertainty["kind"] == "judgment_band"
    # Every touched contribution row carries its own evidence stamp.
    grid_row = next(c for c in uncertainty["contributions"] if c["key"] == "grid_intensity")
    assert grid_row["evidence"] == "grid_sourced_dated"


def test_without_derived_the_band_is_exactly_todays_behaviour():
    doc = dict(_evidenced_doc())
    doc = {k: v for k, v in doc.items() if k != "band"}
    fs = build_factor_set(provider="anthropic", workspace_settings=doc)
    report = _account(_model("L"), factors=fs, measured_energy_wh=5.0)
    uncertainty = report["uncertainty"]
    assert uncertainty["band_factor_low"] == 2.5
    assert uncertainty["band_factor_high"] == 2.5
    assert "derivation" not in uncertainty


def test_co2e_equals_scopes_and_the_report_is_json_serializable_when_derived():
    fs = build_factor_set(provider="anthropic", workspace_settings=_evidenced_doc())
    report = _account(_model("L"), factors=fs, measured_energy_wh=5.0)
    assert Decimal(str(report["co2e_g"])) == pytest.approx(_scope_sum(report))
    json.dumps(report)  # raises on anything non-JSON-safe (a stray Decimal, etc.)


# ── M1: derived must not narrow without evidence ──────────────────────────────
def _configured_band_doc(**over) -> dict:
    doc = {"band": {"derived": True, "low": 4.0, "high": 4.0, "label": "deliberately conservative"}}
    doc.update(over)
    return doc


def test_derived_with_no_evidence_at_configured_4_0_stays_4_0_4_0():
    fs = build_factor_set(provider="anthropic", workspace_settings=_configured_band_doc())
    report = _account(_model("L"), factors=fs)
    uncertainty = report["uncertainty"]
    assert uncertainty["band_factor_low"] == 4.0
    assert uncertainty["band_factor_high"] == 4.0
    assert uncertainty["derivation"]["rule"] == "configured"
    assert uncertainty["derivation"]["narrowed"] is False


def test_measured_energy_alone_keeps_the_high_side_at_configured():
    # measured_energy_wh alone narrows only the energy-related contribution
    # rows (energy_class, batching, measurement_bias) — none of which
    # dominate here — and PUE/grid were never sourced from an operator layer,
    # so no evidence flag touches grid_intensity's own, always-present
    # high_multiplier. It must not set the axis on its own.
    fs = build_factor_set(provider="anthropic", workspace_settings=_configured_band_doc())
    report = _account(_model("L"), factors=fs, measured_energy_wh=5.0)
    uncertainty = report["uncertainty"]
    assert uncertainty["band_factor_high"] == 4.0
    assert uncertainty["derivation"]["rule"] == "configured"
