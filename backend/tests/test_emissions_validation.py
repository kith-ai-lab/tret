import pytest

from tret.services.emissions_validation import cache_experiment, evaluate, inventory


def test_database_run_projection_preserves_provider_and_duration():
    from datetime import datetime, timedelta, timezone
    from types import SimpleNamespace
    from tret.services.emissions_validation import usage_export_record
    now = datetime(2026, 9, 1, tzinfo=timezone.utc)
    run = SimpleNamespace(id="r", provider_used="anthropic", started_at=now,
                          finished_at=now + timedelta(seconds=7), created_at=now)
    exported = usage_export_record(run)
    assert exported["duration_s"] == 7
    assert exported["provider"] == "anthropic"
    report = inventory([exported], start="2026-09-01T00:00:00Z", end="2026-09-02T00:00:00Z")
    assert report["coverage"]["energy_source:unknown"] == 1
    assert report["coverage"]["energy_boundary:unknown"] == 1


def observation(**overrides):
    return {"id": "1", "split": "held_out", "workload_family": "long",
            "serving_configuration": "h100", "boundary": "node_it", "observed_wh": 10,
            "predicted_wh": 11, "baseline_wh": 13, "instrument_validation_id": "lab-1",
            "reference_meter_id": "pdu-1", "temporal_coverage": 1, "device_coverage": 1,
            **overrides}


def test_validation_held_out_metrics_and_missing_predictions():
    report = evaluate([observation(), observation(id="2", predicted_wh=None)])
    assert report["held_out"]["wape"] == .1
    assert report["held_out"]["prediction_failures"] == 1
    assert not report["numerical_targets_met"]
    assert report["accuracy_claim"] is None


def test_validation_refuses_family_or_configuration_leakage():
    with pytest.raises(ValueError, match="split leakage"):
        evaluate([observation(), observation(id="2", split="fit")])


@pytest.mark.parametrize("override", [{"observed_wh": "nan"}, {"boundary": "gpu"},
                                     {"device_coverage": .9}, {"reference_meter_id": None}])
def test_validation_refuses_invalid_measurements(override):
    with pytest.raises(ValueError):
        evaluate([observation(**override)])


def test_zero_denominator_is_undefined():
    report = evaluate([observation(observed_wh=0, predicted_wh=0)])
    assert report["held_out"]["wape"] is None
    assert not report["numerical_targets_met"]


def test_baseline_interval_coverage_uses_only_baseline_bounds():
    report = evaluate([observation(
        interval_low_wh=9, interval_high_wh=11,
        baseline_interval_low_wh=12, baseline_interval_high_wh=14,
    )])
    assert report["held_out"]["interval_coverage"] == 1
    assert report["baseline_held_out"]["interval_coverage"] == 0


def test_inventory_reconciles_without_counting_segment_twice_or_copying_content():
    rows = [{"id": "a", "created_at": "2026-09-01T00:00:00Z", "energy_wh": 2,
             "messages": "private prompt", "status": "failed", "input_tokens": 42,
             "model_timeline": [{"energy_wh": 2, "call_records": [{"served_by": "supplier"}]}]},
            {"id": "b", "created_at": "2026-09-02T00:00:00Z", "energy_wh": None}]
    report = inventory(rows, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z")
    assert report["reconciled"]
    assert report["energy_wh_subtotal"] == 2
    assert report["coverage"]["runs_missing_energy"] == 1
    assert report["coverage"]["calls_unknown_reasoning_tokens"] == 1
    assert "private prompt" not in str(report)
    with pytest.raises(ValueError, match="unique"):
        inventory(rows * 2, start="2026-09-01T00:00:00Z", end="2026-10-01T00:00:00Z")


def test_cache_experiment_reports_whole_request_ratio_not_new_token_coefficient():
    base = {"pair_id": "1", "model_revision": "v1", "configuration": "box",
            "prompt_hash": "abc", "boundary": "node_it", "instrument_validation_id": "meter",
            "temporal_coverage": 1, "device_coverage": 1}
    result = cache_experiment([base | {"cache": "on", "observed_wh": 4},
                               base | {"cache": "off", "observed_wh": 10}])
    assert result["whole_request_energy_ratio"] == .4
    assert result["prefill_weight"] is None
