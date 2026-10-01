"""Emissions method v3 ("facility_v3"): a pure, parallel-run calculator.

Source: Alex Bickley's method lab (Kith), `methodology/decisions.md`
2026-10-01 (decisions R-1..R-18, C1..C10), pinned to core a0b831b. Every
constant lives in `data/calibration/facility_v3.json`; this module holds only
the chain and its bookkeeping. It imports nothing from `services/emissions.py`
and touches no database, network or clock, so it can run beside the current
method (`class_ladder_v2`) without changing any existing figure.

The chain: weighted tokens -> rate placement ladder -> node Wh -> reasoning
uplift -> fleet x PUE (facility Wh) -> grid (location-based, T&D grossed up)
+ embodied + router overhead -> a log-RSS judgment band -> stamped shadows.

Readings chosen where the spec is ambiguous (all reproduce Alex's goldens):

* `deployment_factor_D` is in the band for every curve-derived rate, i.e.
  rungs 2-4 including the classification-table rows (their rates already
  carry D). Including it reproduces the golden envelope (251 g) exactly;
  leaving it out gives 250.1 g.
* A metered self-host is rung 1 with `node_wh = metered_energy_wh`: no host
  uplift, no production step, no reasoning uplift (the meter already holds the
  thinking energy), no hidden-count hedge, no fleet/PUE *band* factors. The
  self-hosted PUE (1.05 workstation / 1.54 on-prem) still multiplies, because
  the meter is the node, not the facility.
* Effective provider = serving provider if named, else the cloud of the
  region pin, else the model maker. A Claude model served by Bedrock with no
  pin therefore lands on the AWS fleet rungs (PUE 1.14), but its grid rung
  keys on the model maker (anthropic -> USA, openai -> World) because the
  reseller cloud has no geography rule of its own.
* Azure geography for `pue_microsoft_geo` is derived from the region's
  country (data file `azure_geography_by_iso3`).
* Band eff values are clamped to the side they belong to (low <= 1 <= high)
  before combining; this never moves a golden.
* `rate_bucket` (optional shadow in the spec) is not stamped.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

METHOD_ID = "facility_v3"
METHOD_VERSION = "3.0.0-preview"
CALIBRATION_ID = "alex_method_lab_2026-10-01"
PIN = "a0b831b"
BOUNDARY = "facility"
REGIME_CLOUD = "ML.ENERGY v3 chat, H100, max_num_seqs 64 base × production step 1.18"
REGIME_METERED = "metered self-host"
TRAINING_DISCLOSURE = (
    "Training excluded from the total; held proxy share for closed models "
    "5–83% (watershed2026), central 32–56%."
)
COMPARATIVE_WORDING = (
    "Comparison against another model is a 'comparative difference', never "
    "'avoided': it is the gap between two modelled figures, not an offset."
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_SUFFIXES = (":batch", ":free", ":thinking")
_TIER_LABELS = {1: "measured for this case", 2: "median of measurements under a stated assumption",
                3: "midpoint of a judgement range"}

DEPLOYMENTS = ("cloud", "self_hosted_workstation", "self_hosted_onprem")


# ── inputs ───────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class V3Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    # Informational: where exposed, reasoning tokens are already inside `output` (D1.2).
    reasoning: int | None = None


@dataclass(frozen=True)
class V3Inputs:
    model_id: str                              # OpenRouter-style, e.g. "anthropic/claude-sonnet-5"
    maker: str | None = None                   # model maker ("anthropic", "openai", ...)
    serving_provider: str | None = None        # "Amazon Bedrock", "Google Vertex", "Azure", "Together", ...
    deployment: str = "cloud"                  # cloud | self_hosted_workstation | self_hosted_onprem
    metered_energy_wh: float | None = None
    total_params_b: float | None = None
    active_params_b: float | None = None       # None -> dense (active = total)
    precision_bits: int | None = None          # None -> closed / unknown (R-11 midpoint)
    params_exact: bool = False                 # published exact size -> rung 2, else rung 3
    cost_tier: str | None = None               # local | economy | standard | premium
    tokens: V3Tokens = field(default_factory=V3Tokens)
    reasoning_mode: bool | None = None         # the run's own record
    reasoning_default_enabled: bool | None = None  # OpenRouter model metadata
    reasoning_count_hidden: bool = False
    region_pin: tuple[str, str] | None = None  # ("aws" | "gcp" | "azure", region code)
    openrouter_data_region: str | None = None  # None | "us" | "europe"
    run_duration_s: float | None = None
    router_overhead_g: float = 0.0


# ── data ─────────────────────────────────────────────────────────────────────
@lru_cache(maxsize=1)
def load_calibration() -> dict[str, Any]:
    with (_DATA_DIR / "calibration" / "facility_v3.json").open() as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _ember() -> dict[str, Any]:
    with (_DATA_DIR / "grid_ember_2025.json").open() as fh:
        return json.load(fh)


# Direct-provider id prefixes -> the OpenRouter maker that table rows use.
_MAKER_ALIASES = {"kimi": "moonshotai"}
_HYPHEN_VERSION = re.compile(r"-(\d+)-(\d+)$")


def canonical_model_id(model_id: str) -> str:
    """Spell a tret/direct-API id the way the classification table does.

    Strips a leading `openrouter/`, maps a direct-provider maker prefix to its
    OpenRouter maker (`kimi/` -> `moonshotai/`), and turns a trailing hyphenated
    version into a dotted one (`claude-haiku-4-5` -> `claude-haiku-4.5`). Any
    `:batch`/`:free`/`:thinking` suffix is kept as it was.
    """
    mid = model_id[len("openrouter/"):] if model_id.startswith("openrouter/") else model_id
    base, sep, suffix = mid.partition(":")
    maker, slash, name = base.partition("/")
    if slash:
        maker = _MAKER_ALIASES.get(maker, maker)
        name = _HYPHEN_VERSION.sub(r"-\1.\2", name)
        base = f"{maker}/{name}"
    return base + sep + suffix


def lookup_classification(model_id: str) -> dict[str, Any] | None:
    """Exact id, then the canonical spelling; each with a :batch/:free/:thinking
    suffix stripped as a fallback."""
    table = load_calibration()["classification"]
    for candidate in dict.fromkeys((model_id, canonical_model_id(model_id))):
        if candidate in table:
            return table[candidate]
        for suffix in _SUFFIXES:
            if candidate.endswith(suffix) and candidate[: -len(suffix)] in table:
                return table[candidate[: -len(suffix)]]
    return None


# ── step 1: weighted tokens (D1.1, R-10) ─────────────────────────────────────
def _weighted(tokens: V3Tokens, w: dict[str, float], hedge_output: int = 0) -> float:
    return (
        w["input"] * tokens.input
        + w["output"] * (tokens.output + hedge_output)
        + w["cache_read"] * tokens.cache_read
        + w["cache_write"] * tokens.cache_write
    )


# ── step 2: rate placement ladder (D2.3, D2.5, C7, R-15) ─────────────────────
def machines(total_b: float, q_bits: int) -> int:
    """Smallest power of two >= 1.2 * total_B * (Q/8) / 80, minimum 1 (C1)."""
    c = load_calibration()["curve"]
    need = c["headroom"] * total_b * (q_bits / 8) / c["gpu_mem_gb"]
    n = 1
    while n < need:
        n *= 2
    return n


def curve_rate_wh_per_mtok(
    total_b: float, active_b: float | None, precision_bits: int | None
) -> tuple[float, float | None]:
    """Live curve (C1, C9, R-11): returns (rate at host 1.43, Q16 shadow rate or None)."""
    c = load_calibration()["curve"]
    active = total_b if active_b is None else active_b
    per_gpu = c["a"] + c["b"] * active

    def j(q: int) -> float:
        return per_gpu * machines(total_b, q)

    if precision_bits is not None:
        joules, q16 = j(precision_bits), None
    else:
        j8, j16 = j(8), j(16)
        joules, q16 = math.sqrt(j8 * j16), j16
    to_rate = (
        c["D"] * load_calibration()["host_uplift"]["table_host"]
        * load_calibration()["production_step"]["value"] * 1e6 / 3600
    )
    return joules * to_rate, (None if q16 is None else q16 * to_rate)


def _place(inp: V3Inputs) -> dict[str, Any]:
    """Pick the rate rung. Returns rung, size_source, flags, rate_1_43 and band hints."""
    cal = load_calibration()
    if inp.metered_energy_wh is not None:
        return {"rung": 1, "size_source": "metered energy (self-host)", "flags": ["metered"],
                "rate_1_43": None, "q16_midpoint": False, "has_D": False, "closed": None,
                "bucket": None}
    row = lookup_classification(inp.model_id)
    if row is not None:
        rung = row["rung"]
        flags = list(row["flags"])
        return {"rung": rung, "size_source": f"classification table (rung {rung})",
                "flags": flags, "rate_1_43": row["rate_wh_per_mtok_at_host_1_43"],
                "q16_midpoint": "precision assumed" in flags and rung >= 2,
                "has_D": rung in (2, 3, 4), "closed": "generic" if rung in (3, 4) else None,
                "bucket": None}
    if inp.total_params_b:
        rate, q16 = curve_rate_wh_per_mtok(inp.total_params_b, inp.active_params_b, inp.precision_bits)
        rung = 2 if inp.params_exact else 3
        flags = ([] if inp.params_exact else ["estimated size"]) + (
            ["precision assumed"] if inp.precision_bits is None else [])
        return {"rung": rung, "size_source": "live curve from model parameters", "flags": flags,
                "rate_1_43": rate, "q16_midpoint": q16 is not None, "has_D": True,
                "closed": "generic" if rung == 3 else None, "bucket": None}
    klass = cal["buckets"]["tier_to_class"].get(inp.cost_tier or "", "M")
    b = cal["buckets"][klass]
    return {"rung": 5, "size_source": f"price tier ({inp.cost_tier or 'unknown'} -> class {klass})",
            "flags": ["class from price"], "rate_1_43": b["median"], "q16_midpoint": False,
            "has_D": False, "closed": "bucket", "bucket": (klass, b)}


# ── providers (D2.1, D3.1, D3.2) ─────────────────────────────────────────────
_PROVIDER_ALIASES = (
    ("bedrock", "aws"), ("amazon", "aws"), ("aws", "aws"),
    ("vertex", "google"), ("google", "google"), ("gemini", "google"),
    ("azure", "microsoft"), ("microsoft", "microsoft"), ("foundry", "microsoft"),
    ("anthropic", "anthropic"), ("openai", "openai"), ("meta", "meta"),
)
_PIN_CLOUD = {"aws": "aws", "gcp": "google", "azure": "microsoft"}


def _canon(name: str | None) -> str | None:
    if not name:
        return None
    low = name.lower()
    for needle, key in _PROVIDER_ALIASES:
        if needle in low:
            return key
    return "other"


def resolve_provider(inp: V3Inputs) -> str:
    """Serving provider, else the pin's cloud, else the model maker; 'other' = specialist."""
    key = _canon(inp.serving_provider)
    if key is None and inp.region_pin:
        key = _PIN_CLOUD.get(inp.region_pin[0].lower())
    if key is None:
        key = _canon(inp.maker)
    return key or "other"


