import json
from decimal import Decimal
from pathlib import Path

import pytest

from tret.providers.catalog import ModelInfo
from tret.services.calibration_fit import fit_manifest
from tret.services.emission_factors import build_factor_set
from tret.services.emissions import (
    ENERGY_CLASS_WH_PER_MTOK_V1,
    ENERGY_CLASS_WH_PER_MTOK_V2,
    energy_accounting,
)


MANIFEST = json.loads(
    (Path(__file__).parents[1] / "tret/data/calibration/jegham_2025_v1.json").read_text()
)
V2_MANIFEST = json.loads(
    (Path(__file__).parents[1] / "tret/data/calibration/class_ladder_v2.json").read_text()
)


def _model(energy_class="M", **over):
    values = dict(
        id="anthropic/v2-test", provider="anthropic", wire_id="test", display_name="Test",
        context_window=1000, input_price_per_mtok=Decimal("1"),
        output_price_per_mtok=Decimal("1"), cost_tier="standard", energy_class=energy_class,
    )
    values.update(over)
    return ModelInfo(**values)


def test_v2_coefficients_regenerate_from_exact_source_pue_normalized_observations():
    diagnostics = fit_manifest(MANIFEST)
    anchors = {"S": "GPT-4.1 nano", "M": "GPT-4o", "L": "Claude 3.7 Sonnet", "R": "o3"}
    for energy_class, model in anchors.items():
        fitted = diagnostics[model]["fixed_weight"]["wh_per_mtok"]
        assert float(ENERGY_CLASS_WH_PER_MTOK_V2[energy_class]) == pytest.approx(fitted, rel=3e-7)
        assert fitted >= 0
        assert diagnostics[model]["nnls_free_input_output"]["input_wh_per_mtok"] >= 0
        assert diagnostics[model]["nnls_free_input_output"]["output_wh_per_mtok"] >= 0
        assert V2_MANIFEST["selected_wh_per_mtok"][energy_class] == float(
            ENERGY_CLASS_WH_PER_MTOK_V2[energy_class]
        )


def test_xl_is_exactly_the_documented_geometric_continuation():
    expected = (
        ENERGY_CLASS_WH_PER_MTOK_V2["L"] ** 2 / ENERGY_CLASS_WH_PER_MTOK_V2["M"]
    )
    assert float(ENERGY_CLASS_WH_PER_MTOK_V2["XL"]) == pytest.approx(float(expected), rel=1e-8)


def test_default_is_node_it_v2_and_v1_is_named_rollback_with_shadow():
    model = _model()
    default = energy_accounting(model, 0, 1_000_000)
    assert default["method_id"] == "class_ladder_v2"
    assert default["calibration_id"] == "jegham_2025_v2_node_it_fixed_weight"
    assert default["energy_boundary"] == "node_it"
    assert default["energy_wh"] == pytest.approx(float(ENERGY_CLASS_WH_PER_MTOK_V2["M"]))
    assert default["energy_method_shadow"]["method_id"] == "class_ladder_v1"
    assert default["energy_method_shadow"]["difference_kind"].startswith("methodology_correction")

    rollback = energy_accounting(
        model, 0, 1_000_000,
        factors=build_factor_set(
            provider=model.provider, model_id=model.id,
            run_overrides={"energy_strategy": "class_ladder_v1"},
        ),
    )
    assert rollback["method_id"] == "class_ladder_v1"
    assert rollback["energy_boundary"] == "unknown"
    assert rollback["energy_wh"] == float(ENERGY_CLASS_WH_PER_MTOK_V1["M"])


def test_explicit_model_override_still_wins_and_keeps_unknown_boundary():
    model = _model()
    factors = build_factor_set(
        provider=model.provider, model_id=model.id,
        workspace_settings={"model_overrides": {
            model.id: {"energy_wh_per_mtok": 333, "label": "operator measurement"}
        }},
    )
    report = energy_accounting(model, 0, 1_000_000, factors=factors)
    assert report["energy_wh"] == 333
    assert report["method_id"] == "model_override_v1"
    assert report["energy_boundary"] == "unknown"


def test_reasoning_denominator_uncertainty_is_retained_in_source_manifest():
    assert MANIFEST["boundary"]["output_reasoning_denominator"] == "unverified"
    diagnostics = fit_manifest(MANIFEST)
    assert diagnostics["DeepSeek-R1"]["fixed_weight"]["rmse_wh"] > 5


def test_xl_factor_record_is_flagged_interpolated_and_named_so():
    report = energy_accounting(_model(energy_class="XL"), 0, 1_000_000)
    factor = next(f for f in report["factors"] if f["key"] == "energy_class")
    assert factor["interpolated"] is True
    assert "no measured anchor" in factor["note"]
    assert "geometric continuation" in factor["note"]
    assert "weakest constant in the ladder" in factor["note"]

    anchored = energy_accounting(_model(energy_class="L"), 0, 1_000_000)
    anchored_factor = next(f for f in anchored["factors"] if f["key"] == "energy_class")
    assert anchored_factor["interpolated"] is False
