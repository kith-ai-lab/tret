"""Coverage envelopes separate an included subtotal from a complete result.

No software check alone establishes conformity with a standard. Standards
profiles are mappings for review, not a certification mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from tret.services.emissions_validation import number


@dataclass(frozen=True)
class ComponentCoverage:
    component_id: str
    lifecycle_stage: str
    status: str
    value: Decimal | None
    unit: str
    boundary: str
    evidence_ids: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self):
        if self.status not in {"observed", "modeled", "supplied", "excluded", "unknown", "not_applicable"}:
            raise ValueError("invalid component status")
        if self.status in {"excluded", "unknown"} and self.value is not None:
            raise ValueError("excluded/unknown components cannot have numeric values")
        if self.status in {"observed", "modeled", "supplied"} and self.value is None:
            raise ValueError("included components require a value")
        if self.status == "not_applicable" and (not self.reason.strip() or not self.evidence_ids):
            raise ValueError("not_applicable requires an applicability reason and evidence")
        if self.value is not None:
            number(self.value, self.component_id)
        if self.value == 0 and self.status != "not_applicable" and not self.evidence_ids:
            raise ValueError("zero requires evidence or a derivation")

    def record(self) -> dict:
        return {"component_id": self.component_id, "lifecycle_stage": self.lifecycle_stage,
                "status": self.status, "value": float(self.value) if self.value is not None else None,
                "unit": self.unit, "boundary": self.boundary,
                "evidence_ids": list(self.evidence_ids), "reason": self.reason}


def coverage_envelope(components: Iterable[ComponentCoverage], *, required: Iterable[str],
                      functional_unit: str, compatible: bool = True) -> dict:
    components, required = list(components), set(required)
    names = [c.component_id for c in components]
    if len(set(names)) != len(names):
        raise ValueError("duplicate coverage component")
    if not functional_unit:
        raise ValueError("functional unit is required")
    missing = sorted(required - {c.component_id for c in components
                                 if c.status not in {"excluded", "unknown"}})
    subtotals: dict[str, Decimal] = {}
    for component in components:
        if component.value is not None:
            subtotals[component.unit] = subtotals.get(component.unit, Decimal(0)) + component.value
    complete = not missing and compatible
    return {"schema_version": "component_coverage_v1", "functional_unit": functional_unit,
            "components": [c.record() for c in components], "missing": missing,
            "covered_subtotal": {k: float(v) for k, v in subtotals.items()},
            "complete_total": {k: float(v) for k, v in subtotals.items()} if complete else None,
            "compatible": compatible,
            "alignment": {"profile_id": "operational_inference_v1",
                          "status": "eligible_for_review" if complete else "incomplete",
                          "conformity_claim": None}}


def operational_coverage(accounting: dict) -> dict:
    """Conservative additive view for a new accounting record, never rewrite history.

    The operational carbon figure already includes applied facility overhead;
    it is one component, not added again as separate GPU and node figures.
    Unknown cloud hardware remains absent even if compatibility fields say 0.
    """
    boundary = accounting.get("energy_boundary", "unknown")
    method = accounting.get("method_id", "unknown")
    co2 = accounting.get("co2e_operational_g")
    if co2 is None:
        co2 = accounting.get("operational_co2e_g")
    if co2 is None and accounting.get("co2e_g") is not None and accounting.get("embodied_g") is not None:
        co2 = number(accounting["co2e_g"], "co2e_g") - number(accounting["embodied_g"], "embodied_g")
    elif co2 is None and accounting.get("co2e_g") is not None and accounting.get("embodied_g") is None:
        # Null embodied means the allocation is unknown, not zero (see the A8
        # null-embodied path in emissions.py) — but it also means embodied
        # contributed nothing to co2e_g, so the whole figure is operational.
        co2 = number(accounting["co2e_g"], "co2e_g")
    derivation_ids = (method,) if method and method not in {"unknown", "mixed"} else ()
    if co2 is not None and co2 == 0 and (not derivation_ids or boundary in {None, "unknown", "mixed"}):
        co2 = None
    components = [ComponentCoverage(
        "inference_electricity", "operation", "modeled" if co2 is not None else "unknown",
        number(co2, "operational_co2e_g") if co2 is not None else None,
        "gCO2e", boundary, derivation_ids, "Operational electricity carbon at the recorded boundary.",
    )]
    embodied = accounting.get("embodied_g")
    has_embodied = embodied is not None and number(embodied, "embodied_co2e_g") > 0
    components.append(ComponentCoverage(
        "serving_hardware", "manufacture", "supplied" if has_embodied else "unknown",
        number(embodied, "embodied_co2e_g") if has_embodied else None,
        "gCO2e", "hardware", ("embodied_factor",) if has_embodied else (),
        "Supplied allocation" if has_embodied else "No supported hardware footprint allocation.",
    ))
    for name in ("idle_reserve", "network_storage", "material_tools"):
        components.append(ComponentCoverage(name, "operation", "unknown", None, "gCO2e", "unknown",
                                            reason="Not observed or separately modeled."))
    required = [c.component_id for c in components]
    if boundary not in {"node_it", "facility"}:
        components.append(ComponentCoverage("remaining_node", "operation", "unknown", None,
                                            "gCO2e", "node_it", reason="Incomplete node energy coverage."))
        required.append("remaining_node")
    result = coverage_envelope(components, required=required, functional_unit="one_run",
                               compatible=accounting.get("carbon_summable", True)
                               and accounting.get("grid_co2e_basis") != "mixed")
    result["standards_alignment"] = {
        "profile_id": "sci_ai_consumer_candidate",
        "spec_revision": "e8d3534f72b26e7b114c9054050db60f4543bb60",
        "spec_uri": "https://github.com/Green-Software-Foundation/sci-ai/blob/e8d3534f72b26e7b114c9054050db60f4543bb60/SPEC.md",
        "status": "incomplete", "conformity_claim": None,
        "missing": ["complete_operation_and_monitoring_boundary", "complete_hardware_allocation",
                    "declared_comparable_functional_unit", "independent_review"],
    }
    return result