# ── step 4: reasoning (D2.4, C5/R-13) ────────────────────────────────────────
def _reasoning_mode(inp: V3Inputs) -> tuple[bool, str]:
    if inp.reasoning_mode is not None:
        return inp.reasoning_mode, "run"
    if inp.reasoning_default_enabled is True:
        return True, "provider_default"
    return False, "unknown_off"


# ── step 5/6: fleet and PUE ──────────────────────────────────────────────────
def _fleet(inp: V3Inputs, provider: str) -> dict[str, Any]:
    f = load_calibration()["fleet"]
    if inp.deployment != "cloud":
        return {"value": f["self_hosted"]["value"], "range": None, "kind": "self_hosted", "tier": 1}
    if provider in f["hyperscaler_providers"]:
        return {"value": f["hyperscaler"]["value"], "range": f["hyperscaler"]["range"],
                "kind": "hyperscaler", "tier": 3}
    return {"value": f["specialist"]["value"], "range": f["specialist"]["range"],
            "kind": "specialist", "tier": 3}


def _azure_geo(iso3: str | None) -> str:
    geos = load_calibration()["azure_geography_by_iso3"]
    for geo, members in geos.items():
        if not geo.startswith("_") and iso3 in members:
            return geo
    return "Europe, Middle East & Africa"


