"""Offline replay of historical token ledgers through class-ladder v1 and v2.

This module reads an allowlisted JSON/JSONL export.  It never opens a database,
loads runtime settings, resolves the live model catalog, or calls a provider.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
from pathlib import Path
from typing import Iterable

from tret.services.emissions import (
    ENERGY_CACHE_READ_MULTIPLIER,
    ENERGY_CACHE_WRITE_MULTIPLIER,
    ENERGY_CLASS_WH_PER_MTOK_V1,
    ENERGY_CLASS_WH_PER_MTOK_V2,
    ENERGY_TOKEN_WEIGHT_INPUT,
    ENERGY_TOKEN_WEIGHT_OUTPUT,
)
from tret.services.emissions_validation import number, shadow_report

TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
WEIGHT_KEYS = ("input_weight", "output_weight", "cache_read_weight", "cache_write_weight")
DEFAULT_WEIGHTS = (
    ENERGY_TOKEN_WEIGHT_INPUT,
    ENERGY_TOKEN_WEIGHT_OUTPUT,
    ENERGY_CACHE_READ_MULTIPLIER,
    ENERGY_CACHE_WRITE_MULTIPLIER,
)
FACTOR_WEIGHT_KEYS = (
    "token_weight_input", "token_weight_output",
    "token_weight_cache_read", "token_weight_cache_write",
)
LADDER_METHODS = {"class_ladder", "class_ladder_v1", "class_ladder_v2"}


def _integer_buckets(record: dict) -> tuple[int, int, int, int] | None:
    values = tuple(record.get(key) for key in TOKEN_KEYS)
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in values):
        return None
    return values  # type: ignore[return-value]


def _weights(accounting: dict) -> tuple[tuple[Decimal, ...], str] | None:
    factors = accounting.get("factors") or []
    resolved: list[Decimal | None] = []
    any_recorded = False
    for top_key, factor_key in zip(WEIGHT_KEYS, FACTOR_WEIGHT_KEYS):
        candidates = []
        if accounting.get(top_key) is not None:
            candidates.append(number(accounting[top_key], top_key))
        candidates.extend(
            number(factor.get("value"), factor_key)
            for factor in factors
            if isinstance(factor, dict) and factor.get("key") == factor_key
        )
        distinct = set(candidates)
        if len(distinct) > 1:
            return None
        resolved.append(next(iter(distinct)) if distinct else None)
        any_recorded = any_recorded or bool(distinct)
    if any_recorded and any(value is None for value in resolved):
        return None
    if any_recorded:
        return tuple(value for value in resolved if value is not None), "recorded"
    return tuple(Decimal(str(value)) for value in DEFAULT_WEIGHTS), "declared_ladder_default"


def _energy_output_tokens(
    record: dict, accounting: dict, buckets: tuple[int, int, int, int]
) -> tuple[int, str, int] | None:
    stored = accounting.get("energy_output_tokens")
    if stored is not None:
        if isinstance(stored, bool) or not isinstance(stored, int) or stored < buckets[1]:
            return None

    calls = record.get("call_records") or []
    if calls:
        parsed = [_integer_buckets(call) for call in calls]
        if any(value is None for value in parsed):
            return None
        if any(call.get("usage_status", "reported") != "reported" for call in calls):
            return None
        assert all(value is not None for value in parsed)
        if tuple(sum(value[i] for value in parsed) for i in range(4)) != buckets:
            return None
        additional = 0
        unknown = 0
        for call in calls:
            semantics = call.get("reasoning_accounting") or "unknown"
            reasoning = call.get("reasoning_tokens")
            if reasoning is not None and (
                isinstance(reasoning, bool) or not isinstance(reasoning, int) or reasoning < 0
            ):
                return None
            if semantics not in {"counted_in_output", "additional", "unknown"}:
                return None
            if semantics == "additional" and reasoning is not None:
                additional += reasoning
            elif semantics == "unknown" or reasoning is None:
                unknown += 1
        reconciled = buckets[1] + additional
        if stored is not None and stored != reconciled:
            return None
        return (
            reconciled,
            "recorded_energy_output_tokens_reconciled_calls"
            if stored is not None else "reconciled_call_reasoning",
            unknown,
        )

    reasoning = record.get("reasoning_tokens")
    semantics = record.get("reasoning_accounting") or "unknown"
    reasoning_valid = isinstance(reasoning, int) and not isinstance(reasoning, bool) and reasoning >= 0
    if semantics == "additional" and reasoning_valid:
        reconciled = buckets[1] + reasoning
        if stored is not None and stored != reconciled:
            return None
        return reconciled, (
            "recorded_energy_output_tokens_reconciled_aggregate"
            if stored is not None else "recorded_aggregate_additional_reasoning"
        ), 0
    if semantics == "counted_in_output" and reasoning_valid:
        if stored is not None and stored != buckets[1]:
            return None
        return buckets[1], (
            "recorded_energy_output_tokens_reconciled_aggregate"
            if stored is not None else "recorded_aggregate_counted_reasoning"
        ), 0
    if stored is not None:
        return stored, "recorded_energy_output_tokens_unknown_reasoning", 1
    return buckets[1], "billed_output_unknown_reasoning", 1


def _wh(coefficient: Decimal, tokens: tuple[int, int, int, int], weights: tuple[Decimal, ...]) -> Decimal:
    weighted = sum((Decimal(value) * weight for value, weight in zip(tokens, weights)), Decimal(0))
    return coefficient * weighted / Decimal(1_000_000)


def _units(row: dict) -> tuple[list[tuple[str, dict]], str | None]:
    timeline = row.get("model_timeline") or []
    if timeline:
        if not isinstance(timeline, list) or any(not isinstance(segment, dict) for segment in timeline):
            return [], "invalid_model_timeline"
        parent = _integer_buckets(row)
        segment_buckets = [_integer_buckets(segment) for segment in timeline]
        if parent is None or any(bucket is None for bucket in segment_buckets):
            return [], "missing_or_invalid_token_bucket"
        assert all(bucket is not None for bucket in segment_buckets)
        if tuple(sum(bucket[index] for bucket in segment_buckets) for index in range(4)) != parent:
            return [], "unreconciled_segment_token_allocation"
        return [(f"{row.get('id')}#segment-{index}", segment) for index, segment in enumerate(timeline)], None
    return [(str(row.get("id")), row)], None


def _ladder_method(accounting: dict) -> str | None:
    if accounting.get("energy_source") in {"measured", "mixed"}:
        return None
    if "method_id" in accounting and accounting.get("method_id") is not None:
        candidate = accounting["method_id"]
        return candidate if candidate in LADDER_METHODS else None
    if "energy_strategy" in accounting and accounting.get("energy_strategy") is not None:
        candidate = accounting["energy_strategy"]
        return candidate if candidate in LADDER_METHODS else None
    for factor in accounting.get("factors") or []:
        if isinstance(factor, dict) and factor.get("key") == "energy_class":
            if factor.get("strategy") in LADDER_METHODS:
                return factor["strategy"]
    return None


def _source_hashes() -> dict[str, str]:
    root = Path(__file__).resolve().parents[1]
    paths = {
        "runtime_coefficients": Path(__file__).with_name("emissions.py"),
        "v1_source_manifest": root / "data/calibration/jegham_2025_v1.json",
        "v2_method_manifest": root / "data/calibration/class_ladder_v2.json",
    }
    return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}


def replay(rows: Iterable[dict]) -> dict:
    """Reconstruct paired energy-method estimates without mutating input rows."""
    rows = list(rows)
    before = deepcopy(rows)
    exclusions: Counter[str] = Counter()
    assumptions: Counter[str] = Counter()
    paired: list[dict] = []
    eligible_runs: set[str] = set()

    ids: set[str] = set()
    for row in rows:
        run_id = row.get("id")
        if not isinstance(run_id, str) or not run_id or run_id in ids:
            exclusions["missing_or_duplicate_run_id"] += 1
            continue
        ids.add(run_id)
        units, reason = _units(row)
        if reason:
            exclusions[reason] += 1
            continue
        run_pairs: list[dict] = []
        run_assumptions: Counter[str] = Counter()
        failed = None
        for unit_id, unit in units:
            if units[0][1] is not row and not isinstance(unit.get("energy_accounting"), dict):
                failed = "missing_segment_energy_accounting"
                break
            if unit.get("estimated_usage") is True or unit.get("estimated") is True:
                failed = "estimated_segment_usage"
                break
            accounting = unit.get("energy_accounting") or {}
            method = _ladder_method(accounting)
            if method is None:
                failed = "non_ladder_or_measured_method"
                break
            model = unit.get("model") or accounting.get("model") or row.get("model")
            energy_class = accounting.get("energy_class")
            if not isinstance(model, str) or not model:
                failed = "unknown_model"
                break
            if energy_class not in ENERGY_CLASS_WH_PER_MTOK_V1:
                failed = "missing_or_unknown_energy_class"
                break
            buckets = _integer_buckets(unit)
            if buckets is None:
                failed = "missing_or_invalid_token_bucket"
                break
            try:
                weight_result = _weights(accounting)
            except ValueError:
                failed = "partial_or_invalid_recorded_weights"
                break
            if weight_result is None:
                failed = "partial_or_invalid_recorded_weights"
                break
            weights, weight_source = weight_result
            output_result = _energy_output_tokens(unit, accounting, buckets)
            if output_result is None:
                failed = "unreconciled_or_invalid_call_usage"
                break
            energy_output, denominator_source, unknown_reasoning = output_result
            energy_tokens = (buckets[0], energy_output, buckets[2], buckets[3])
            v1 = _wh(ENERGY_CLASS_WH_PER_MTOK_V1[energy_class], energy_tokens, weights)
            v2 = _wh(ENERGY_CLASS_WH_PER_MTOK_V2[energy_class], energy_tokens, weights)
            run_assumptions[f"token_weights:{weight_source}"] += 1
            run_assumptions[f"output_denominator:{denominator_source}"] += 1
            if unknown_reasoning:
                run_assumptions["units_with_unknown_reasoning_denominator"] += 1
            observed = accounting.get("energy_wh")
            try:
                observed_wh = None if observed is None else float(number(observed, "observed_energy_wh"))
            except ValueError:
                failed = "invalid_observed_stored_energy"
                break
            run_pairs.append({
                "id": unit_id,
                "run_id": run_id,
                "model": model,
                "energy_class": energy_class,
                "tokens": dict(zip(TOKEN_KEYS, buckets)),
                "energy_output_tokens": energy_output,
                "token_weights": dict(zip(WEIGHT_KEYS, (float(v) for v in weights))),
                "weight_source": weight_source,
                "output_denominator_source": denominator_source,
                "reasoning_denominator_complete": unknown_reasoning == 0,
                "observed_stored": {"method_id": method, "energy_wh": observed_wh},
                "reconstructed": {"class_ladder_v1_wh": float(v1), "class_ladder_v2_wh": float(v2)},
                "percent_change_v2_vs_v1": float((v2 - v1) * 100 / v1) if v1 else None,
            })
        if failed:
            exclusions[failed] += 1
            continue
        eligible_runs.add(run_id)
        assumptions.update(run_assumptions)
        paired.extend(run_pairs)

    shadow_rows = [{
        "id": item["id"],
        "energy_accounting": {
            "model": item["model"], "method_id": "class_ladder_v2",
            "energy_wh": item["reconstructed"]["class_ladder_v2_wh"],
            "energy_method_shadow": {"method_id": "class_ladder_v1",
                                     "energy_wh": item["reconstructed"]["class_ladder_v1_wh"]},
        },
    } for item in paired]
    shadow = shadow_report(shadow_rows)
    by_class: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"units": 0, "v1_wh": Decimal(0), "v2_wh": Decimal(0)}
    )
    for item in paired:
        group = by_class[item["energy_class"]]
        group["units"] += 1
        group["v1_wh"] += Decimal(str(item["reconstructed"]["class_ladder_v1_wh"]))
        group["v2_wh"] += Decimal(str(item["reconstructed"]["class_ladder_v2_wh"]))

    if rows != before:  # defensive invariant for library callers
        raise RuntimeError("replay mutated its input")
    return {
        "schema_version": "historical_energy_method_replay_v1",
        "evaluation_type": "retrospective_energy_method_replay",
        "prospective_shadow": False,
        "accuracy_claim": False,
        "difference_kind": "methodology_correction_not_emissions_savings",
        "method_boundaries": {
            "class_ladder_v1": {
                "coefficient_boundary": "unresolved_facility_source_calibration",
                "note": (
                    "The published rollback constants retain the facility-source "
                    "calibration boundary; they are not normalized to target-deployment IT load."
                ),
            },
            "class_ladder_v2": {
                "coefficient_boundary": "source_pue_normalized_node_it",
                "note": (
                    "The v2 coefficients divide source observations by source-provider "
                    "PUE before fitting and represent modeled node-IT energy."
                ),
            },
            "replay_factor_application": {
                "target_deployment_pue_applied": False,
                "stored_ledger_boundary": "unknown",
                "comparison_boundary": "coefficient_methodologies_as_published",
            },
        },
        "denominators": {
            "input_runs": len(rows), "eligible_runs": len(eligible_runs),
            "excluded_runs": len(rows) - len(eligible_runs), "paired_units": len(paired),
        },
        "exclusions": dict(sorted(exclusions.items())),
        "assumptions": dict(sorted(assumptions.items())),
        "paired_records": paired,
        "group_stats": {
            key: {"units": int(value["units"]), "v1_wh": float(value["v1_wh"]),
                  "v2_wh": float(value["v2_wh"]),
                  "percent_change_v2_vs_v1": (
                      float((value["v2_wh"] - value["v1_wh"]) * 100 / value["v1_wh"])
                      if value["v1_wh"] else None
                  )}
            for key, value in sorted(by_class.items())
        },
        "shadow_report": shadow,
        "reproducibility": {
            "method_sources_sha256": _source_hashes(),
            "network_access": False, "runtime_settings_used": False,
            "catalog_resolution_used": False,
        },
        "limitations": [
            "This compares two reconstructed energy methods on the same historical token ledger; it is not measured accuracy.",
            "Stored energy is shown only as provenance and is not treated as a reference measurement.",
            "Unknown reasoning uses billed output for both methods and is counted as an explicit denominator assumption.",
            "The coefficient-only replay applies no target-deployment PUE: v1 retains an unresolved facility-source calibration boundary while v2 is source-PUE-normalized node IT, so this is not a matched-boundary facility-energy comparison.",
            "No carbon, savings, or promotion claim is produced.",
        ],
    }


def _read_rows(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        value = [json.loads(line) for line in text.splitlines() if line.strip()]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("input must be a JSON array/object or JSONL objects")
    return value


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(json.dumps(replay(_read_rows(args.input)), indent=2, allow_nan=False) + "\n")


if __name__ == "__main__":
    main()
