"""A conserving energy interval ledger. Observation Wh is entered exactly once.

External node/PDU adapters can submit this format without depending on a GPU
library. Concurrent requests need explicit resource shares; otherwise their
pool is retained as unallocated. No equal-share assumption is implicit.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from tret.services.emissions_validation import number


@dataclass(frozen=True)
class EnergyObservation:
    observation_id: str
    source_id: str
    device_id: str
    start_ns: int
    end_ns: int
    energy_wh: Decimal
    energy_boundary: str
    clock_id: str
    temporal_coverage: Decimal = Decimal(1)
    reserved_wh: Decimal = Decimal(0)

    def __post_init__(self):
        if not all((self.observation_id, self.source_id, self.device_id, self.clock_id)):
            raise ValueError("observation identities and clock are required")
        if self.end_ns <= self.start_ns:
            raise ValueError("observation must have positive duration")
        if self.energy_boundary not in {"gpu", "node_it", "facility", "partial", "unknown"}:
            raise ValueError("invalid energy boundary")
        for key in ("energy_wh", "temporal_coverage", "reserved_wh"):
            object.__setattr__(self, key, number(getattr(self, key), key))
        if self.temporal_coverage > 1 or self.reserved_wh > self.energy_wh:
            raise ValueError("invalid observation coverage or reserve")


@dataclass(frozen=True)
class WorkClaim:
    claim_id: str
    accounting_id: str
    device_id: str
    start_ns: int
    end_ns: int
    clock_id: str
    resource_share: Decimal | None = None

    def __post_init__(self):
        if not all((self.claim_id, self.accounting_id, self.device_id, self.clock_id)) or self.end_ns <= self.start_ns:
            raise ValueError("claim needs identities and a positive interval")
        if self.resource_share is not None:
            object.__setattr__(self, "resource_share", number(self.resource_share, "resource_share"))
            if self.resource_share > 1:
                raise ValueError("resource_share must be <= 1")


def allocate_energy(observations: Iterable[EnergyObservation], claims: Iterable[WorkClaim]) -> dict:
    observations, claims = list(observations), list(claims)
    if len({o.observation_id for o in observations}) != len(observations):
        raise ValueError("duplicate observation")
    if len({c.claim_id for c in claims}) != len(claims) or len({c.accounting_id for c in claims}) != len(claims):
        raise ValueError("duplicate claim/accounting identity")
    # Multiple sources observing the same device/window cannot both enter the
    # ledger. Node meters and GPU submeters must be allocated in separate views.
    for index, obs in enumerate(observations):
        for other in observations[:index]:
            overlaps = max(obs.start_ns, other.start_ns) < min(obs.end_ns, other.end_ns)
            boundaries = {obs.energy_boundary, other.energy_boundary}
            nested_boundaries = (
                "facility" in boundaries and len(boundaries) > 1
            ) or boundaries == {"node_it", "gpu"}
            if overlaps and obs.clock_id == other.clock_id and nested_boundaries:
                raise ValueError("overlapping nested-boundary observations would double count")
            if obs.device_id == other.device_id:
                if obs.clock_id != other.clock_id:
                    raise ValueError("device observations use incompatible clocks")
                if overlaps:
                    raise ValueError("overlapping device observations would double count")
    totals = {c.claim_id: {"allocated_wh_observed": Decimal(0), "covered_ns": 0} for c in claims}
    ledgers = []
    for obs in observations:
        matching = [c for c in claims if c.device_id == obs.device_id
                    and max(c.start_ns, obs.start_ns) < min(c.end_ns, obs.end_ns)]
        if any(c.clock_id != obs.clock_id for c in matching):
            raise ValueError("claim and observation clocks do not match")
        cuts = sorted({obs.start_ns, obs.end_ns} | {max(obs.start_ns, c.start_ns) for c in matching}
                      | {min(obs.end_ns, c.end_ns) for c in matching})
        allocated = {}
        pool = obs.energy_wh - obs.reserved_wh
        for left, right in zip(cuts, cuts[1:]):
            active = [c for c in matching if c.start_ns <= left and c.end_ns >= right]
            if not active or obs.temporal_coverage != 1:
                continue
            shares = [c.resource_share for c in active]
            if len(active) == 1 and shares == [None]:
                shares = [Decimal(1)]
            elif any(s is None for s in shares):
                continue
            if sum(shares, Decimal(0)) > 1:
                raise ValueError("concurrent resource shares exceed one")
            slice_wh = pool * Decimal(right - left) / Decimal(obs.end_ns - obs.start_ns)
            for claim, share in zip(active, shares):
                wh = slice_wh * share
                allocated[claim.claim_id] = allocated.get(claim.claim_id, Decimal(0)) + wh
                totals[claim.claim_id]["allocated_wh_observed"] += wh
                totals[claim.claim_id]["covered_ns"] += right - left
        allocated_sum = sum(allocated.values(), Decimal(0))
        # Decimal division can leave a final ulp. Retain it in the unallocated pool.
        unallocated = obs.energy_wh - obs.reserved_wh - allocated_sum
        if unallocated < 0 and abs(unallocated) <= Decimal("1e-24"):
            last = next(reversed(allocated))
            allocated[last] += unallocated
            totals[last]["allocated_wh_observed"] += unallocated
            unallocated = Decimal(0)
        if unallocated < 0:
            raise ValueError("allocation exceeded observation")
        ledgers.append({"observation_id": obs.observation_id, "measured_wh": obs.energy_wh,
                        "reserved_wh": obs.reserved_wh, "unallocated_wh": unallocated,
                        "allocated_wh": allocated, "energy_boundary": obs.energy_boundary})
    for claim in claims:
        total = totals[claim.claim_id]
        total["temporal_coverage"] = Decimal(total["covered_ns"]) / Decimal(claim.end_ns - claim.start_ns)
        total["complete"] = total["covered_ns"] == claim.end_ns - claim.start_ns
        total["missing_duration_s"] = Decimal(claim.end_ns - claim.start_ns - total["covered_ns"]) / Decimal(10**9)
    return {"method_id": "conserving_interval_allocation_v1", "observations": ledgers, "claims": totals}
