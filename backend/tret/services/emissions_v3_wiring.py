"""Wiring for the parallel emissions method v3 ("facility_v3", preview).

`emissions_v3.compute_facility_v3` is a pure calculator; this module maps a
run's recorded facts (segments, per-call records, overhead calls) onto its
inputs and rolls the per-segment blocks up into one run-level `method_v3`
block. Nothing here feeds back into any existing figure: `co2e_g`,
`energy_accounting`, `cost_usd`, analytics and exports are untouched. The
block is persisted additively under `runs.routing["method_v3"]` (the same
JSONB the cache ledger already extends) and exposed as its own field on the
run detail / chat responses; `public_routing` strips it from the `routing`
field so that field stays byte-identical to what it was before v3 existed.

Every public function here is exception-safe: on any failure it logs at
warning level and returns None, so the run path can never be broken by the
preview method.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable

from datetime import datetime

from tret.services.emissions_v3 import V3Inputs, V3Tokens, canonical_model_id, compute_facility_v3

log = logging.getLogger(__name__)

ROUTING_KEY = "method_v3"
LABEL = "method v3 (preview) — parallel estimate, not the reported figure"
PROVENANCE_NOTE = (
    "Parallel estimate under the revised method (Kith method lab, 2026-10-01). "
    "Not the reported figure."
)
BAND_AGGREGATION = "comonotonic_sum"

_PROFILE_TO_DEPLOYMENT = {
    "workstation": "self_hosted_workstation",
    "onprem_datacenter": "self_hosted_onprem",
}
_PIN_CLOUDS = ("aws", "gcp", "azure")
# model.provider values that are routers/hosts rather than a serving identity.
_NOT_A_SERVING_PROVIDER = {"openrouter", "local"}


@dataclass(frozen=True)
class V3FactorsCtx:
    """Optional resolved emissions factors for a segment (a `FactorSet`)."""

    factors: Any | None = None


# ── small helpers ────────────────────────────────────────────────────────────
def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def maker_of(model_id: str) -> str | None:
    """Model maker = the id prefix, with a leading `openrouter/` stripped."""
    mid = normalise_model_id(model_id)
    return mid.split("/", 1)[0] if "/" in mid else None


def _v3_model_id(model: Any, fallback: str | None) -> str:
    """The catalog's explicit `openrouter_id` when it has one, else the id with
    only `openrouter/` stripped. Not canonicalised here: `lookup_classification`
    tries the id as given before its canonical spelling, and canonicalising first
    would rewrite dated table ids (`gpt-4o-2024-11-20` → `…-11.20`) out of reach."""
    explicit = getattr(model, "openrouter_id", None)
    mid = explicit or getattr(model, "id", None) or fallback
    if not mid:
        raise ValueError("segment has no model id")
    return explicit or mid.removeprefix("openrouter/")


def normalise_model_id(model_id: str) -> str:
    """The id the classification table is keyed by (see `canonical_model_id`)."""
    return canonical_model_id(model_id)


def _cloud_for_region(region: str | None, served_by: str | None) -> tuple[str, str] | None:
    """Map a pinned region code to ("aws" | "gcp" | "azure", code), or None.

    The three clouds' region codes have disjoint spellings, so a region present in
    the calibration's region map under exactly one cloud is unambiguous; when more
    than one matches, the serving provider breaks the tie.
    """
    if not region:
        return None
    from tret.services.emissions_v3 import load_calibration

    rmap = load_calibration()["region_map"]
    code = region.strip().lower()
    hits = [c for c in _PIN_CLOUDS if f"{c}:{code}" in rmap]
    if len(hits) == 1:
        return hits[0], code
    if hits:
        hint = (served_by or "").lower()
        for cloud, needles in (("aws", ("bedrock", "amazon", "aws")),
                               ("gcp", ("vertex", "google")),
                               ("azure", ("azure", "microsoft"))):
            if cloud in hits and any(n in hint for n in needles):
                return cloud, code
    return None


def _region_of(segment: Any, factors: Any | None) -> str | None:
    region = None
    try:
        region = factors.grid.region if factors is not None else None
    except AttributeError:
        region = None
    if region is None:
        acct = _get(segment, "energy_accounting")
        if isinstance(acct, dict):
            region = acct.get("grid_region")
    return region


def _deployment(model: Any, factors: Any | None, accounting: dict | None) -> str:
    local = getattr(model, "provider", None) == "local"
    profile = None
    if factors is not None:
        local = local or getattr(factors, "deployment", None) == "local"
        profile = getattr(factors, "pue_profile", None)
    if accounting:
        local = local or accounting.get("deployment") == "local"
        profile = profile or accounting.get("pue_profile")
    if not local:
        return "cloud"
    return _PROFILE_TO_DEPLOYMENT.get(profile or "", "self_hosted_workstation")


MODELLED_FLAG = "meter incomplete; modelled"


def _measured_wh(segment: Any) -> tuple[float | None, bool]:
    """(metered node Wh, meter_was_unusable).

    Used only where the meter's boundary is the node (v3 applies the self-hosted
    PUE itself; a facility/gpu/partial reading cannot be mapped without double
    counting or understating) and the reading is complete and attributable to
    this run. An incomplete or shared-device reading falls back to the modelled
    path (second element True, so the block can say so)."""
    reading = _get(segment, "meter_reading")
    if reading is not None:
        if getattr(reading, "energy_boundary", None) != "node_it":
            return None, False
        if not getattr(reading, "complete", True) or getattr(reading, "shared_device", False):
            return None, True
        return float(reading.wh), False
    acct = _get(segment, "energy_accounting")
    if isinstance(acct, dict) and acct.get("energy_source") == "measured" \
            and acct.get("energy_boundary") == "node_it" and acct.get("energy_wh") is not None:
        meter = acct.get("energy_meter") or {}
        if acct.get("energy_boundary_complete") is False or meter.get("complete") is False \
                or meter.get("shared_device"):
            return None, True
        return float(acct["energy_wh"]), False
    return None, False


def _span_s(calls: list[dict]) -> float | None:
    try:
        starts = [datetime.fromisoformat(c["started_at"]) for c in calls]
        ends = [datetime.fromisoformat(c["ended_at"]) for c in calls]
    except (KeyError, TypeError, ValueError):
        return None
    if not starts or len(starts) != len(calls):
        return None
    span = (max(ends) - min(starts)).total_seconds()
    return span if span > 0 else None


def _segment_calls(segment: Any) -> list[dict]:
    calls = list(_get(segment, "call_records") or [])
    if calls:
        return calls
    usage = _get(segment, "usage")
    if usage is not None:  # a live ModelSegment with no call records
        tok = {"input_tokens": usage.input_tokens, "output_tokens": usage.output_tokens,
               "cache_read_tokens": usage.cache_read_tokens,
               "cache_write_tokens": usage.cache_write_tokens,
               "reasoning_tokens": usage.reasoning_tokens,
               "reasoning_accounting": usage.reasoning_accounting}
    else:  # the persisted segment JSON
        tok = {k: _get(segment, k) for k in (
            "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
            "reasoning_tokens", "reasoning_accounting")}
    return [{**tok, "served_by": _get(segment, "served_by"),
             "reasoning_requested": True if _get(segment, "effort") else None}]


# ── block arithmetic ─────────────────────────────────────────────────────────
_PART_KEYS = ("weighted_tokens", "node_wh_before_reasoning", "node_wh", "facility_wh",
              "operational_g", "embodied_g", "router_g", "total_g")


def sum_shadows(blocks: list[dict]) -> dict[str, dict]:
    """Shadow totals summed over blocks. A block lacking a shadow contributes its
    own base total (the shadow's change does not apply to it), and a shadow that
    exists only on a non-lead block is included."""
    out: dict[str, dict] = {}
    for block in blocks:
        for name, sh in block["shadows"].items():
            out.setdefault(name, {"label": sh["label"], "total_g": 0.0})
    for name, entry in out.items():
        entry["total_g"] = sum(
            b["shadows"][name]["total_g"] if name in b["shadows"] else b["parts"]["total_g"]
            for b in blocks
        )
    return out


def _merge_group_blocks(blocks: list[dict], served_by: list[str | None]) -> dict:
    """Sum per-served_by blocks of one segment into one segment block.

    Placement, grid, factors and stamps come from the heaviest group; additive
    parts add; band edges add (comonotonic sum, see `build_run_block`).
    """
    if len(blocks) == 1:
        return blocks[0]
    lead = max(blocks, key=lambda b: b["parts"]["total_g"])
    merged = {**lead, "parts": dict(lead["parts"])}
    for key in _PART_KEYS:
        vals = [b["parts"].get(key) for b in blocks]
        if all(v is not None for v in vals):
            merged["parts"][key] = sum(vals)
    band = dict(lead["band"])
    for key in ("low_g", "high_g", "envelope_low_g", "envelope_high_g"):
        band[key] = sum(b["band"][key] for b in blocks)
    total = merged["parts"]["total_g"]
    band["low_div"] = total / band["low_g"] if band["low_g"] else lead["band"]["low_div"]
    band["high_mult"] = band["high_g"] / total if total else lead["band"]["high_mult"]
    merged["band"] = band
    merged["shadows"] = sum_shadows(blocks)
    merged["groups"] = [
        {"served_by": sb, "total_g": b["parts"]["total_g"], "grid_rung": b["grid"]["rung"],
         "pue": b["parts"]["pue"]}
        for sb, b in zip(served_by, blocks)
    ]
    return merged


# ── adapter: one segment ─────────────────────────────────────────────────────
def _inputs_for_group(
    *, model: Any, model_id: str, calls: list[dict], serving: str | None, deployment: str,
    region_pin: tuple[str, str] | None, metered_wh: float | None,
    run_duration_s: float | None = None,
) -> V3Inputs:
    def total(key: str) -> int:
        return sum(int(c.get(key) or 0) for c in calls)

    extra_reasoning = sum(
        int(c["reasoning_tokens"]) for c in calls
        if c.get("reasoning_accounting") == "additional" and c.get("reasoning_tokens") is not None
    )
    rtoks = [c.get("reasoning_tokens") for c in calls]
    known_reasoning = sum(int(r) for r in rtoks if r is not None) if all(
        r is not None for r in rtoks) else None
    thinking = any(c.get("reasoning_requested") is True for c in calls)
    hidden = any(
        c.get("reasoning_requested") is True and c.get("reasoning_tokens") is None
        and c.get("reasoning_accounting") == "unknown" for c in calls
    )
    return V3Inputs(
        model_id=model_id,
        maker=maker_of(model_id),
        serving_provider=serving,
        deployment=deployment,
        metered_energy_wh=metered_wh,
        active_params_b=getattr(model, "active_params_b", None),
        cost_tier=getattr(model, "cost_tier", None),
        tokens=V3Tokens(
            input=total("input_tokens"),
            # Reasoning outside the provider's output count is still generated output (D1.2).
            output=total("output_tokens") + extra_reasoning,
            cache_read=total("cache_read_tokens"),
            cache_write=total("cache_write_tokens"),
            reasoning=known_reasoning,
        ),
        reasoning_mode=True if thinking else None,
        reasoning_default_enabled=getattr(model, "reasoning_default_enabled", None),
        reasoning_count_hidden=hidden,
        region_pin=region_pin,
        openrouter_data_region=None,  # tret has no OpenRouter data_region concept
        run_duration_s=run_duration_s,
    )


def v3_for_segment(
    segment: Any, model: Any, factors_ctx: V3FactorsCtx | Any | None = None,
    *, run_duration_s: float | None = None,
) -> dict | None:
    """The v3 block for one model segment (a `ModelSegment` or its persisted JSON).

    Calls are grouped by `served_by`; a segment whose calls all share one upstream
    is a single computation, otherwise each group is computed on its own and the
    blocks are summed. Returns None (and logs at warning) on any failure.
    """
    try:
        factors = getattr(factors_ctx, "factors", factors_ctx) or _get(segment, "factors")
        model_id = _v3_model_id(model, _get(segment, "model"))
        accounting = _get(segment, "energy_accounting")
        accounting = accounting if isinstance(accounting, dict) else None
        deployment = _deployment(model, factors, accounting)
        region = _region_of(segment, factors)
        metered, meter_unusable = _measured_wh(segment)
        calls = _segment_calls(segment)
        if metered is not None:  # a meter is per segment, not per call: one group
            groups: dict[str | None, list[dict]] = {_get(segment, "served_by"): calls}
        else:
            groups = {}
            for call in calls:
                groups.setdefault(call.get("served_by"), []).append(call)
        provider = getattr(model, "provider", None) or _get(segment, "provider")
        blocks, keys = [], []
        for served_by, group in groups.items():
            serving = served_by or (provider if provider not in _NOT_A_SERVING_PROVIDER else None)
            inputs = _inputs_for_group(
                model=model, model_id=model_id, calls=group, serving=serving,
                deployment=deployment, region_pin=_cloud_for_region(region, served_by),
                metered_wh=metered,
                run_duration_s=run_duration_s if run_duration_s is not None else _span_s(group),
            )
            block = compute_facility_v3(inputs)
            if meter_unusable:
                block["placement"]["flags"].append(MODELLED_FLAG)
            blocks.append(block)
            keys.append(served_by)
        return _merge_group_blocks(blocks, keys) if blocks else None
    except Exception:  # noqa: BLE001 - the preview method must never break a run
        log.warning("method v3: segment adapter failed", exc_info=True)
        return None


# ── adapter: one overhead call (router / compaction) ─────────────────────────
def v3_for_overhead_call(call: dict, model: Any | None = None) -> dict | None:
    """The v3 block for one overhead call, computed like a segment on its own model."""
    try:
        model_id = _v3_model_id(model, call["model"])
        acct = call.get("energy_accounting") if isinstance(call.get("energy_accounting"), dict) else None
        provider = (getattr(model, "provider", None) or call.get("provider"))
        deployment = _deployment(model or _Stub(provider), None, acct)
        region = acct.get("grid_region") if acct else None
        served_by = call.get("served_by")
        serving = served_by or (provider if provider not in _NOT_A_SERVING_PROVIDER else None)
        inputs = _inputs_for_group(
            model=model or _Stub(provider), model_id=model_id, calls=[call], serving=serving,
            deployment=deployment, region_pin=_cloud_for_region(region, served_by),
            metered_wh=None,
        )
        block = compute_facility_v3(inputs)
        block["kind"] = call.get("kind")
        return block
    except Exception:  # noqa: BLE001
        log.warning("method v3: overhead adapter failed", exc_info=True)
        return None


@dataclass(frozen=True)
class _Stub:
    provider: str | None = None


# ── run level ────────────────────────────────────────────────────────────────
def build_run_block(segment_blocks: list[dict], overhead_blocks: list[dict]) -> dict:
    """Roll segment and overhead blocks into one run-level `method_v3` block.

    `total_g` = Σ segment totals + Σ overhead totals; the overhead total sits
    inside it as the named component `router_g` (D0.2, D5.3), so operational_g +
    embodied_g + router_g == total_g. The band edges are the sums of every
    component's own low_g / high_g: a comonotonic sum, which assumes all
    components err in the same direction at once and so is the conservative
    (widest) combination, wider than any independent-errors alternative.
    """
    first = segment_blocks[0]
    router_g = sum(b["parts"]["total_g"] for b in overhead_blocks)
    operational = sum(b["parts"]["operational_g"] for b in segment_blocks)
    embodied = sum(b["parts"]["embodied_g"] for b in segment_blocks)
    seg_total = sum(b["parts"]["total_g"] for b in segment_blocks)
    total = seg_total + router_g
    everything = [*segment_blocks, *overhead_blocks]
    low = sum(b["band"]["low_g"] for b in everything)
    high = sum(b["band"]["high_g"] for b in everything)
    primary_idx = max(range(len(segment_blocks)), key=lambda i: segment_blocks[i]["parts"]["total_g"])
    primary = segment_blocks[primary_idx]
    return {
        "method_id": first["method_id"], "method_version": first["method_version"],
        "calibration_id": first["calibration_id"], "pin": first["pin"],
        "boundary": first["boundary"],
        "preview": True,
        "label": LABEL,
        "note": PROVENANCE_NOTE,
        "parts": {"operational_g": operational, "embodied_g": embodied,
                  "router_g": router_g, "total_g": total},
        "band": {"low_g": low, "high_g": high,
                 "low_div": total / low if low else None,
                 "high_mult": high / total if total else None,
                 "floor_applied": primary["band"]["floor_applied"],
                 "aggregation": BAND_AGGREGATION, "label": first["band"]["label"]},
        "shadows": sum_shadows(everything),
        "total_g": total,
        "primary_segment": primary_idx,
        "grid": primary["grid"],
        "placement": primary["placement"],
        "segments": segment_blocks,
        "overhead": overhead_blocks,
        "training": first["training"],
        "comparative_difference": first["comparative_difference"],
    }


def build_method_v3(
    segments: Iterable[Any],
    overhead_calls: Iterable[dict | None],
    *,
    catalog: Any | None = None,
    run_duration_s: float | None = None,
) -> dict | None:
    """The run's `method_v3` block, or None when anything could not be computed.

    A partial figure (one segment missing) would understate the run, so any
    failed component voids the whole block rather than reporting a short total.
    """
    try:
        seg_blocks: list[dict] = []
        for seg in segments:
            block = v3_for_segment(
                seg, seg.model if not isinstance(seg, dict) else None,
                run_duration_s=run_duration_s,
            )
            if block is None:
                return None
            seg_blocks.append(block)
        if not seg_blocks:
            return None
        oh_blocks: list[dict] = []
        for call in overhead_calls:
            if not call:
                continue
            model = catalog.get(call.get("model")) if catalog is not None else None
            block = v3_for_overhead_call(call, model)
            if block is None:
                return None
            oh_blocks.append(block)
        return build_run_block(seg_blocks, oh_blocks)
    except Exception:  # noqa: BLE001
        log.warning("method v3: run block failed", exc_info=True)
        return None


def run_duration(started: datetime | None, finished: datetime | None) -> float | None:
    """Seconds between two run timestamps, or None when unknown or incomparable."""
    try:
        if started is None or finished is None:
            return None
        span = (finished - started).total_seconds()
        return span if span >= 0 else None
    except Exception:  # noqa: BLE001 - e.g. naive vs aware datetimes
        return None


def attach_to_routing(routing: dict | None, block: dict | None) -> dict | None:
    """`routing` with `method_v3` added (a new dict, so the JSONB change is seen).

    Returns `routing` unchanged when there is no block."""
    if block is None:
        return routing
    return {**(routing or {}), ROUTING_KEY: block}


_SLIM_STAMPS = ("method_id", "method_version", "calibration_id", "pin", "boundary", "preview",
                "label", "note", "total_g", "parts", "shadows", "training",
                "comparative_difference", "primary_segment")
_SLIM_BAND = ("low_g", "high_g", "low_div", "high_mult", "label", "floor_applied", "aggregation")
_SLIM_GRID = ("rung", "basis", "location_based", "market_based")
_SLIM_PLACEMENT = ("rung", "size_source", "flags", "provider", "reasoning")


def slim_method_v3(block: dict | None) -> dict | None:
    """The API projection: stamps, parts, total/band, grid, placement, shadows and
    the training note. Omits per-factor band rows, `values`, and the per-segment,
    per-overhead and per-group detail (`?v3=full` returns the stored block)."""
    if not isinstance(block, dict):
        return None
    out = {k: block[k] for k in _SLIM_STAMPS if k in block}
    out["band"] = {k: block["band"][k] for k in _SLIM_BAND if k in block.get("band", {})}
    out["grid"] = {k: block["grid"][k] for k in _SLIM_GRID if k in block.get("grid", {})}
    out["placement"] = {k: block["placement"][k] for k in _SLIM_PLACEMENT
                        if k in block.get("placement", {})}
    return out


def method_v3_of(routing: dict | None, *, full: bool = False) -> dict | None:
    """The persisted block (slim by default), or None for a run that predates v3
    (never backfilled)."""
    block = (routing or {}).get(ROUTING_KEY) if isinstance(routing, dict) else None
    return block if full else slim_method_v3(block)


def public_routing(routing: dict | None) -> dict | None:
    """`routing` as it was before v3 existed: the same object when it carries no
    block, otherwise a copy without it."""
    if isinstance(routing, dict) and ROUTING_KEY in routing:
        return {k: v for k, v in routing.items() if k != ROUTING_KEY}
    return routing


__all__ = [
    "LABEL", "ROUTING_KEY", "V3FactorsCtx", "attach_to_routing", "build_method_v3",
    "build_run_block", "maker_of", "method_v3_of", "run_duration", "public_routing", "slim_method_v3", "sum_shadows", "v3_for_overhead_call",
    "v3_for_segment",
]
