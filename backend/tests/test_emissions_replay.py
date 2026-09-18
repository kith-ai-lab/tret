from copy import deepcopy

from tret.services.emissions_replay import replay


def unit(**overrides):
    value = {
        "id": "run-1",
        "model": "openai/example",
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 10,
        "cache_write_tokens": 5,
        "energy_accounting": {
            "method_id": "class_ladder_v1",
            "energy_class": "M",
            "energy_wh": 0.1,
        },
    }
    value.update(overrides)
    return value


def test_replay_pairs_methods_without_mutating_input():
    rows = [unit()]
    original = deepcopy(rows)
    report = replay(rows)
    assert rows == original
    assert report["denominators"] == {
        "input_runs": 1, "eligible_runs": 1, "excluded_runs": 0, "paired_units": 1,
    }
    pair = report["paired_records"][0]
    assert pair["reconstructed"]["class_ladder_v1_wh"] > pair["reconstructed"]["class_ladder_v2_wh"]
    assert pair["observed_stored"] == {"method_id": "class_ladder_v1", "energy_wh": 0.1}
    assert pair["reasoning_denominator_complete"] is False
    assert report["accuracy_claim"] is False
    assert report["prospective_shadow"] is False
    assert report["method_boundaries"] == {
        "class_ladder_v1": {
            "coefficient_boundary": "unresolved_facility_source_calibration",
            "note": (
                "The published rollback constants retain the facility-source calibration "
                "boundary; they are not normalized to target-deployment IT load."
            ),
        },
        "class_ladder_v2": {
            "coefficient_boundary": "source_pue_normalized_node_it",
            "note": (
                "The v2 coefficients divide source observations by source-provider PUE "
                "before fitting and represent modeled node-IT energy."
            ),
        },
        "replay_factor_application": {
            "target_deployment_pue_applied": False,
            "stored_ledger_boundary": "unknown",
            "comparison_boundary": "coefficient_methodologies_as_published",
        },
    }
    assert any("not a matched-boundary facility-energy comparison" in item
               for item in report["limitations"])


def test_missing_bucket_override_and_measurement_are_excluded_not_zero_filled():
    missing = unit(id="missing")
    del missing["cache_write_tokens"]
    override = unit(
        id="override",
        energy_accounting={"method_id": "model_override_v1", "energy_class": "M"},
    )
    measured = unit(
        id="measured",
        energy_accounting={"method_id": "measured_energy_v1", "energy_class": "M"},
    )
    report = replay([missing, override, measured])
    assert report["denominators"]["eligible_runs"] == 0
    assert report["exclusions"] == {
        "missing_or_invalid_token_bucket": 1,
        "non_ladder_or_measured_method": 2,
    }


def test_legacy_factor_strategy_is_safe_classification_but_unknown_class_is_not():
    legacy = unit(
        energy_accounting={
            "energy_class": "S",
            "factors": [{"key": "energy_class", "strategy": "class_ladder_v1"}],
        }
    )
    unknown = unit(id="unknown", energy_accounting={"method_id": "class_ladder_v1"})
    report = replay([legacy, unknown])
    assert report["denominators"]["eligible_runs"] == 1
    assert report["exclusions"]["missing_or_unknown_energy_class"] == 1


def test_measured_or_explicit_override_cannot_fall_back_to_ladder_factor():
    factor = [{"key": "energy_class", "strategy": "class_ladder_v1"}]
    measured = unit(
        id="measured",
        energy_accounting={"energy_source": "measured", "energy_class": "S", "factors": factor},
    )
    override = unit(
        id="override",
        energy_accounting={
            "method_id": "model_override_v1", "energy_class": "S", "factors": factor,
        },
    )
    report = replay([measured, override])
    assert report["denominators"]["eligible_runs"] == 0
    assert report["exclusions"] == {"non_ladder_or_measured_method": 2}


def test_known_additional_reasoning_is_counted_once():
    row = unit(
        call_records=[{
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 10,
            "cache_write_tokens": 5,
            "reasoning_tokens": 7,
            "reasoning_accounting": "additional",
            "usage_status": "reported",
        }]
    )
    pair = replay([row])["paired_records"][0]
    assert pair["energy_output_tokens"] == 27
    assert pair["output_denominator_source"] == "reconciled_call_reasoning"
    assert pair["reasoning_denominator_complete"] is True


def test_stored_denominator_does_not_imply_complete_reasoning_metadata():
    row = unit(energy_accounting={
        "method_id": "class_ladder_v2", "energy_class": "M", "energy_output_tokens": 20,
    })
    pair = replay([row])["paired_records"][0]
    assert pair["energy_output_tokens"] == 20
    assert pair["reasoning_denominator_complete"] is False
    assert pair["output_denominator_source"] == "recorded_energy_output_tokens_unknown_reasoning"


