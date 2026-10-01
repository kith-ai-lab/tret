"""Pure per-call cloud emissions accounting with token reconciliation."""
from __future__ import annotations

import dataclasses
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable

from tret.config import Settings
from tret.services.emissions import combine_accountings, energy_accounting, _union_by_key
from tret.services.emission_factors import (
    FactorSet,
    context_with_run_overrides,
    factor_set_for_call,
)

_TOKEN_KEYS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")


def _tokens(value: Any) -> tuple[int, int, int, int]:
    if value is None:
        raise ValueError("aggregate billed usage is required")
    values = [getattr(value, key, None) if not isinstance(value, dict) else value.get(key)
              for key in _TOKEN_KEYS]
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in values):
        raise ValueError("billing token buckets must be nonnegative integers")
    return tuple(values)  # type: ignore[return-value]


def _zero_embodied(factors: FactorSet) -> FactorSet:
    context = factors.resolution_context
    if context is not None:
        context = context_with_run_overrides(context, {"embodied_g": 0})
    return dataclasses.replace(
        factors,
        embodied_g=dataclasses.replace(factors.embodied_g, value=Decimal(0)),
        resolution_context=context,
    )


def account_call_records(
    model,
    call_records: Iterable[dict],
    *,
    billed_usage: Any | None = None,
    totals: Any | None = None,
    factors: FactorSet,
    settings: Settings | None = None,
    catalog=None,
    at: datetime | None = None,
    resolve_call_factors=None,
) -> dict | None:
    """Account reconciled estimated cloud calls, or return ``None`` on gaps.

    A record with estimated/unavailable usage is a coverage gap even when all
    four buckets are zero. ``inference_geo`` is retained as evidence only.

    ``resolve_call_factors`` — when given, a callable with
    ``factor_set_for_call``'s own signature — replaces the direct
    ``factor_set_for_call`` call below. It exists so a caller that resolves
    many call records per turn (`ModelSegment.accounting`, see
    `engine.harness._EmissionsContext.resolve_call_factors`) can cache/bound
    that work across a whole run instead of re-resolving the full factor
    ladder once per call record on every turn. Left ``None`` (e.g. the SDK's
    own call site), this behaves exactly as before: one uncached
    `factor_set_for_call` call per record.
    """
    resolver = resolve_call_factors or factor_set_for_call
    records = list(call_records)
    if not records:
        return None
    aggregate = _tokens(billed_usage if billed_usage is not None else totals)
    parsed: list[tuple[dict, tuple[int, int, int, int]]] = []
    for record in records:
        if record.get("usage_status", "reported") != "reported":
            return None
        try:
            call_tokens = _tokens(record)
        except ValueError:
            return None
        parsed.append((record, call_tokens))
    if tuple(sum(values[i] for _, values in parsed) for i in range(4)) != aggregate:
        return None

    blocks: list[dict] = []
    provenance: list[dict] = []
    known_additional = 0
    unknown_reasoning = 0
    for index, (record, call_tokens) in enumerate(parsed):
        start, end = at, None
        if record.get("started_at") is not None or record.get("ended_at") is not None:
            try:
                start = datetime.fromisoformat(record["started_at"])
                end = datetime.fromisoformat(record["ended_at"])
                if start.tzinfo is None or end.tzinfo is None or end <= start:
                    return None
            except (KeyError, TypeError, ValueError):
                return None
        call_factors = resolver(
            factors, provider=model.provider, model_id=model.id,
            served_by=record.get("served_by"), at=start, interval_end=end,
        )
        if call_factors is None:
            if record.get("served_by"):
                return None
            call_factors = factors
        if index:
            call_factors = _zero_embodied(call_factors)
        reasoning = record.get("reasoning_tokens")
        semantics = record.get("reasoning_accounting") or "unknown"
        if reasoning is not None and (
            isinstance(reasoning, bool) or not isinstance(reasoning, int) or reasoning < 0
        ):
            return None
        if semantics not in {"counted_in_output", "additional", "unknown"}:
            return None
        extra = reasoning if semantics == "additional" and reasoning is not None else 0
        known_additional += extra
        if reasoning is None or semantics == "unknown":
            unknown_reasoning += 1
        block = energy_accounting(
            model, *call_tokens, settings=settings, catalog=catalog, factors=call_factors,
            energy_output_tokens=call_tokens[1] + extra,
            reasoning_tokens=reasoning, reasoning_accounting=semantics,
        )
        if index:
            # The internal zero avoids repeating the flat allocation. Restore
            # its original evidence instead of reporting a user run override.
            for side, first_side in ((block, blocks[0]),
                                     (block["baseline"], blocks[0]["baseline"])):
                first_embodied = next((entry for entry in first_side.get("factors", [])
                                       if entry["key"] == "embodied_hardware"), None)
                if first_embodied:
                    side["factors"] = [
                        dict(first_embodied, value=0, allocation="already_allocated_to_first_call")
                        if entry["key"] == "embodied_hardware" else entry
                        for entry in side.get("factors", [])
                    ]
        blocks.append(block)
        provenance.append({
            "index": index,
            "iteration": record.get("iteration"),
            "served_by": record.get("served_by"),
            "inference_geo": record.get("inference_geo"),
            "usage_status": "reported",
            "started_at": record.get("started_at"),
            "ended_at": record.get("ended_at"),
            "grid_temporal": block.get("grid_temporal"),
            "grid_g_per_kwh": block.get("grid_co2e_g_per_kwh"),
            "billed_tokens": dict(zip(_TOKEN_KEYS, call_tokens)),
            "energy_output_tokens": call_tokens[1] + extra,
            "pue": block["pue"],
            "pue_disclosure": block.get("pue_disclosure"),
            "site_wue_l_per_kwh": call_factors.water.site_wue_l_per_kwh,
            "water_disclosure": next(
                (r.get("disclosure") for r in call_factors.water.records
                 if r["key"] == "site_wue_l_per_kwh"), None,
            ),
            "co2e_g": block.get("co2e_g"),
        })
    combined = combine_accountings(blocks)
    if combined is None:
        return None
    # The combined figure owns the once-per-segment allocation, while call
    # details above retain the zeroed subsequent arithmetic.
    for side, first_side, all_sides in (
        (combined, blocks[0], blocks),
        (combined["baseline"], blocks[0]["baseline"], [b["baseline"] for b in blocks]),
    ):
        first_embodied = next((entry for entry in first_side.get("factors", [])
                               if entry["key"] == "embodied_hardware"), None)
        side["factors"] = [
            dict(first_embodied, allocation="once_per_segment")
            if len(blocks) > 1 and first_embodied and entry["key"] == "embodied_hardware" else entry
            for entry in _union_by_key(all_sides, "factors")
        ]
    shadows = [block.get("energy_method_shadow") for block in blocks]
    if all(shadow and shadow.get("method_id") == "class_ladder_v1" for shadow in shadows):
        combined["energy_method_shadow"] = {
            "method_id": "class_ladder_v1",
            "calibration_id": "jegham_2025_legacy_unresolved_boundary",
            "energy_boundary": "unknown",
            "energy_wh": round(sum(shadow["energy_wh"] for shadow in shadows), 6),
            "energy_wh_total": round(sum(shadow["energy_wh_total"] for shadow in shadows), 6),
            "co2e_g": (
                round(sum(shadow["co2e_g"] for shadow in shadows), 6)
                if combined.get("carbon_summable") is not False
                else None
            ),
            "difference_kind": "methodology_correction_not_emissions_savings",
        }
    combined["call_accountings"] = provenance
    combined["call_accounting_method"] = "reconciled_per_call_v1"
    combined["functional_unit"] = "one_run"
    combined["temporal_allocation"] = (
        "constant_power_within_each_call" if all(record.get("started_at") for record, _ in parsed)
        else "point_in_time_fallback"
    )
    combined["reasoning_coverage"] = {
        "known_additional_tokens": known_additional,
        "calls_unknown_reasoning": unknown_reasoning,
        "complete": unknown_reasoning == 0,
    }
    return combined
