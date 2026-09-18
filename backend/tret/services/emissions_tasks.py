"""Explicit task-cohort accounting, with failed attempts and coverage retained.

An activity is a self-only accounting entry or an inclusive rollup. A caller
must supply attribution and a versioned quality gate; conversation membership
alone is insufficient evidence of a shared deliverable.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from tret.services.emissions_validation import number


def task_cohort(activities: Iterable[dict], deliverables: Iterable[dict], *, quality_gate: str) -> dict:
    activities, deliverables = list(activities), list(deliverables)
    if not quality_gate:
        raise ValueError("a versioned quality_gate is required")
    activity_ids, accounting_ids, deliverable_ids = set(), set(), set()
    by_id = {}
    for item in deliverables:
        ident = item.get("deliverable_id")
        if not ident or ident in deliverable_ids:
            raise ValueError("duplicate or missing deliverable_id")
        deliverable_ids.add(ident)
        if item.get("quality_gate") != quality_gate or type(item.get("accepted")) is not bool:
            raise ValueError("all deliverables require an explicit result under the same quality gate")
    for item in activities:
        ident, accounting = item.get("activity_id"), item.get("accounting_id")
        if not ident or ident in activity_ids or not accounting or accounting in accounting_ids:
            raise ValueError("duplicate or missing activity/accounting identity")
        if item.get("aggregation_mode", "self_only") not in {"self_only", "inclusive"}:
            raise ValueError("invalid aggregation_mode")
        if item.get("deliverable_id") not in deliverable_ids:
            raise ValueError("activity must be attributed to a declared deliverable")
        activity_ids.add(ident)
        accounting_ids.add(accounting)
        by_id[ident] = item
    for item in activities:
        seen = {item["activity_id"]}
        parent = item.get("parent_id")
        while parent:
            if parent not in by_id or parent in seen:
                raise ValueError("missing parent or activity cycle")
            seen.add(parent)
            if by_id[parent].get("aggregation_mode") == "inclusive":
                raise ValueError("inclusive parent and child would double count energy")
            if by_id[parent]["deliverable_id"] != item["deliverable_id"]:
                raise ValueError("parent and child attribution disagree")
            parent = by_id[parent].get("parent_id")
    groups = defaultdict(lambda: {"energy_wh": Decimal(0), "co2e_g": Decimal(0),
                                   "activities": 0, "missing_energy": 0, "missing_carbon": 0})
    signature_fields = ("energy_boundary", "accounting_basis", "factor_boundary", "gas_coverage", "gwp_basis",
                        "gwp_horizon_years", "includes_td_losses", "electricity_mix_basis")
    for item in activities:
        domains = {
            "energy_boundary": {"gpu", "node_it", "facility", "partial", "unknown"},
            "accounting_basis": {"location_based", "market_based", "unspecified", "unknown"},
            "factor_boundary": {"generation", "upstream", "lifecycle", "unknown"},
            "gas_coverage": {"co2", "co2e", "unknown"},
            "gwp_basis": {"ar4", "ar5", "ar6", "unknown"},
            "electricity_mix_basis": {"production", "consumption", "unknown"},
        }
        for field, allowed in domains.items():
            if item.get(field) is not None and item[field] not in allowed:
                raise ValueError(f"invalid {field}")
        horizon = item.get("gwp_horizon_years")
        if horizon is not None and (type(horizon) is not int or horizon <= 0):
            raise ValueError("gwp_horizon_years must be a positive integer")
        losses = item.get("includes_td_losses")
        if losses is not None and type(losses) is not bool:
            raise ValueError("includes_td_losses must be a boolean")
        key = tuple(item.get(k) if item.get(k) is not None else "unknown" for k in signature_fields)
        group = groups[key]
        group["activities"] += 1
        for field, missing in (("energy_wh", "missing_energy"), ("co2e_g", "missing_carbon")):
            if item.get(field) is None:
                group[missing] += 1
            else:
                group[field] += number(item[field], field)
    accepted = sum(item["accepted"] for item in deliverables)
    result_groups = []
    for key, value in groups.items():
        entry = dict(zip(signature_fields, key))
        entry.update({k: float(v) if isinstance(v, Decimal) else v for k, v in value.items()})
        known_basis = "unknown" not in key and key[1] != "unspecified"
        if not known_basis:
            entry["co2e_g"] = None
        entry["carbon_compatible"] = known_basis and not value["missing_carbon"]
        entry["energy_wh_per_accepted_task"] = (
            float(value["energy_wh"] / accepted) if accepted and not value["missing_energy"] else None
        )
        entry["co2e_g_per_accepted_task"] = (
            float(value["co2e_g"] / accepted)
            if accepted and not value["missing_carbon"] and known_basis else None
        )
        result_groups.append(entry)
    return {"schema_version": "task_cohort_v1", "functional_unit": "accepted_deliverable",
            "quality_gate": quality_gate, "deliverables": len(deliverables), "accepted": accepted,
            "success_rate": accepted / len(deliverables) if deliverables else None,
            "activities": len(activities),
            "failed_activities": sum(a.get("status") in {"failed", "cancelled"} for a in activities),
            "groups": result_groups,
            "combined_carbon_total": result_groups[0]["co2e_g"] if len(result_groups) == 1
            and result_groups[0]["carbon_compatible"] else None,
            "coverage_note": "Only explicitly attributed activities are covered; material unobserved work remains excluded."}