def _pue(inp: V3Inputs, provider: str) -> dict[str, Any]:
    p = load_calibration()["pue"]
    if inp.deployment == "self_hosted_workstation":
        return {"value": p["self_hosted"]["workstation"], "range": None, "rung": "self_hosted_workstation",
                "label": "assumed (self-hosted workstation)", "tier": 3}
    if inp.deployment == "self_hosted_onprem":
        return {"value": p["self_hosted"]["onprem"], "range": None, "rung": "self_hosted_onprem",
                "label": "assumed (on-prem facility)", "tier": 3}
    pin = inp.region_pin
    if pin:
        cloud, region = pin[0].lower(), pin[1].lower()
        if cloud == "aws" and region in p["pue_aws_region"]:
            return {"value": p["pue_aws_region"][region], "range": None, "rung": "region_specific",
                    "label": f"provider-reported (AWS {region})", "tier": 2}
        if cloud == "azure":
            iso = load_calibration()["region_map"].get(f"azure:{region}")
            if iso:
                geo = _azure_geo(iso)
                return {"value": p["pue_microsoft_geo"][geo], "range": None, "rung": "region_specific",
                        "label": f"provider-reported (Microsoft {geo})", "tier": 2}
    fleet = p["provider_fleet"]
    if provider in fleet:
        return {"value": fleet[provider], "range": None, "rung": "provider_fleet",
                "label": f"provider-reported ({provider} fleet)", "tier": 2}
    if provider == "anthropic":
        a = p["anthropic_unknown_cloud"]
        return {"value": a["value"], "range": a["range"], "rung": "provider_fleet",
                "label": "provider-reported (serving cloud unknown)", "tier": 3}
    ind = p["industry"]
    return {"value": ind["value"], "range": ind["range"], "rung": "industry_average",
            "label": "industry average (non-hyperscaler or unknown host)", "tier": 3}


