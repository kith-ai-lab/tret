"""Evidence-responsive derivation of the headline uncertainty band.

`tret.services.emissions.uncertainty_contributions` returns per-factor
sensitivity rows that are deliberately NOT multiplied into the headline band
— doing so would give a band wider than any published methodology claims (see
that function's docstring). This module keeps that stance and adds only one
thing: a way for the band to *narrow* in response to evidence an operator
actually has on a specific run, without ever widening past the configured
band and without multiplying anything together.

`Evidence` records what was actually closed out on this run — a metered PUE,
a dated/sourced grid factor, a measured energy figure, a profiled embodied
estimate. `adjust_contributions` tightens the matching contribution rows to
reflect that (returning new rows; it never mutates its input). `derive_band`
then folds the (possibly adjusted) rows into a band bounded above by the
configured low/high: for each axis, the tightest single row's implied bound
is compared against the configured value and the *narrower* of the two wins,
so the band can only ever end up at or inside the configured range, never
outside it. `rule` and `dominant_key` record which row (if any) actually
governed the result, so a stored run can show its work.

Pure and stand-alone: it treats `uncertainty_contributions`'s row shape as a
read-only input contract but does not import the accounting path itself, and
nothing imports this module yet — wiring `Evidence` into a real run and
persisting `band_record`'s output is later work.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable


@dataclass(frozen=True)
class EvidenceRecord:
    """A validated, domain-scoped observation that may narrow one component."""

    id: str
    component: str
    evidence_class: str
    validation_id: str
    source: str
    as_of: str
    boundary: str
    temporal: str
    device_coverage: str
    domain: str
    temporal_coverage: float = 0.0

    def __post_init__(self) -> None:
        required = (self.id, self.component, self.evidence_class, self.validation_id,
                    self.source, self.as_of, self.boundary, self.temporal,
                    self.device_coverage, self.domain)
        if not all(isinstance(value, str) and value.strip() for value in required):
            raise ValueError("evidence records require every identity and validation field")
        if not 0 <= self.temporal_coverage <= 1:
            raise ValueError("temporal coverage must be finite and between zero and one")


@dataclass(frozen=True)
class Evidence:
    energy_measured: bool = False  # operator-supplied Wh, not modeled from tokens
    pue_metered: bool = False  # PUE evidence_class == "measurement", complete device + temporal coverage
    grid_sourced_dated: bool = False  # grid evidence_class in {"published", "measurement"} with a known temporal reference
    embodied_profiled: bool = False  # embodied figure from a named hardware profile
    record_ids: tuple[str, ...] = ()

    @classmethod
    def from_records(
        cls,
        records: Iterable[EvidenceRecord],
        *,
        domain: str,
        energy_boundary: str = "unknown",
    ) -> "Evidence":
        """Derive flags only from validated records matching this exact domain.

        A GPU-only energy observation is intentionally insufficient for complete
        node IT energy. Labels and dates alone never create evidence.
        """
        matched = [r for r in records if r.domain == domain and r.validation_id]
        energy = any(
            r.component == "energy"
            and r.evidence_class == "measurement"
            and r.device_coverage == "complete"
            and r.temporal_coverage == 1
            and r.boundary == energy_boundary
            and energy_boundary in {"node_it", "facility"}
            for r in matched
        )
        return cls(
            energy_measured=energy,
            pue_metered=any(
                r.component == "pue" and r.evidence_class == "measurement"
                and r.device_coverage == "complete" and r.temporal_coverage == 1 for r in matched
            ),
            grid_sourced_dated=any(
                r.component == "grid" and r.evidence_class in {"published", "measurement"}
                and r.temporal != "unknown" for r in matched
            ),
            embodied_profiled=any(
                r.component == "embodied" and r.evidence_class in {"profile", "published"}
                and r.device_coverage == "complete" for r in matched
            ),
            record_ids=tuple(r.id for r in matched),
        )


# Rows narrowed once the run's IT-load energy was measured rather than
# modeled: the class/batch-size/unbatched-inference guesswork those rows
# exist to bound no longer applies once the actual energy was metered.
_ENERGY_MEASURED_KEYS = (
    "energy_class",
    "batching",
    "measurement_bias",
    "unbatched_local_inference",
)


def _to_decimal(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def adjust_contributions(contributions: list[dict], evidence: Evidence) -> list[dict]:
    """New contribution rows, narrowed per `evidence`; `contributions` and
    its rows are never mutated. Each row this touches gets an
    `"evidence": "<flag name>"` key naming which flag narrowed it; untouched
    rows are returned unchanged (as new dicts, still copies)."""
    out: list[dict] = []
    for row in contributions:
        new_row = dict(row)
        key = row.get("key")
        if evidence.energy_measured and key in _ENERGY_MEASURED_KEYS:
            new_row["low_multiplier"] = 0.9
            new_row["high_multiplier"] = 1.1
            new_row["note"] = "measured on this run"
            new_row["evidence"] = "energy_measured"
        elif evidence.pue_metered and key == "pue":
            new_row["low_multiplier"] = 0.95
            new_row["high_multiplier"] = 1.05
            new_row["note"] = (
                "PUE metered by a labeled operator layer (workspace/managed/harness), "
                "not the shipped default."
            )
            new_row["evidence"] = "pue_metered"
        elif evidence.grid_sourced_dated and key == "grid_intensity":
            new_row["low_multiplier"] = 0.7
            new_row["high_multiplier"] = 1.3
            new_row["note"] = (
                "Grid factor sourced and dated by a labeled operator layer, not the "
                "annual-average default."
            )
            new_row["evidence"] = "grid_sourced_dated"
        elif evidence.embodied_profiled and key in ("embodied", "embodied_hardware"):
            # No row of this key exists in the shipped `uncertainty_contributions`
            # output today (embodied carbon has no per-factor sensitivity row
            # there) — this is the extension point for when one does. Multipliers
            # are left as-is; only the provenance note changes.
            new_row["note"] = "Embodied figure computed from a named hardware profile."
            new_row["evidence"] = "embodied_profiled"
        out.append(new_row)
    return out


@dataclass(frozen=True)
class DerivedBand:
    low: Decimal  # divisor, >= 1
    high: Decimal  # multiplier, >= 1
    rule: str  # "configured" | "dominant_contribution"
    dominant_key: str | None
    narrowed: bool


def derive_band(
    contributions: list[dict], *, configured_low: Decimal, configured_high: Decimal
) -> DerivedBand:
    """The headline band `contributions` implies, capped at `configured_*`.

    For the low axis: each row's `low_multiplier` (skipped when <= 0) implies
    a divisor `1/low_multiplier`; the largest such divisor across rows is the
    row-implied bound. For the high axis: the largest `high_multiplier`
    across rows is the row-implied bound directly. Each axis's final value is
    `min(configured, row-implied)`, clamped to >= 1 — so evidence can only
    ever pull an axis tighter than configured, never push it wider. Ties
    within an axis keep the first row in `contributions` order.

    An axis narrows below `configured_*` only when the row that set its
    candidate was itself evidence-stamped (`row["evidence"]`, set by
    `adjust_contributions` for a row an `Evidence` flag actually touched) —
    otherwise the axis stays at the configured value, even if some untouched
    row's own baseline multiplier happens to imply a tighter bound. Without
    this, `derived: true` narrows a run's band off whichever row's ordinary,
    always-present sensitivity multiplier is smallest, regardless of whether
    this run actually closed out anything — a deliberately conservative
    configured band would then narrow on every run with no evidence at all,
    and a run with only one kind of evidence (energy measured, say) could get
    narrowed on an axis a completely different, unevidenced row (grid
    intensity's own undated default) happens to dominate.

    `rule` is `"dominant_contribution"` when either axis actually landed
    below its configured value, else `"configured"`. `dominant_key` names the
    row that set the tighter of the two axes when both narrowed (the row
    whose contribution left the smaller absolute distance from 1 wins ties
    deterministically); when only one axis narrowed, that axis's row is
    named; when neither did, it is None.
    """
    configured_low = max(_to_decimal(configured_low), Decimal(1))
    configured_high = max(_to_decimal(configured_high), Decimal(1))

    candidate_low: Decimal | None = None
    candidate_low_key: str | None = None
    candidate_low_evidenced = False
    candidate_high: Decimal | None = None
    candidate_high_key: str | None = None
    candidate_high_evidenced = False

    for row in contributions:
        key = row.get("key")
        evidenced = bool(row.get("evidence"))
        low_mult = _to_decimal(row["low_multiplier"])
        high_mult = _to_decimal(row["high_multiplier"])
        if low_mult > 0:
            implied_low = Decimal(1) / low_mult
            if candidate_low is None or implied_low > candidate_low:
                candidate_low = implied_low
                candidate_low_key = key
                candidate_low_evidenced = evidenced
        if candidate_high is None or high_mult > candidate_high:
            candidate_high = high_mult
            candidate_high_key = key
            candidate_high_evidenced = evidenced

    if candidate_low is None or not candidate_low_evidenced:
        candidate_low = configured_low
        candidate_low_key = None
    if candidate_high is None or not candidate_high_evidenced:
        candidate_high = configured_high
        candidate_high_key = None

    low = max(min(configured_low, candidate_low), Decimal(1))
    high = max(min(configured_high, candidate_high), Decimal(1))

    low_narrowed = low < configured_low
    high_narrowed = high < configured_high
    narrowed = low_narrowed or high_narrowed

    dominant_key: str | None = None
    if low_narrowed and high_narrowed:
        # Both axes moved off configured, possibly from different rows.
        # Deterministic tie-break: the axis landing closer to 1 (a tighter
        # constraint) names its row; an exact tie prefers the low axis.
        dominant_key = candidate_low_key if low <= high else candidate_high_key
    elif low_narrowed:
        dominant_key = candidate_low_key
    elif high_narrowed:
        dominant_key = candidate_high_key

    return DerivedBand(
        low=low,
        high=high,
        rule="dominant_contribution" if narrowed else "configured",
        dominant_key=dominant_key,
        narrowed=narrowed,
    )


def band_record(derived: DerivedBand, *, configured_low: Decimal, configured_high: Decimal) -> dict:
    """JSON-serializable provenance for `derived`, for a stored run: the
    resulting band alongside the configured band it was capped against."""
    return {
        "low": float(derived.low),
        "high": float(derived.high),
        "configured_low": float(_to_decimal(configured_low)),
        "configured_high": float(_to_decimal(configured_high)),
        "rule": derived.rule,
        "dominant_key": derived.dominant_key,
        "narrowed": derived.narrowed,
    }
