"""Water consumption from energy. Pure: no DB, network or clock.

Method and sources: docs/water-methodology.md. Constants: data/water_factors.json.

    onsite_ml  = IT energy Wh       * site WUE (L/kWh)
    offsite_ml = facility energy Wh * grid water (L/kWh)

Wh x L/kWh = mL. A missing energy figure gives None, never 0.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

SCHEMA_VERSION = 1
WATER_BASIS = "consumption"

# Same string values as emissions.py DEPLOYMENT_CLOUD / DEPLOYMENT_LOCAL.
DEPLOYMENT_CLOUD = "cloud"
DEPLOYMENT_LOCAL = "local"

_DATA = Path(__file__).resolve().parent.parent / "data" / "water_factors.json"
_PLACES = 6

# Layers a factor can resolve from, highest precedence first.
LAYER_OVERRIDE = "override"
LAYER_DATASET = "dataset"
LAYER_DEFAULT = "global_default"

_OVERRIDE_KEYS = (
    "site_wue_l_per_kwh",
    "grid_water_l_per_kwh",
    "band_low",
    "band_high",
    "embodied_water_ml_per_run",
)

_HYDRO_CAVEAT = (
    "Grid water factor includes hydropower reservoir evaporation, with 100% allocated "
    "to electricity (hydro_evaporation: included_full_allocation); it cannot be subtracted out."
)
_UNDERSTATE_CAVEAT = (
    "Energy boundary covers only part of the node (gpu/partial); water is understated, as carbon is."
)
_FACILITY_CAVEAT = "IT energy derived from configured PUE"


@dataclass(frozen=True)
class WaterFactors:
    site_wue_l_per_kwh: float
    grid_water_l_per_kwh: float
    band_low: float
    band_high: float
    embodied_water_ml_per_run: float | None = None  # None = not counted
    water_basis: str = WATER_BASIS
    hydro_included: bool = True  # grid factor came from WRI
    records: tuple[dict, ...] = field(default_factory=tuple)
    caveats: tuple[str, ...] = field(default_factory=tuple)


def _f(value: Decimal | float | int, places: int = _PLACES) -> float:
    """JSON-safe rounded float."""
    return float(round(Decimal(str(value)), places))


def _d(value: Decimal | float | int | str) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


@lru_cache(maxsize=1)
def _load() -> dict:
    with _DATA.open(encoding="utf-8") as fh:
        return json.load(fh)


def _record(src: dict, *, layer: str, source: str | None = None, value=None, note: str | None = None, setting=None) -> dict:
    """Provenance record: same keys as emissions._factor, plus water_basis and layer."""
    return {
        "key": src["key"],
        "label": src["label"],
        "value": src["value"] if value is None else value,
        "unit": src.get("unit"),
        "source": source or src["source"],
        "url": src.get("url"),
        "date": src.get("date"),
        "confidence": src["confidence"],
        "note": note or src.get("note", ""),
        "setting": setting,
        "water_basis": src.get("water_basis", WATER_BASIS),
        "layer": layer,
    }


def _check_number(name: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError(f"{name} must be a number, got {value!r}")
    num = float(value)
    if not math.isfinite(num) or num < 0:
        raise ValueError(f"{name} must be a non-negative finite number, got {value!r}")
    return num


def _validate_overrides(overrides: dict) -> dict[str, float]:
    unknown = set(overrides) - set(_OVERRIDE_KEYS)
    if unknown:
        raise ValueError(f"unknown water override(s): {sorted(unknown)}")
    out = {k: _check_number(k, v) for k, v in overrides.items() if v is not None}
    if "band_low" in out and not 0 < out["band_low"] <= 1:
        raise ValueError(f"band_low must be in (0, 1], got {out['band_low']!r}")
    if "band_high" in out and out["band_high"] < 1:
        raise ValueError(f"band_high must be >= 1, got {out['band_high']!r}")
    return out


def default_water_factors(
    deployment: str,
    *,
    country_iso3: str | None = None,
    overrides: dict | None = None,
) -> WaterFactors:
    """Resolve factors. Precedence: overrides > country table > shipped default."""
    if deployment not in (DEPLOYMENT_CLOUD, DEPLOYMENT_LOCAL):
        raise ValueError(f"unknown deployment {deployment!r}")
    data = _load()
    ov = _validate_overrides(overrides or {})
    caveats: list[str] = []
    records: list[dict] = []

    # site WUE
    wue_src = data["site_wue"][deployment]
    if "site_wue_l_per_kwh" in ov:
        wue = ov["site_wue_l_per_kwh"]
        records.append(_record(wue_src, layer=LAYER_OVERRIDE, source="override", value=wue,
                               note="Operator-supplied value.", setting="site_wue_l_per_kwh"))
    else:
        wue = float(wue_src["value"])
        records.append(_record(wue_src, layer=LAYER_DEFAULT))

    # grid water
    grid_src = data["grid_water"]
    hydro = True
    if "grid_water_l_per_kwh" in ov:
        grid = ov["grid_water_l_per_kwh"]
        hydro = False
        records.append(_record(grid_src, layer=LAYER_OVERRIDE, source="override", value=grid,
                               note="Operator-supplied value.", setting="grid_water_l_per_kwh"))
    else:
        iso = country_iso3.strip().upper() if country_iso3 else None
        row = grid_src["countries"].get(iso) if iso else None
        if row is not None:
            grid = float(row["l_per_kwh"])
            records.append(_record(
                grid_src, layer=LAYER_DATASET, source=f"dataset:wri2020:{iso}", value=grid,
                note=f"{row['name']}: {row['gal_per_kwh']} gal/kWh x {grid_src['gal_to_l']} L/gal.",
                setting=iso,
            ))
        else:
            grid = float(grid_src["value"])
            records.append(_record(grid_src, layer=LAYER_DEFAULT))
            if iso:
                caveats.append(f"No WRI 2020 water factor for {iso} in tret's table; used the world average.")

    # band
    band_src = data["band"]
    low = ov.get("band_low", float(band_src["low"]))
    high = ov.get("band_high", float(band_src["high"]))
    band_overridden = "band_low" in ov or "band_high" in ov
    band_rec = _record(band_src, layer=LAYER_OVERRIDE if band_overridden else LAYER_DEFAULT,
                       value={"low": low, "high": high}, source="override" if band_overridden else None)
    band_rec["is_confidence_interval"] = False
    records.append(band_rec)

    embodied = ov.get("embodied_water_ml_per_run")
    if embodied is not None:
        records.append({
            "key": "embodied_water_ml_per_run", "label": "Embodied water", "value": embodied,
            "unit": "mL/run", "source": "override", "url": None, "date": None,
            "confidence": "low", "note": "Operator-supplied value.",
            "setting": "embodied_water_ml_per_run", "water_basis": WATER_BASIS, "layer": LAYER_OVERRIDE,
        })

    return WaterFactors(
        site_wue_l_per_kwh=wue,
        grid_water_l_per_kwh=grid,
        band_low=low,
        band_high=high,
        embodied_water_ml_per_run=embodied,
        water_basis=data["water_basis"],
        hydro_included=hydro,
        records=tuple(records),
        caveats=tuple(caveats),
    )


def _onsite_it_wh(energy_wh, energy_wh_total, boundary: str, pue) -> tuple[Decimal, bool]:
    """IT energy the site WUE applies to, and whether it was derived from PUE."""
    if boundary == "facility":
        p = _d(pue)
        if p <= 0:
            raise ValueError("pue must be positive for the facility boundary")
        return _d(energy_wh_total) / p, True
    return _d(energy_wh), False


def _split(energy_wh, energy_wh_total, boundary, pue, f: WaterFactors):
    it, derived = _onsite_it_wh(energy_wh, energy_wh_total, boundary, pue)
    onsite = it * _d(f.site_wue_l_per_kwh)
    offsite = _d(energy_wh_total) * _d(f.grid_water_l_per_kwh)
    embodied = _d(f.embodied_water_ml_per_run) if f.embodied_water_ml_per_run is not None else None
    total = onsite + offsite + (embodied or Decimal(0))
    return onsite, offsite, embodied, total, derived


def compute_water(
    energy_wh,
    energy_wh_total,
    *,
    boundary: str,
    pue,
    factors: WaterFactors,
    baseline_energy_wh=None,
    baseline_energy_wh_total=None,
    baseline_factors: WaterFactors | None = None,
) -> dict | None:
    """The `water` block for one run or segment; None when energy is unknown."""
    if energy_wh is None or energy_wh_total is None:
        return None
    onsite, offsite, embodied, total, derived = _split(energy_wh, energy_wh_total, boundary, pue, factors)

    baseline = avoided = None
    if baseline_energy_wh is not None and baseline_energy_wh_total is not None:
        bf = baseline_factors or factors
        # The baseline is always a token estimate (node IT), whatever meter the run had.
        b_total = _split(baseline_energy_wh, baseline_energy_wh_total, "node_it", pue, bf)[3]
        baseline = _f(b_total)
        avoided = _f(b_total - total)  # signed, never clamped

    caveats: list[str] = list(factors.caveats)
    if derived:
        caveats.append(_FACILITY_CAVEAT)
    if boundary in ("gpu", "partial"):
        caveats.append(_UNDERSTATE_CAVEAT)
    if factors.hydro_included:
        caveats.append(_HYDRO_CAVEAT)
    caveats = list(dict.fromkeys(caveats))

    return {
        "schema_version": SCHEMA_VERSION,
        "water_basis": factors.water_basis,
        "water_ml": _f(total),
        "onsite_ml": _f(onsite),
        "offsite_ml": _f(offsite),
        "embodied_ml": _f(embodied) if embodied is not None else None,
        "water_ml_low": _f(total * _d(factors.band_low)),
        "water_ml_high": _f(total * _d(factors.band_high)),
        "baseline_water_ml": baseline,
        "avoided_water_ml": avoided,
        "factors": [dict(r) for r in factors.records],
        "caveats": caveats,
    }


_SUM_KEYS = ("water_ml", "onsite_ml", "offsite_ml", "water_ml_low", "water_ml_high")


def combine_water(blocks) -> dict | None:
    """Roll up water blocks across runs/segments. None entries are counted, not summed."""
    blocks = list(blocks)
    if not blocks:
        return None
    present = [b for b in blocks if b is not None]
    missing = len(blocks) - len(present)
    if not present:
        return None

    bases = {b.get("water_basis") for b in present}
    caveats: list[str] = []
    for b in present:
        caveats.extend(b.get("caveats") or [])
    mixed = len(bases) > 1
    if mixed:
        caveats.append(f"Water bases differ across runs ({', '.join(sorted(map(str, bases)))}); water not summed.")

    def total(key: str, *, all_or_none: bool = False):
        vals = [b.get(key) for b in present if b.get(key) is not None]
        if mixed or not vals or (all_or_none and len(vals) < len(present)):
            return None
        return _f(sum((_d(v) for v in vals), Decimal(0)))

    # A baseline summed over only some runs would not compare with water_ml.
    no_baseline = sum(1 for b in present if b.get("baseline_water_ml") is None)
    if no_baseline and no_baseline < len(present):
        caveats.append(f"{no_baseline} run(s) have no baseline; baseline and avoided water not summed.")

    out = {
        "schema_version": SCHEMA_VERSION,
        "water_basis": None if mixed else next(iter(bases)),
        **{k: total(k) for k in _SUM_KEYS},
        "embodied_ml": total("embodied_ml"),
        "baseline_water_ml": total("baseline_water_ml", all_or_none=True),
        "avoided_water_ml": total("avoided_water_ml", all_or_none=True),
        "runs_counted": len(present),
        "runs_without_water": missing,
        "caveats": list(dict.fromkeys(caveats)),
    }
    if missing:
        out["caveats"].append(f"{missing} run(s) have no water figure and are not counted.")
    return out