# ── step 7: grid ladder (D4.2 R-9, D4.4, D4.1 R-14) ──────────────────────────
def _td_multiplier(iso3: str | None) -> tuple[float, str | None]:
    """1/(1 - loss%) for the geography; EU aggregate or a missing country falls back to WLD, flagged."""
    td = load_calibration()["td"]
    if iso3 in td:
        return 1.0 / (1.0 - td[iso3]["loss_pct"] / 100.0), None
    note = "EU aggregate" if iso3 == "EU" else f"{iso3} missing from T&D table"
    return 1.0 / (1.0 - td["WLD"]["loss_pct"] / 100.0), f"{note}: WLD T&D loss used"


def _country_g(iso3: str) -> float | None:
    e = _ember()
    if iso3 == "WLD":
        return float(e["world"]["g_per_kwh"])
    if iso3 == "EU":
        return float(load_calibration()["grid"]["eu_aggregate_g_per_kwh"])
    c = e["countries"].get(iso3)
    return float(c["g_per_kwh"]) if c else None


def _grid(inp: V3Inputs, provider: str) -> dict[str, Any]:
    cal = load_calibration()
    g_cfg = cal["grid"]
    notes: list[str] = []

    def located(rung: str, iso3: str, basis: str, tier: int, band_class: str) -> dict[str, Any] | None:
        g = _country_g(iso3)
        if g is None:
            notes.append(f"{iso3} not in Ember table")
            return None
        mult, td_note = _td_multiplier(iso3)
        if td_note:
            notes.append(td_note)
        return {"rung": rung, "geography": iso3, "g_per_kwh": g, "td_multiplier": mult,
                "td_applied": True, "basis": basis, "tier": tier, "band_class": band_class,
                "notes": notes}

    if inp.region_pin:
        cloud, region = inp.region_pin[0].lower(), inp.region_pin[1].lower()
        iso = cal["region_map"].get(f"{cloud}:{region}")
        if iso:
            out = located("operator_pin", iso, f"operator pin {cloud}:{region} -> {iso}, Ember 2025", 2, "known")
            if out:
                return out
            # The run is known to have been pinned somewhere that has no Ember figure:
            # it is not in the maker's default geography, so fall to World, never
            # to the provider-geography rung.
            if iso:
                notes.append("pinned region country not in Ember table")
                world = located("world", "WLD",
                                f"World average, {g_cfg['world_basis']} "
                                f"(pin {cloud}:{region} -> {iso} has no Ember figure)", 3, "world")
                assert world is not None
                return world
        else:
            notes.append(f"region {cloud}:{region} not in region map")
    if inp.openrouter_data_region in ("us", "europe"):
        iso = g_cfg["data_region"][inp.openrouter_data_region]
        out = located("data_region", iso,
                      f"OpenRouter data_region={inp.openrouter_data_region} (continent-level), Ember 2025",
                      3, "known")
        if out:
            return out
    if inp.deployment == "cloud":
        geo = g_cfg["provider_geography"].get(provider)
        if geo is None and not inp.region_pin:
            # A reseller cloud (Bedrock, Vertex, Azure) has no geography rule of its own:
            # fall back to the model maker's rule (anthropic -> USA, openai -> World).
            geo = g_cfg["provider_geography"].get(_canon(inp.maker) or "")
        if geo:
            iso = geo["geography"]
            out = located("provider_geography", iso,
                          f"{geo['basis']} ({geo['source']}); inferred, Ember 2025", 3,
                          "geo_usa" if iso == "USA" else "world")
            if out:
                return out
        if provider == "google":
            gg = g_cfg["google"]
            return {"rung": "provider_weighted", "geography": "Google fleet",
                    "g_per_kwh": float(gg["location_based_g_per_kwh"]), "td_multiplier": 1.0,
                    "td_applied": False, "basis": f"Google {gg['basis']}", "tier": 2,
                    "band_class": "provider_weighted", "notes": notes}
    out = located("world", "WLD", f"World average, {g_cfg['world_basis']}", 3, "world")
    assert out is not None
    return out


