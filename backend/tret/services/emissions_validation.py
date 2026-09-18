"""Offline workload inventory and preregistered energy-model evaluation.

Input is an authorised JSON/JSONL export, never a live production connection.
Reports contain identifiers and aggregates only; prompts and responses are not
copied. Passing synthetic tests does not establish measured accuracy.

Run ``python -m tret.services.emissions_validation --help`` for the CLI.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
from statistics import median
from typing import Any, Iterable


def number(value: Any, name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"{name} must be a finite nonnegative number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} must be a finite nonnegative number")
    return result


def _unique(rows: list[dict], key: str) -> None:
    seen: set[str] = set()
    for row in rows:
        value = row.get(key)
        if not isinstance(value, str) or not value or value in seen:
            raise ValueError(f"{key} must be present and unique: {value!r}")
        seen.add(value)


def inventory(rows: Iterable[dict], *, start: str, end: str) -> dict:
    """Reconcile every exported run; caller declares the half-open UTC window.

    Missing segment detail is retained as a coverage gap. Segment energy is
    never added to its parent run. Counts are runs (not inferred API requests).
    """
    rows = list(rows)
    _unique(rows, "id")
    begin, finish = (datetime.fromisoformat(v.replace("Z", "+00:00")) for v in (start, end))
    if begin.tzinfo is None or finish.tzinfo is None or finish <= begin:
        raise ValueError("window must be increasing timezone-aware timestamps")
    groups: dict[tuple, dict] = {}
    coverage = Counter()
    total_wh = Decimal(0)
    token_totals = Counter()
    token_keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    for row in rows:
        created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
        if created.tzinfo is None or not begin <= created < finish:
            raise ValueError(f"run {row['id']} is outside the declared window")
        accounting = row.get("energy_accounting") or {}
        key = tuple(row.get(k) or "unknown" for k in (
            "model", "model_revision", "provider", "deployment", "harness_id",
        ))
        group = groups.setdefault(key, {"runs": 0, "energy_wh_subtotal": Decimal(0),
                                       "runs_missing_energy": 0, "statuses": Counter()})
        group["runs"] += 1
        group["statuses"][row.get("status") or "unknown"] += 1
        wh = row.get("energy_wh")
        if wh is None:
            group["runs_missing_energy"] += 1
            coverage["runs_missing_energy"] += 1
        else:
            wh = number(wh, "energy_wh")
            group["energy_wh_subtotal"] += wh
            total_wh += wh
        coverage[f"energy_source:{accounting.get('energy_source') or 'unknown'}"] += 1
        coverage[f"energy_boundary:{accounting.get('energy_boundary') or 'unknown'}"] += 1
        for token in token_keys:
            value = row.get(token)
            if value is None:
                coverage[f"runs_missing_{token}"] += 1
            else:
                parsed = number(value, token)
                if parsed != parsed.to_integral_value():
                    raise ValueError(f"{token} must be an integer")
                token_totals[token] += int(parsed)
        calls = [c for s in (row.get("model_timeline") or []) for c in s.get("call_records", [])]
        if not calls:
            coverage["runs_without_call_detail"] += 1
        for call in calls:
            coverage["reported_calls"] += 1
            for field in ("reasoning_tokens", "served_by", "inference_geo"):
                if call.get(field) is None:
                    coverage[f"calls_unknown_{field}"] += 1
            coverage[f"upstream:{call.get('served_by') or 'unknown'}"] += 1
            coverage[f"geography:{call.get('inference_geo') or 'unknown'}"] += 1
            coverage[f"usage_status:{call.get('usage_status') or 'unknown'}"] += 1
        if row.get("retry_count") is None:
            coverage["runs_unknown_retry_count"] += 1
        if row.get("duration_s") is None:
            coverage["runs_unknown_duration"] += 1
        if row.get("tool_activity") is None:
            coverage["runs_unknown_tool_activity"] += 1
    output = []
    for key, group in groups.items():
        output.append(dict(zip(("model", "model_revision", "provider", "deployment", "harness_id"), key))
                      | group | {"energy_wh_subtotal": float(group["energy_wh_subtotal"]),
                                 "statuses": dict(group["statuses"])})
    return {
        "schema_version": "workload_inventory_v1", "window": {"start": start, "end": end},
        "denominator": "exported_runs", "runs": len(rows),
        "energy_wh_subtotal": float(total_wh), "tokens_reported_subtotal": dict(token_totals),
        "coverage": dict(coverage), "groups": output,
        "rank_by_runs": sorted(range(len(output)), key=lambda i: -output[i]["runs"]),
        "rank_by_energy": sorted(range(len(output)), key=lambda i: -output[i]["energy_wh_subtotal"]),
        "reconciled": sum(g["runs"] for g in output) == len(rows),
    }


def usage_export_record(run: Any) -> dict:
    """Allowlisted export projection for a Run object; no messages/task input.

    Request counters remain unknown if per-call records are absent. A model
    alias is not a revision. Caller controls database access and window.
    """
    accounting = getattr(run, "energy_accounting", None) or {}
    call_fields = ("iteration", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                   "reasoning_tokens", "reasoning_accounting", "served_by", "inference_geo", "usage_status",
                   "started_at", "ended_at")
    segments = []
    for segment in getattr(run, "model_timeline", None) or []:
        segments.append({"model": segment.get("model"), "provider": segment.get("provider"),
                         "call_records": [{k: call.get(k) for k in call_fields}
                                          for call in segment.get("call_records", [])]})
    created = getattr(run, "created_at", None)
    start, end = getattr(run, "started_at", None), getattr(run, "finished_at", None)
    return {"id": str(run.id), "created_at": created.isoformat() if created else None,
            "model": getattr(run, "model_used", None), "model_revision": accounting.get("model_revision"),
            "provider": getattr(run, "provider_used", None), "deployment": accounting.get("deployment"),
            "harness_id": str(run.harness_id) if getattr(run, "harness_id", None) else None,
            "status": getattr(run, "status", None), "model_timeline": segments,
            "energy_wh": float(run.energy_wh) if getattr(run, "energy_wh", None) is not None else None,
            "energy_accounting": {k: accounting.get(k) for k in ("energy_source", "energy_boundary", "method_id")},
            "duration_s": (end - start).total_seconds() if start and end else None,
            "retry_count": None, "tool_activity": None,
            **{k: getattr(run, k, None) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")}}


def shadow_report(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    _unique(rows, "id")
    groups = defaultdict(lambda: {"runs": 0, "current_wh": Decimal(0), "legacy_wh": Decimal(0)})
    missing = 0
    for row in rows:
        account = row.get("energy_accounting") or {}
        shadow = account.get("energy_method_shadow") or {}
        if account.get("energy_wh") is None or shadow.get("energy_wh") is None:
            missing += 1
            continue
        if account.get("method_id") != "class_ladder_v2" or shadow.get("method_id") != "class_ladder_v1":
            raise ValueError("shadow report requires the v2/v1 paired methods")
        key = str(account.get("model") or "unknown")
        group = groups[key]
        group["runs"] += 1
        group["current_wh"] += number(account["energy_wh"], "current_wh")
        group["legacy_wh"] += number(shadow["energy_wh"], "legacy_wh")
    reversals = []
    names = sorted(groups)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if (groups[a]["current_wh"] - groups[b]["current_wh"]) * (groups[a]["legacy_wh"] - groups[b]["legacy_wh"]) < 0:
                reversals.append([a, b])
    return {"schema_version": "method_shadow_report_v1", "paired_runs": len(rows) - missing,
            "runs_missing_pair": missing, "difference_kind": "methodology_correction_not_emissions_savings",
            "model_cohort_total_rank_reversals": reversals,
            "groups": {name: {k: float(v) if isinstance(v, Decimal) else v for k, v in group.items()}
                       for name, group in groups.items()},
            "note": "Ranks compare the same recorded cohort under two methods, not equivalent model tasks.",
            "promotion": "requires representative window, measured validation and independent review"}


def benchmark_protocol() -> dict:
    return {
        "protocol_id": "tret_energy_validation_v1", "status": "preregistered_unexecuted",
        "functional_unit": "one uniquely identified inference activity",
        "boundary": "node_it", "splits": ["fit", "validation", "held_out"],
        "split_rule": "Keep each workload_family and serving_configuration in one split only.",
        "dimensions": {
            "input": ["short", "typical", "long_context", "upper_tail"],
            "output": ["short", "normal", "long"], "reasoning": ["off", "low", "high"],
            "cache": ["cold", "warm", "mixed"], "concurrency": [1, 2, 4, 8],
            "phase": ["warmup", "steady_state", "idle", "model_switch", "failure"],
        },
        "required_metadata": ["model_revision", "hardware", "gpu_count", "precision",
                              "engine_version", "allocation_method", "reference_meter_id",
                              "instrument_validation_id", "temporal_coverage", "device_coverage"],
        "thresholds": {"absolute_bias_fraction": 0.10, "wape": 0.20},
        "minimum_samples": None,
        "repetition_policy": "Pilot first; preregister a sample count using instrument precision and variance.",
        "candidate_methods": ["class_ladder_v2", "nonnegative_prefill_decode_context",
                              "architecture_with_gpu_count_and_host"],
        "cache_experiment": "Pair cache-on/off runs by prompt, revision and configuration; randomize order.",
        "refit_cadence": "Every six months or when a new hardware generation is introduced.",
        "promotion": "Independent review, adequate repetitions and subgroup checks required; metrics alone do not promote.",
    }


def _metrics(rows: list[dict], prediction_key: str) -> dict:
    pairs, failures, coverage = [], 0, []
    for row in rows:
        predicted = row.get(prediction_key)
        if predicted is None:
            failures += 1
            continue
        observed = number(row["observed_wh"], "observed_wh")
        prediction = number(predicted, prediction_key)
        pairs.append((observed, prediction))
        interval_prefix = "" if prediction_key == "predicted_wh" else "baseline_"
        low_key, high_key = f"{interval_prefix}interval_low_wh", f"{interval_prefix}interval_high_wh"
        if row.get(low_key) is not None and row.get(high_key) is not None:
            low, high = (number(row[k], k) for k in (low_key, high_key))
            if low > high:
                raise ValueError("interval bounds reversed")
            coverage.append(low <= observed <= high)
    absolute = sorted(abs(p - o) for o, p in pairs)
    measured = sum((o for o, _ in pairs), Decimal(0))
    bias = sum((p - o for o, p in pairs), Decimal(0))
    return {
        "observations": len(rows), "paired": len(pairs), "prediction_failures": failures,
        "observed_wh_subtotal": float(measured),
        "signed_bias_wh": float(bias) if pairs else None,
        "signed_bias_fraction": float(bias / measured) if measured else None,
        "median_absolute_error_wh": float(median(absolute)) if absolute else None,
        "p95_absolute_error_wh": float(absolute[max(0, (95 * len(absolute) + 99) // 100 - 1)]) if absolute else None,
        "wape": float(sum(absolute) / measured) if measured else None,
        "interval_coverage": sum(coverage) / len(coverage) if coverage else None,
        "interval_observations": len(coverage),
    }


def evaluate(rows: Iterable[dict]) -> dict:
    """Held-out diagnostics only. Refuse leakage or incompatible observations.

    Each row has id, split, workload_family, serving_configuration, boundary,
    observed_wh, predicted_wh and optional baseline_wh / interval bounds.
    The caller fits candidates on the fit split; this function never fits on
    held-out data or automatically changes production coefficients.
    """
    rows = list(rows)
    _unique(rows, "id")
    memberships: dict[tuple[str, str], str] = {}
    for row in rows:
        if row.get("split") not in {"fit", "validation", "held_out"}:
            raise ValueError("each observation needs a valid split")
        for name in ("workload_family", "serving_configuration"):
            if not row.get(name):
                raise ValueError(f"missing {name}")
            key = (name, str(row[name]))
            if key in memberships and memberships[key] != row["split"]:
                raise ValueError(f"split leakage: {name}={row[name]}")
            memberships[key] = row["split"]
        if row.get("boundary") != "node_it":
            raise ValueError("validation protocol requires node_it observations")
        number(row.get("observed_wh"), "observed_wh")
        for field in ("instrument_validation_id", "reference_meter_id"):
            if not row.get(field):
                raise ValueError(f"missing measured evidence: {field}")
        for field in ("temporal_coverage", "device_coverage"):
            if number(row.get(field), field) != 1:
                raise ValueError(f"incomplete measurement: {field}")
    held = [r for r in rows if r["split"] == "held_out"]
    metrics = _metrics(held, "predicted_wh")
    baseline = _metrics(held, "baseline_wh")
    subgroups = defaultdict(list)
    for row in held:
        for name in ("workload_family", "serving_configuration", "cache", "reasoning", "input"):
            subgroups[f"{name}:{row.get(name, 'unknown')}"] .append(row)
    thresholds_met = (bool(held) and metrics["prediction_failures"] == 0
                      and metrics["signed_bias_fraction"] is not None
                      and abs(metrics["signed_bias_fraction"]) <= 0.10
                      and metrics["wape"] <= 0.20)
    return {"protocol_id": "tret_energy_validation_v1", "status": "requires_independent_review",
            "splits": dict(Counter(r["split"] for r in rows)), "held_out": metrics,
            "baseline_held_out": baseline, "numerical_targets_met": thresholds_met,
            "subgroups": {key: {"candidate": _metrics(values, "predicted_wh"),
                                 "baseline": _metrics(values, "baseline_wh")}
                          for key, values in subgroups.items()},
            "accuracy_claim": None,
            "limitations": ["Sample adequacy and instrument validity require independent review.",
                            "No hosted-provider accuracy can be established from a local meter."]}


def cache_experiment(rows: Iterable[dict]) -> dict:
    pairs = defaultdict(dict)
    for row in rows:
        if row.get("cache") not in {"on", "off"}:
            raise ValueError("cache must be on or off")
        key = tuple(row.get(k) for k in ("pair_id", "model_revision", "configuration", "prompt_hash"))
        if any(v is None for v in key) or row["cache"] in pairs[key]:
            raise ValueError("cache pairs require unique complete matching identities")
        if row.get("boundary") != "node_it" or not row.get("instrument_validation_id"):
            raise ValueError("cache experiment requires validated node_it measurement")
        if any(number(row.get(k), k) != 1 for k in ("temporal_coverage", "device_coverage")):
            raise ValueError("cache experiment requires complete measurement coverage")
        pairs[key][row["cache"]] = number(row["observed_wh"], "observed_wh")
    complete = [p for p in pairs.values() if set(p) == {"on", "off"}]
    off = sum((p["off"] for p in complete), Decimal(0))
    on = sum((p["on"] for p in complete), Decimal(0))
    return {"pairs": len(complete), "incomplete_pairs": len(pairs) - len(complete),
            "cache_on_wh": float(on), "cache_off_wh": float(off),
            "whole_request_energy_ratio": float(on / off) if off else None,
            "prefill_weight": None,
            "note": "Whole-request ratio includes decode and overhead; it is not a prefill coefficient."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("inventory", "protocol", "evaluate", "cache", "cohort", "shadow"))
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start")
    parser.add_argument("--end")
    args = parser.parse_args()
    rows = []
    if args.command != "protocol":
        if args.input is None:
            parser.error("--input is required")
        data = args.input.read_text()
        try:
            rows = json.loads(data)
        except json.JSONDecodeError:
            rows = [json.loads(s) for s in data.splitlines() if s.strip()]
        if isinstance(rows, dict) and args.command != "cohort":
            rows = [rows]
        if args.command != "cohort" and not isinstance(rows, list):
            parser.error("--input must contain a JSON array/object or JSONL objects")
    if args.command == "inventory":
        if not args.start or not args.end:
            parser.error("inventory requires --start and --end")
        report = inventory(rows, start=args.start, end=args.end)
    elif args.command == "evaluate":
        report = evaluate(rows)
    elif args.command == "cache":
        report = cache_experiment(rows)
    elif args.command == "cohort":
        from tret.services.emissions_tasks import task_cohort
        report = task_cohort(rows["activities"], rows["deliverables"], quality_gate=rows["quality_gate"])
    elif args.command == "shadow":
        report = shadow_report(rows)
    else:
        report = benchmark_protocol()
    args.output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
