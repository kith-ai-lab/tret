"""Evidence-responsive narrowing of the headline uncertainty band: pure,
offline. The band must only ever get tighter than the configured 2.5/2.5, and
contributions are never multiplied together (that stance is inherited from
`tret.services.emissions.uncertainty_contributions`, whose docstring explains
why) — this module only ever picks `min(configured, row-implied)` per axis,
and only ever picks a row that was itself evidence-stamped by
`adjust_contributions` — an axis whose tightest row is an ordinary, untouched
baseline multiplier (nobody's evidence) stays at the configured value."""
from __future__ import annotations

import copy
import json
from decimal import Decimal

import pytest

from tret.services.emissions import uncertainty_contributions
from tret.services.uncertainty_derivation import (
    DerivedBand,
    Evidence,
    adjust_contributions,
    band_record,
    derive_band,
)

CONFIGURED_LOW = Decimal("2.5")
CONFIGURED_HIGH = Decimal("2.5")


def cloud_rows() -> list[dict]:
    return uncertainty_contributions(reasoning_tier=False, deployment="cloud")


def local_rows() -> list[dict]:
    return uncertainty_contributions(reasoning_tier=False, deployment="local")


# ── no evidence: band equals configured, never wider ─────────────────────────
def test_no_evidence_band_equals_configured_cloud():
    band = derive_band(cloud_rows(), configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    assert band.low == CONFIGURED_LOW
    assert band.high == CONFIGURED_HIGH
    assert band.rule == "configured"
    assert band.dominant_key is None
    assert band.narrowed is False


def test_no_evidence_band_equals_configured_local():
    # unbatched_local_inference's 5.0 high_multiplier is wider than configured
    # (as is grid_intensity's implied ~16.7 low) yet the band still does not
    # widen past 2.5/2.5.
    band = derive_band(local_rows(), configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    assert band.low == CONFIGURED_LOW
    assert band.high == CONFIGURED_HIGH
    assert band.rule == "configured"


def test_grid_intensitys_own_low_multiplier_never_widens_the_band():
    band = derive_band(cloud_rows(), configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    # grid_intensity's low_multiplier of 0.06 implies a divisor of ~16.7 —
    # nowhere near the band, which stays capped at the configured 2.5.
    assert band.low == Decimal("2.5")


# ── energy_measured alone: never narrows off an unevidenced row ──────────────
def test_energy_measured_alone_does_not_narrow_on_an_unevidenced_grid_row():
    rows = adjust_contributions(cloud_rows(), Evidence(energy_measured=True))
    band = derive_band(rows, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    # energy_class, batching, measurement_bias all narrowed, but grid_intensity
    # was not touched by this evidence: its own low_multiplier (0.06) still
    # implies a divisor far past configured, so the low axis stays exactly at
    # configured...
    assert band.low == CONFIGURED_LOW
    # ...and although grid_intensity's own untouched high_multiplier (1.6)
    # would undercut configured, it was never evidence-stamped — only energy
    # evidence was supplied, and it never touches grid_intensity — so the high
    # axis must not narrow off it either: the axis stays at configured.
    assert band.high == CONFIGURED_HIGH
    assert band.rule == "configured"
    assert band.dominant_key is None
    assert band.narrowed is False


def test_energy_measured_alone_on_local_deployment_also_stays_at_configured():
    rows = adjust_contributions(local_rows(), Evidence(energy_measured=True))
    band = derive_band(rows, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    # unbatched_local_inference is narrowed by energy_measured too, but
    # grid_intensity (still the largest remaining high_multiplier) is not
    # evidenced here either, so the high axis stays at configured exactly as
    # in the cloud case.
    assert band.high == CONFIGURED_HIGH
    assert band.dominant_key is None


# ── fully evidenced: narrows to ~1.3-1.4, grid dominant on both axes ─────────
def test_fully_evidenced_narrows_to_grid_intensity_band():
    evidence = Evidence(energy_measured=True, grid_sourced_dated=True, pue_metered=True)
    rows = adjust_contributions(cloud_rows(), evidence)
    band = derive_band(rows, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    assert band.rule == "dominant_contribution"
    assert band.dominant_key == "grid_intensity"
    assert band.narrowed is True
    # grid_intensity's evidenced 0.7/1.3 is now the tightest row on both axes:
    # low = 1/0.7 = 10/7, high = 1.3 exactly.
    assert band.low == Decimal(1) / Decimal("0.7")
    assert Decimal("1.42") < band.low < Decimal("1.43")
    assert band.high == Decimal("1.3")


def test_fully_evidenced_band_is_narrower_than_energy_measured_alone():
    partial = derive_band(
        adjust_contributions(cloud_rows(), Evidence(energy_measured=True)),
        configured_low=CONFIGURED_LOW,
        configured_high=CONFIGURED_HIGH,
    )
    full = derive_band(
        adjust_contributions(
            cloud_rows(),
            Evidence(energy_measured=True, grid_sourced_dated=True, pue_metered=True),
        ),
        configured_low=CONFIGURED_LOW,
        configured_high=CONFIGURED_HIGH,
    )
    assert full.high < partial.high
    assert full.low < partial.low


# ── adjust_contributions: never mutates, stamps evidence ─────────────────────
def test_adjust_contributions_never_mutates_input():
    rows = cloud_rows()
    before = copy.deepcopy(rows)
    adjust_contributions(rows, Evidence(energy_measured=True, pue_metered=True))
    assert rows == before


def test_adjust_contributions_returns_new_dict_objects():
    rows = cloud_rows()
    adjusted = adjust_contributions(rows, Evidence())
    for original, new in zip(rows, adjusted):
        assert new is not original
        assert new == original  # no evidence -> untouched content, but a copy


def test_adjust_contributions_stamps_evidence_field():
    rows = adjust_contributions(cloud_rows(), Evidence(pue_metered=True))
    by_key = {row["key"]: row for row in rows}
    assert by_key["pue"]["evidence"] == "pue_metered"
    assert by_key["pue"]["low_multiplier"] == 0.95
    assert by_key["pue"]["high_multiplier"] == 1.05
    # untouched rows carry no evidence stamp
    assert "evidence" not in by_key["grid_intensity"]


def test_adjust_contributions_grid_sourced_dated_narrows_grid_row_only():
    rows = adjust_contributions(cloud_rows(), Evidence(grid_sourced_dated=True))
    by_key = {row["key"]: row for row in rows}
    assert by_key["grid_intensity"]["low_multiplier"] == 0.7
    assert by_key["grid_intensity"]["high_multiplier"] == 1.3
    assert by_key["grid_intensity"]["evidence"] == "grid_sourced_dated"
    assert "evidence" not in by_key["energy_class"]


def test_adjust_contributions_energy_measured_narrows_local_only_row_too():
    rows = adjust_contributions(local_rows(), Evidence(energy_measured=True))
    by_key = {row["key"]: row for row in rows}
    assert by_key["unbatched_local_inference"]["low_multiplier"] == 0.9
    assert by_key["unbatched_local_inference"]["high_multiplier"] == 1.1
    assert by_key["unbatched_local_inference"]["evidence"] == "energy_measured"


def test_adjust_contributions_embodied_profiled_notes_a_synthetic_row():
    # uncertainty_contributions ships no "embodied" row today; this exercises
    # the extension point against a synthetic contributions list.
    synthetic = [
        {"key": "embodied", "label": "x", "low_multiplier": 0.5, "high_multiplier": 2.0}
    ]
    rows = adjust_contributions(synthetic, Evidence(embodied_profiled=True))
    assert rows[0]["low_multiplier"] == 0.5
    assert rows[0]["high_multiplier"] == 2.0
    assert rows[0]["evidence"] == "embodied_profiled"
    assert "note" in rows[0]


def test_adjust_contributions_no_evidence_touches_nothing():
    rows = adjust_contributions(cloud_rows(), Evidence())
    assert all("evidence" not in row for row in rows)


# ── clamping at 1 ──────────────────────────────────────────────────────────────
def test_derive_band_clamps_configured_values_below_one():
    band = derive_band([], configured_low=Decimal("0.5"), configured_high=Decimal("0.5"))
    assert band.low == Decimal(1)
    assert band.high == Decimal(1)
    assert band.rule == "configured"
    assert band.narrowed is False


def test_derive_band_empty_contributions_is_configured():
    band = derive_band([], configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    assert band.low == CONFIGURED_LOW
    assert band.high == CONFIGURED_HIGH
    assert band.rule == "configured"
    assert band.dominant_key is None


def test_derive_band_ignores_rows_with_nonpositive_low_multiplier_for_low_axis():
    rows = [
        # Evidence-stamped so the high axis is actually allowed to narrow off
        # it (see the evidence-gating tests above) — this test's own point is
        # the low axis, not the gate.
        {"key": "x", "low_multiplier": 0, "high_multiplier": 1.5, "evidence": "test"},
        {"key": "y", "low_multiplier": -1, "high_multiplier": 1.2, "evidence": "test"},
    ]
    band = derive_band(rows, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    # neither row contributes to the low axis (both <= 0), so it stays at
    # the configured value with no row credited.
    assert band.low == CONFIGURED_LOW
    # the high axis still narrows off the largest high_multiplier (1.5, row x)
    assert band.high == Decimal("1.5")
    assert band.dominant_key == "x"
    assert band.rule == "dominant_contribution"


# ── DerivedBand / band_record ─────────────────────────────────────────────────
def test_band_record_is_json_serializable():
    band = derive_band(cloud_rows(), configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    record = band_record(band, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    encoded = json.dumps(record)
    assert json.loads(encoded) == record
    assert record["rule"] == "configured"
    assert record["configured_low"] == 2.5
    assert record["configured_high"] == 2.5


def test_band_record_fully_evidenced_is_json_serializable_and_narrowed():
    evidence = Evidence(energy_measured=True, grid_sourced_dated=True, pue_metered=True)
    rows = adjust_contributions(cloud_rows(), evidence)
    band = derive_band(rows, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    record = band_record(band, configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    json.dumps(record)  # raises if not serializable
    assert record["narrowed"] is True
    assert record["dominant_key"] == "grid_intensity"
    assert 1.3 <= record["low"] < 1.5
    assert record["high"] == 1.3


def test_derived_band_is_a_frozen_dataclass():
    band = derive_band(cloud_rows(), configured_low=CONFIGURED_LOW, configured_high=CONFIGURED_HIGH)
    assert isinstance(band, DerivedBand)
    with pytest.raises(Exception):
        band.low = Decimal(1)  # type: ignore[misc]