def _market_based(provider: str, deployment: str, facility_wh: float) -> dict[str, Any] | None:
    if deployment != "cloud":
        return None
    g = load_calibration()["grid"]
    cfg = g.get(provider) if provider in ("google", "meta") else None
    if not cfg:
        return None
    mb = float(cfg["market_based_g_per_kwh"])
    return {"g_per_kwh": mb, "basis": cfg["basis"], "operational_g": facility_wh / 1000.0 * mb,
            "note": "Market-based, stored beside location-based; never summed, no headline."}


# ── the chain (steps 1-8), reusable for shadows ──────────────────────────────
@dataclass
class _Ctx:
    inp: V3Inputs
    place: dict[str, Any]
    provider: str
    thinking: bool
    thinking_source: str
    hedge: bool
    host: float
    fleet: dict[str, Any]
    pue: dict[str, Any]
    grid: dict[str, Any]


def _chain(
    c: _Ctx, *, weights: str = "central", rate_mult: float = 1.0, fleet: float | None = None,
    reasoning: float | None = None, thinking: bool | None = None, grid_world: bool = False,
    host: float | None = None, embodied_ratio: float | None = None,
) -> dict[str, float]:
    cal = load_calibration()
    inp = c.inp
    think = c.thinking if thinking is None else thinking
    hedge = think and c.hedge
    hedge_out = int(cal["reasoning"]["hidden_hedge_output_multiple"] * inp.tokens.output) if hedge else 0
    w = _weighted(inp.tokens, cal["weights"][weights], hedge_out)
    metered = inp.metered_energy_wh is not None
    if metered:
        rate = None
        node_before = float(inp.metered_energy_wh)
        node = node_before
    else:
        h = c.host if host is None else host
        rate = c.place["rate_1_43"] * h / cal["host_uplift"]["table_host"] * rate_mult
        node_before = w * rate / 1e6
        r_mult = cal["reasoning"]["uplift"] if reasoning is None else reasoning
        node = node_before * (r_mult if think else 1.0)
    fleet_v = c.fleet["value"] if fleet is None else fleet
    facility = node * fleet_v * c.pue["value"]
    if grid_world:
        mult, _ = _td_multiplier("WLD")
        grid_eff = _country_g("WLD") * mult
    else:
        grid_eff = c.grid["g_per_kwh"] * (c.grid["td_multiplier"] if c.grid["td_applied"] else 1.0)
    operational = facility / 1000.0 * grid_eff
    if embodied_ratio is None:
        embodied = facility / 1000.0 * cal["embodied"]["g_per_kwh_facility"]
    else:
        embodied = embodied_ratio * operational
    router = inp.router_overhead_g
    return {"weighted_tokens": w, "rate": rate, "node_before": node_before, "node": node,
            "facility": facility, "grid_eff": grid_eff, "operational": operational,
            "embodied": embodied, "router": router, "total": operational + embodied + router}


# ── step 9: the band (D5.1, C4, C8, C10, R-16) ───────────────────────────────
def _factor(name: str, low: float, high: float, share: float, basis: str) -> dict[str, Any]:
    """Convert a (low, high) multiplier on one quantity to the total: eff = 1 + s(m - 1)."""
    return {"name": name, "low_mult": low, "high_mult": high, "share": share,
            "low_eff": min(1.0, 1.0 + share * (low - 1.0)),
            "high_eff": max(1.0, 1.0 + share * (high - 1.0)), "basis": basis}


