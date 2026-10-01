"""Water consumption from energy. Pure: no DB, network or clock.

Method and sources: docs/water-methodology.md. Constants: data/water_factors.json.

    onsite_ml  = IT energy Wh       * site WUE (L/kWh)
    offsite_ml = facility energy Wh * grid water (L/kWh)

Wh x L/kWh = mL. A missing energy figure gives None, never 0.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
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
    records: tuple[dict, ...] = field(default_factory=tuple, hash=False)
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


def _record(
    src: dict, *, layer: str, source: str | None = None, value=None, note: str | None = None,
    setting=None, url: str | None = None, date: str | None = None, label: str | None = None,
    disclosure: dict | None = None,
) -> dict:
    """Provenance record: same keys as emissions._factor, plus water_basis and layer.

    `url`/`date`/`label`/`disclosure` are set only for an upstream's own
    disclosure (a per-hosting-provider WUE), which carries its own citation.
    """
    record = {
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
    if url is not None:
        record["url"] = url
    if date is not None:
        record["date"] = date
    if label is not None:
        record["label"] = label
    if disclosure is not None:
        record["disclosure"] = disclosure
        record["water_basis"] = disclosure.get("water_basis", record["water_basis"])
    return record


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


@dataclass(frozen=True)
class WaterInput:
    """One already-resolved override value and where the ladder found it.

    `layer`/`source` are recorded verbatim on the factor's provenance record
    (`workspace`, `managed:<name>`, `env`, `run_override`, ...); `setting` is
    the dotted path or env var that applied.
    """

    value: float
    layer: str
    source: str
    setting: str | None = None
    # Set only for an upstream's own disclosure (`water.upstreams.<key>`).
    url: str | None = None
    as_of: str | None = None
    label: str | None = None
    disclosure: dict | None = field(default=None, hash=False, compare=False)


# Most specific first; the legacy "override" layer (default_water_factors) leads.
_LAYER_ORDER = (
    LAYER_OVERRIDE, "run_override", "harness", "workspace", "managed", "env",
    LAYER_DATASET, LAYER_DEFAULT,
)


def _layer_rank(layer: str) -> int:
    return _LAYER_ORDER.index(layer) if layer in _LAYER_ORDER else len(_LAYER_ORDER)


def _checked(inp: WaterInput | None, name: str) -> WaterInput | None:
    if inp is None:
        return None
    return replace(inp, value=_check_number(name, inp.value))


def resolve_water_factors(
    deployment: str,
    *,
    site_wue: WaterInput | None = None,
    grid_water: WaterInput | None = None,
    band_low: WaterInput | None = None,
    band_high: WaterInput | None = None,
    embodied: WaterInput | None = None,
    country_iso3: str | None = None,
    country_setting: str | None = None,
) -> WaterFactors:
    """Resolve factors from per-key values the caller already walked the ladder for.

    A key left `None` falls to the dataset rung (grid water, when `country_iso3`
    names a country in the WRI table) and then the shipped default.
    """
    if deployment not in (DEPLOYMENT_CLOUD, DEPLOYMENT_LOCAL):
        raise ValueError(f"unknown deployment {deployment!r}")
    data = _load()
    site_wue = _checked(site_wue, "site_wue_l_per_kwh")
    grid_water = _checked(grid_water, "grid_water_l_per_kwh")
    band_low = _checked(band_low, "band_low")
    band_high = _checked(band_high, "band_high")
    embodied = _checked(embodied, "embodied_water_ml_per_run")
    if band_low is not None and not 0 < band_low.value <= 1:
        raise ValueError(f"band_low must be in (0, 1], got {band_low.value!r}")
    if band_high is not None and band_high.value < 1:
        raise ValueError(f"band_high must be >= 1, got {band_high.value!r}")
    caveats: list[str] = []
    records: list[dict] = []

    # site WUE
    wue_src = data["site_wue"][deployment]
    if site_wue is not None:
        wue = site_wue.value
        records.append(_record(wue_src, layer=site_wue.layer, source=site_wue.source, value=wue,
                               note="Operator-supplied value.", setting=site_wue.setting,
                               url=site_wue.url, date=site_wue.as_of, label=site_wue.label,
                               disclosure=site_wue.disclosure))
    else:
        wue = float(wue_src["value"])
        records.append(_record(wue_src, layer=LAYER_DEFAULT))

    # grid water
    grid_src = data["grid_water"]
    hydro = True
    if grid_water is not None:
        grid = grid_water.value
        hydro = False
        records.append(_record(grid_src, layer=grid_water.layer, source=grid_water.source, value=grid,
                               note="Operator-supplied value.", setting=grid_water.setting))
    else:
        iso = country_iso3.strip().upper() if country_iso3 else None
        row = grid_src["countries"].get(iso) if iso else None
        if row is not None:
            grid = float(row["l_per_kwh"])
            records.append(_record(
                grid_src, layer=LAYER_DATASET, source=f"dataset:wri2020:{iso}", value=grid,
                note=f"{row['name']}: {row['gal_per_kwh']} gal/kWh x {grid_src['gal_to_l']} L/gal.",
                setting=country_setting or iso,
            ))
        else:
            grid = float(grid_src["value"])
            records.append(_record(grid_src, layer=LAYER_DEFAULT))
            if iso:
                caveats.append(f"No WRI 2020 water factor for {iso} in tret's table; used the world average.")

    # band
    band_src = data["band"]
    low = band_low.value if band_low is not None else float(band_src["low"])
    high = band_high.value if band_high is not None else float(band_src["high"])
    won = [b for b in (band_low, band_high) if b is not None]
    if won:
        top = min(won, key=lambda b: _layer_rank(b.layer))
        settings = list(dict.fromkeys(b.setting for b in won if b.setting))
        band_rec = _record(band_src, layer=top.layer, value={"low": low, "high": high},
                           source=top.source, setting="; ".join(settings) or None)
    else:
        band_rec = _record(band_src, layer=LAYER_DEFAULT, value={"low": low, "high": high})
    band_rec["is_confidence_interval"] = False
    records.append(band_rec)

    embodied_value = embodied.value if embodied is not None else None
    if embodied is not None:
        records.append({
            "key": "embodied_water_ml_per_run", "label": "Embodied water", "value": embodied_value,
            "unit": "mL/run", "source": embodied.source, "url": None, "date": None,
            "confidence": "low", "note": "Operator-supplied value.",
            "setting": embodied.setting, "water_basis": WATER_BASIS, "layer": embodied.layer,
        })

    return WaterFactors(
        site_wue_l_per_kwh=wue,
        grid_water_l_per_kwh=grid,
        band_low=low,
        band_high=high,
        embodied_water_ml_per_run=embodied_value,
        water_basis=data["water_basis"],
        hydro_included=hydro,
        records=tuple(records),
        caveats=tuple(caveats),
    )


def default_water_factors(
    deployment: str,
    *,
    country_iso3: str | None = None,
    overrides: dict | None = None,
) -> WaterFactors:
    """Resolve factors. Precedence: overrides > country table > shipped default."""
    if deployment not in (DEPLOYMENT_CLOUD, DEPLOYMENT_LOCAL):
        raise ValueError(f"unknown deployment {deployment!r}")
    ov = _validate_overrides(overrides or {})

    def _inp(key: str) -> WaterInput | None:
        return WaterInput(ov[key], LAYER_OVERRIDE, "override", key) if key in ov else None

    return resolve_water_factors(
        deployment,
        site_wue=_inp("site_wue_l_per_kwh"),
        grid_water=_inp("grid_water_l_per_kwh"),
        band_low=_inp("band_low"),
        band_high=_inp("band_high"),
        embodied=_inp("embodied_water_ml_per_run"),
        country_iso3=country_iso3,
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
        avoided = _f(b_total - total) + 0.0  # signed, never clamped (+0.0 folds -0.0)

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
        # Same shape as a combine_water() roll-up, so one run and many look alike.
        "runs_counted": 1,
        "runs_without_water": 0,
    }


_SUM_KEYS = ("water_ml", "onsite_ml", "offsite_ml", "water_ml_low", "water_ml_high")


def combine_water(blocks) -> dict | None:
    """Roll up water blocks across runs/segments. None entries are counted, not summed."""
    blocks = list(blocks)
    if not blocks:
        return None
    present = [b for b in blocks if b is not None]
    # An already-combined block may itself cover runs without water: count them.
    inner_missing = sum(int(b.get("runs_without_water") or 0) for b in present)
    counted = sum(int(b.get("runs_counted", 1)) for b in present)
    missing = len(blocks) - len(present) + inner_missing
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
        "runs_counted": counted,
        "runs_without_water": missing,
        "caveats": list(dict.fromkeys(caveats)),
    }
    if missing:
        out["caveats"].append(f"{missing} run(s) have no water figure and are not counted.")
    return out