def test_stored_denominator_must_reconcile_with_call_reasoning():
    call = {
        "input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 10,
        "cache_write_tokens": 5, "reasoning_tokens": 7,
        "reasoning_accounting": "additional", "usage_status": "reported",
    }
    good = unit(call_records=[call], energy_accounting={
        "method_id": "class_ladder_v2", "energy_class": "M", "energy_output_tokens": 27,
    })
    bad = unit(id="bad", call_records=[call], energy_accounting={
        "method_id": "class_ladder_v2", "energy_class": "M", "energy_output_tokens": 20,
    })
    report = replay([good, bad])
    assert report["denominators"]["eligible_runs"] == 1
    assert report["paired_records"][0]["reasoning_denominator_complete"] is True
    assert report["exclusions"] == {"unreconciled_or_invalid_call_usage": 1}


def test_unreconciled_multi_segment_run_is_excluded_atomically():
    first = unit()["energy_accounting"] | {"energy_class": "S"}
    segments = [
        {
            "model": "openai/a", "input_tokens": 10, "output_tokens": 2,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "energy_accounting": first,
        },
        {
            "model": "openai/b", "input_tokens": 10, "output_tokens": 2,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
            "call_records": [{
                "input_tokens": 9, "output_tokens": 2, "cache_read_tokens": 0,
                "cache_write_tokens": 0, "usage_status": "reported",
            }],
            "energy_accounting": first,
        },
    ]
    report = replay([unit(
        input_tokens=20, output_tokens=4, cache_read_tokens=0, cache_write_tokens=0,
        model_timeline=segments,
    )])
    assert report["denominators"]["eligible_runs"] == 0
    assert report["denominators"]["paired_units"] == 0
    assert report["exclusions"] == {"unreconciled_or_invalid_call_usage": 1}


def test_model_timeline_replaces_top_level_totals_instead_of_double_counting():
    accounting = unit()["energy_accounting"]
    segment = {
        "model": "openai/a", "input_tokens": 10, "output_tokens": 2,
        "cache_read_tokens": 0, "cache_write_tokens": 0,
        "energy_accounting": accounting,
    }
    report = replay([unit(
        input_tokens=10, output_tokens=2, cache_read_tokens=0, cache_write_tokens=0,
        model_timeline=[segment],
    )])
    assert report["denominators"]["paired_units"] == 1
    assert report["paired_records"][0]["tokens"]["input_tokens"] == 10


def test_timeline_requires_segment_accounting_and_parent_token_reconciliation():
    segment = {
        "model": "openai/a", "input_tokens": 50, "output_tokens": 20,
        "cache_read_tokens": 10, "cache_write_tokens": 5,
        "energy_accounting": unit()["energy_accounting"],
    }
    mismatch = replay([unit(model_timeline=[segment])])
    assert mismatch["exclusions"] == {"unreconciled_segment_token_allocation": 1}

    segment["input_tokens"] = 100
    missing = deepcopy(segment)
    del missing["energy_accounting"]
    result = replay([unit(model_timeline=[missing])])
    assert result["exclusions"] == {"missing_segment_energy_accounting": 1}

    complete = replay([unit(model_timeline=[segment])])
    assert complete["denominators"]["eligible_runs"] == 1


def test_historical_weights_resolve_across_top_fields_and_factor_records():
    accounting = {
        "method_id": "class_ladder_v1", "energy_class": "M",
        "cache_read_weight": 0.005, "cache_write_weight": 0.05,
        "factors": [
            {"key": "token_weight_input", "value": 0.05},
            {"key": "token_weight_output", "value": 1.0},
        ],
    }
    pair = replay([unit(energy_accounting=accounting)])["paired_records"][0]
    assert pair["weight_source"] == "recorded"
    assert pair["token_weights"] == {
        "input_weight": 0.05, "output_weight": 1.0,
        "cache_read_weight": 0.005, "cache_write_weight": 0.05,
    }

    conflicting = deepcopy(accounting)
    conflicting["input_weight"] = 0.1
    report = replay([unit(energy_accounting=conflicting)])
    assert report["exclusions"] == {"partial_or_invalid_recorded_weights": 1}


def test_invalid_numbers_and_estimated_segments_are_exclusions_not_batch_errors():
    invalid_weight = unit(id="weight", energy_accounting={
        "method_id": "class_ladder_v1", "energy_class": "M", "input_weight": "nan",
    })
    invalid_observed = unit(id="observed", energy_accounting={
        "method_id": "class_ladder_v1", "energy_class": "M", "energy_wh": "nan",
    })
    segment = {
        "model": "openai/a", "input_tokens": 100, "output_tokens": 20,
        "cache_read_tokens": 10, "cache_write_tokens": 5, "estimated_usage": True,
        "energy_accounting": unit()["energy_accounting"],
    }
    estimated = unit(id="estimated", model_timeline=[segment])
    report = replay([invalid_weight, invalid_observed, estimated, unit(id="valid")])
    assert report["denominators"]["eligible_runs"] == 1
    assert report["exclusions"] == {
        "estimated_segment_usage": 1,
        "invalid_observed_stored_energy": 1,
        "partial_or_invalid_recorded_weights": 1,
    }