def _band_factors(c: _Ctx, base: dict[str, float]) -> list[dict[str, Any]]:
    cal = load_calibration()
    bf = cal["band"]["factors"]
    inp = c.inp
    total = base["total"]
    if total <= 0:
        return []
    s_energy = (base["operational"] + base["embodied"]) / total
    s_op = base["operational"] / total
    s_emb = base["embodied"] / total
    metered = inp.metered_energy_wh is not None
    out: list[dict[str, Any]] = []

    def add(name: str, rng: list[float], share: float, basis: str, relative_to: float = 1.0) -> None:
        out.append(_factor(name, rng[0] / relative_to, rng[1] / relative_to, share, basis))

    if not metered:
        add("regime_x_hardware", bf["regime_x_hardware"], s_energy, "C4: regime x hardware spread")
        closed = c.place["closed"]
        if closed == "generic":
            add("closed_model_unknown", bf["closed_model_unknown_generic"], s_energy,
                "generic closed-model size factor (worked-job derivation)")
        elif closed == "bucket":
            klass, b = c.place["bucket"]
            add("closed_model_unknown", b["range"], s_energy,
                f"class {klass} bucket min/median - max/median", relative_to=b["median"])
        if c.place["has_D"]:
            add("deployment_factor_D", bf["deployment_factor_D"], s_energy, "C9: curve deployment factor D")
        if c.thinking:
            add("reasoning_uplift", bf["reasoning_uplift"], s_energy, "C5/R-13: thinking-run uplift")
        w0 = base["weighted_tokens"]
        hedge_out = int(cal["reasoning"]["hidden_hedge_output_multiple"] * inp.tokens.output) if (
            c.thinking and c.hedge) else 0
        for key, attr in (("input", "input"), ("cache_read", "cache_read"), ("cache_write", "cache_write")):
            if getattr(inp.tokens, attr) <= 0 or w0 <= 0:
                continue
            lo, hi = cal["weights"]["range"][key]
            ends = []
            for end in (lo, hi):
                w_end = dict(cal["weights"]["central"])
                w_end[key] = end
                ends.append(_weighted(inp.tokens, w_end, hedge_out) / w0)
            out.append(_factor(f"token_weight_{key}", ends[0], ends[1], s_energy,
                               f"D1.1/R-10: {key} weight range, W recomputed"))
        if c.fleet["range"]:
            v = c.fleet["value"]
            add("fleet", c.fleet["range"], s_energy, f"D3.2: {c.fleet['kind']} fleet range", relative_to=v)
        if c.pue["range"]:
            add("pue", c.pue["range"], s_energy, "D3.1: PUE came from a range", relative_to=c.pue["value"])
        if c.host != cal["host_uplift"]["google"]:
            add("host_uplift", cal["host_uplift"]["range"], s_energy, "D2.1/R-17: host uplift range",
                relative_to=c.host)
    emb = cal["embodied"]
    add("embodied_k", emb["range"], s_emb, "D3.3: embodied g/kWh range",
        relative_to=emb["g_per_kwh_facility"])
    g = c.grid
    bc = g["band_class"]
    if g["td_applied"]:
        if bc == "known":
            out.append(_factor("td_losses", 1.0 / g["td_multiplier"], 1.0, s_op,
                               "D4.4: known geography, one-sided down"))
        elif bc == "geo_usa":
            add("td_losses", bf["td_usa"], s_op, "D4.4: provider-geography USA")
        else:
            add("td_losses", bf["td_world"], s_op, "D4.4: World rung")
    if bc == "known":
        hi_thr = bf["grid_hourly_threshold_g_per_kwh"]
        if g["g_per_kwh"] >= hi_thr:
            rng = bf["grid_hourly_high"]
        elif inp.run_duration_s is None or inp.run_duration_s < bf["grid_hourly_short_run_s"]:
            rng = bf["grid_hourly_low_short"]
        else:
            rng = bf["grid_hourly_low_long"]
        add("grid_intensity", rng, s_op, "D4.5: hourly band on a known region")
    elif bc == "geo_usa":
        add("grid_intensity", bf["grid_usa"], s_op, "R-9: eGRID 2023 state spread on Ember USA")
    else:
        add("grid_intensity", bf["grid_world"], s_op, "R-9: World spread")
    return out


def _combine(factors: list[dict[str, Any]], total: float, rung: int) -> dict[str, Any]:
    floor = load_calibration()["band"]["floor"]
    low_div = math.exp(math.sqrt(sum(math.log(1.0 / f["low_eff"]) ** 2 for f in factors)))
    high_mult = math.exp(math.sqrt(sum(math.log(f["high_eff"]) ** 2 for f in factors)))
    floored = {"low": False, "high": False}
    if rung != 1:  # C10: curve-placed rates carry a floor; a measured/metered rate does not
        floored = {"low": low_div < floor, "high": high_mult < floor}
        low_div, high_mult = max(low_div, floor), max(high_mult, floor)
    env_low = math.prod(f["low_eff"] for f in factors)
    env_high = math.prod(f["high_eff"] for f in factors)
    return {
        "factors": [{k: v for k, v in f.items()} for f in factors],
        "low_div": low_div, "high_mult": high_mult,
        "low_g": total / low_div, "high_g": total * high_mult,
        "floor": floor if rung != 1 else None, "floor_applied": floored,
        "envelope_low_g": total * env_low, "envelope_high_g": total * env_high,
        "label": load_calibration()["band"]["label"],
    }


# ── step 10: shadows ─────────────────────────────────────────────────────────
def _shadows(c: _Ctx) -> dict[str, dict[str, Any]]:
    cal = load_calibration()
    metered = c.inp.metered_energy_wh is not None
    sh: dict[str, dict[str, Any]] = {}

    def put(name: str, label: str, **kw: Any) -> None:
        sh[name] = {"total_g": _chain(c, **kw)["total"], "label": label}

    if not metered:
        put("weights_0.05_set", "Shadow token weights 0.05 / 1 / 0.005 / 0.05", weights="shadow")
        if c.place["q16_midpoint"]:
            put("rate_q16", "Rate at Q16 (the geometric midpoint x sqrt(2))", rate_mult=math.sqrt(2))
        if c.thinking:
            put("reasoning_2.2", "Thinking uplift 2.2 instead of 1.3", reasoning=cal["reasoning"]["shadow"])
            if c.thinking_source == "provider_default":
                put("reasoning_off", "Reasoning off (mode came from the provider default)", thinking=False)
        if c.host == cal["host_uplift"]["default"]:
            put("host_1.43", "Host uplift 1.43 instead of 1.41", host=cal["host_uplift"]["table_host"])
    if c.fleet["kind"] == "hyperscaler":
        put("fleet_1.31", "Hyperscaler fleet 1.31 instead of 1.20", fleet=cal["fleet"]["hyperscaler"]["shadow"])
    put("embodied_ratio_0.111", "Embodied = 0.111 x operational",
        embodied_ratio=cal["embodied"]["shadow_ratio_to_operational"])
    world_eff = _country_g("WLD") * _td_multiplier("WLD")[0]
    if abs(c.grid["g_per_kwh"] * (c.grid["td_multiplier"] if c.grid["td_applied"] else 1.0) - world_eff) > 1e-9:
        put("grid_world", "World grid (458.49) with WLD T&D", grid_world=True)
    return sh


# ── values / evidence tiers (D5.1) ───────────────────────────────────────────
def _val(value: Any, tier: int, rng: Any, label: str) -> dict[str, Any]:
    return {"value": value, "tier": tier, "tier_label": _TIER_LABELS[tier], "range": rng, "label": label}


def _values(c: _Ctx, base: dict[str, float]) -> dict[str, dict[str, Any]]:
    cal = load_calibration()
    metered = c.inp.metered_energy_wh is not None
    v: dict[str, dict[str, Any]] = {}
    if metered:
        v["metered_energy_wh"] = _val(c.inp.metered_energy_wh, 1, None, "metered at the node")
    else:
        v["weights"] = _val(cal["weights"]["central"], 2, cal["weights"]["range"], "D1.1/R-10")
        v["rate_wh_per_mtok"] = _val(base["rate"], 2 if c.place["rung"] == 1 else 3, None,
                                     f"rung {c.place['rung']}: {c.place['size_source']}")
        v["host_uplift"] = _val(c.host, 2, cal["host_uplift"]["range"],
                                "provider-reported" if c.host == cal["host_uplift"]["google"] else "D2.1/R-17")
        v["production_step"] = _val(cal["production_step"]["value"], 2, cal["production_step"]["range"],
                                    "inside the regime factor, not a separate band factor")
        if c.thinking:
            v["reasoning_uplift"] = _val(cal["reasoning"]["uplift"], 2, cal["reasoning"]["range"],
                                         f"mode from {c.thinking_source}")
    v["fleet"] = _val(c.fleet["value"], c.fleet["tier"], c.fleet["range"], c.fleet["kind"])
    v["pue"] = _val(c.pue["value"], c.pue["tier"], c.pue["range"], c.pue["label"])
    v["grid_g_per_kwh"] = _val(c.grid["g_per_kwh"], c.grid["tier"], None, c.grid["basis"])
    v["td_multiplier"] = _val(c.grid["td_multiplier"] if c.grid["td_applied"] else 1.0, 2, None,
                              "D4.4" if c.grid["td_applied"] else "not applied (provider figure)")
    emb = cal["embodied"]
    v["embodied_g_per_kwh"] = _val(emb["g_per_kwh_facility"], 2, emb["range"], "D3.3, grid-independent")
    return v


# ── public entry point ───────────────────────────────────────────────────────
def compute_facility_v3(inputs: V3Inputs) -> dict[str, Any]:
    """Compute the facility_v3 block for one run. Pure and JSON-serialisable."""
    if inputs.deployment not in DEPLOYMENTS:
        raise ValueError(f"deployment must be one of {DEPLOYMENTS}")
    cal = load_calibration()
    metered = inputs.metered_energy_wh is not None
    place = _place(inputs)
    provider = resolve_provider(inputs)
    thinking, source = _reasoning_mode(inputs)
    if metered:
        thinking = False  # the meter already holds the thinking energy
    hosts = cal["host_uplift"]
    host = (hosts["metered"] if metered
            else hosts["google"] if (inputs.deployment == "cloud" and provider == "google")
            else hosts["default"])
    ctx = _Ctx(
        inp=inputs, place=place, provider=provider, thinking=thinking, thinking_source=source,
        hedge=bool(thinking and inputs.reasoning_count_hidden and not metered), host=host,
        fleet=_fleet(inputs, provider), pue=_pue(inputs, provider), grid=_grid(inputs, provider),
    )
    base = _chain(ctx)
    factors = _band_factors(ctx, base)
    band = _combine(factors, base["total"], place["rung"])
    flags = list(place["flags"])
    if ctx.hedge:
        flags.append("hidden reasoning hedge (tier 3)")
    if source == "provider_default" and thinking:
        flags.append("reasoning mode from provider default")
    flags.extend(ctx.grid["notes"])
    market = _market_based(provider, inputs.deployment, base["facility"])
    g = ctx.grid
    return {
        "method_id": METHOD_ID, "method_version": METHOD_VERSION, "calibration_id": CALIBRATION_ID,
        "pin": PIN, "boundary": BOUNDARY,
        "regime": REGIME_METERED if metered else REGIME_CLOUD,
        "parts": {
            "weighted_tokens": base["weighted_tokens"], "rate_wh_per_mtok": base["rate"],
            "node_wh_before_reasoning": base["node_before"], "node_wh": base["node"],
            "facility_wh": base["facility"], "fleet": ctx.fleet["value"], "pue": ctx.pue["value"],
            "grid_g_per_kwh": g["g_per_kwh"] * (g["td_multiplier"] if g["td_applied"] else 1.0),
            "td_multiplier": g["td_multiplier"] if g["td_applied"] else 1.0,
            "operational_g": base["operational"], "embodied_g": base["embodied"],
            "router_g": base["router"], "total_g": base["total"],
        },
        "placement": {"rung": place["rung"], "size_source": place["size_source"], "flags": flags,
                      "provider": provider, "host_uplift": host, "reasoning": {
                          "thinking": thinking, "source": source}},
        "grid": {
            "rung": g["rung"], "basis": g["basis"],
            "location_based": {"geography": g["geography"], "g_per_kwh": g["g_per_kwh"],
                               "td_multiplier": g["td_multiplier"] if g["td_applied"] else 1.0,
                               "g_per_kwh_effective": g["g_per_kwh"] * (
                                   g["td_multiplier"] if g["td_applied"] else 1.0)},
            "market_based": market,
        },
        "band": band,
        "shadows": _shadows(ctx),
        "values": _values(ctx, base),
        "training": TRAINING_DISCLOSURE,
        "comparative_difference": COMPARATIVE_WORDING,
    }
