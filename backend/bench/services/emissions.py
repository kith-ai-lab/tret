"""Emissions accounting: energy, data-centre overhead, GHG Protocol scopes.

This module owns every carbon number bench prints. It grew out of
`providers/catalog.py`'s energy accounting, which still re-exports the names it
used to own so existing imports keep working.

What is here, and what each part is worth:

* **Energy classes** (`ENERGY_CLASS_WH_PER_MTOK`) — order-of-magnitude buckets,
  Wh per million tokens processed. Nothing is metered.
* **PUE** — data-centre overhead. `energy_wh` stays the *compute* (IT-load)
  figure it has always been; `energy_wh_total` is compute x PUE.
* **Scopes** — the GHG Protocol mapping for the *bench operator*: Scope 1 is
  always 0, self-hosted electricity is Scope 2, cloud inference is Scope 3
  (purchased service), and amortized local hardware is Scope 3 (capital goods).
  `_SCOPE_BASIS` states the reasoning inline, on every run.
* **Frontier baseline** — a same-token counterfactual, i.e. what the identical
  token counts would have emitted on the heaviest curated cloud model. It is an
  efficiency indicator. It is not an offset, not a reduction claim, and not
  usable for statutory reporting. `avoided_co2e_g` is signed: a run heavier than
  the baseline reports a negative figure rather than a clamped zero.

Read docs/emissions-methodology.md before quoting any of these numbers. Every
constant in here is a heuristic with an uncertainty band of roughly a factor of
two to five, and the honest use of the output is comparison between two model
choices — never disclosure.

The catalog is imported lazily (inside functions) because `providers/catalog.py`
imports this module at import time; keep it that way.
"""
from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from bench.config import Settings, get_settings

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bench.providers.catalog import ModelCatalog, ModelInfo

# ── energy classes ───────────────────────────────────────────────────────────
# ORDER-OF-MAGNITUDE ESTIMATES, not measurements. No provider publishes
# per-model energy draw, so bench buckets models into four classes and attaches
# a heuristic Wh-per-million-tokens figure to each. The M anchor (~300 Wh/Mtok ≈
# 0.3 Wh for a ~1k-token prompt) matches the per-prompt figures large operators
# have published for mid-size served models; S/L/XL step roughly by model scale
# from there. Treat every number as a scale, not a measurement — assumptions and
# limits are spelled out in docs/emissions-methodology.md.
ENERGY_CLASS_WH_PER_MTOK: dict[str, Decimal] = {
    "S": Decimal("50"),  # small / distilled / quantized local weights
    "M": Decimal("300"),  # mid-size served models
    "L": Decimal("1200"),  # large frontier models
    "XL": Decimal("3000"),  # largest frontier / heavy-reasoning models
}
ENERGY_CLASSES = tuple(ENERGY_CLASS_WH_PER_MTOK)
DEFAULT_ENERGY_CLASS = "M"

# Energy weights per token bucket. A cache read re-uses stored KV state instead
# of a fresh forward pass, so it is discounted at the same 0.1x as price; a cache
# *write* is a full forward pass and carries full weight.
ENERGY_CACHE_READ_MULTIPLIER = Decimal("0.1")
ENERGY_CACHE_WRITE_MULTIPLIER = Decimal("1")

# Class defaults for catalog entries nobody has classified by hand, keyed on cost
# tier. Local models default to S (small quantized weights on end-user hardware),
# and a zero *dollar* price never means zero energy.
_TIER_ENERGY_CLASS = {"local": "S", "economy": "M", "standard": "L", "premium": "XL"}

# ── deployment split ─────────────────────────────────────────────────────────
# Only two cases matter for scope classification: did the operator buy the
# electricity (self-hosted) or did they buy a service that included it (cloud)?
DEPLOYMENT_CLOUD = "cloud"
DEPLOYMENT_LOCAL = "local"
LOCAL_PROVIDER = "local"

# Rounding used for every persisted figure. Six decimals is well below the
# precision these estimates actually carry; it exists so the JSON is stable and
# so component figures sum exactly to their total.
_PLACES = 6

_SCOPE_BASIS = (
    "GHG Protocol scopes from the bench operator's perspective. Scope 1 = 0: "
    "running inference burns no fuel on the operator's own site, and a nonzero "
    "Scope 1 could only come from on-site generation, which bench cannot "
    "observe — so it is reported as an explicit zero rather than omitted. "
    "Scope 2 = purchased electricity for self-hosted (local) inference, where "
    "the operator buys the power. Scope 3 = cloud inference, which is a "
    "purchased service: the provider's own Scope 1/2 becomes the operator's "
    "Scope 3 (Cat. 1, purchased goods and services); plus amortized embodied "
    "hardware for local inference (Cat. 2, capital goods), which is 0 unless "
    "BENCH_EMBODIED_G_PER_RUN is set. Estimated from token counts, not an "
    "inventory — docs/emissions-methodology.md."
)

_BASELINE_BASIS = (
    "Same-token counterfactual: this run's exact token counts (input, output, "
    "cache) re-priced through the baseline model's energy class. A different "
    "model would NOT produce identical token counts, so this is an efficiency "
    "indicator only — it is not an offset, not an emissions reduction or saving, "
    "not a credit, and not usable for statutory or regulatory reporting. The "
    "figure is signed: a run heavier than the baseline reports negative avoided "
    "carbon. docs/emissions-methodology.md."
)

_NO_BASELINE_BASIS = (
    "No baseline counterfactual available: no curated non-local model could be "
    "resolved (BENCH_EMISSIONS_BASELINE_MODEL may name a model the catalog does "
    "not have). Reported as null rather than zero — an unavailable comparison is "
    "not a comparison that came out even."
)

_ACCOUNTING_BASIS = (
    "heuristic energy class x weighted tokens, x data-centre PUE, x grid "
    "intensity; estimate, not a measurement (docs/emissions-methodology.md)"
)


def _f(value: Decimal | float | int, places: int = _PLACES) -> float:
    """JSON-safe rounded float. Everything persisted goes through here."""
    return float(round(Decimal(str(value)), places))


def wh_per_mtok_for_class(energy_class: str) -> Decimal:
    """Wh per million tokens for an energy class; unknown classes fall back to M."""
    return ENERGY_CLASS_WH_PER_MTOK.get(
        energy_class, ENERGY_CLASS_WH_PER_MTOK[DEFAULT_ENERGY_CLASS]
    )


def energy_class_for_tier(cost_tier: str) -> str:
    """Estimated energy class for an unclassified model, from its cost tier."""
    return _TIER_ENERGY_CLASS.get(cost_tier, DEFAULT_ENERGY_CLASS)


def weighted_tokens(
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Tokens weighted by how much forward-pass work each bucket really costs."""
    return (
        Decimal(input_tokens)
        + Decimal(output_tokens)
        + ENERGY_CACHE_READ_MULTIPLIER * Decimal(cache_read_tokens)
        + ENERGY_CACHE_WRITE_MULTIPLIER * Decimal(cache_write_tokens)
    )


def co2e_grams(energy_wh: Decimal, grid_g_per_kwh: float | None = None) -> Decimal:
    """gCO2e for an energy figure, at the configured grid intensity.

    Takes energy as given: pass the *total* (PUE-inclusive) figure if that is
    what you mean to convert. Unchanged from the original contract.
    """
    if grid_g_per_kwh is None:
        grid_g_per_kwh = get_settings().grid_co2e_g_per_kwh
    return energy_wh * Decimal(str(grid_g_per_kwh)) / Decimal(1000)


# ── deployment, overhead, grid factors ───────────────────────────────────────
def deployment_for(provider: str) -> str:
    """"local" for self-hosted inference, "cloud" for everything else.

    The distinction is who pays the power bill, which is what decides the scope
    the emissions land in.
    """
    return DEPLOYMENT_LOCAL if provider == LOCAL_PROVIDER else DEPLOYMENT_CLOUD


def pue_for(deployment: str, settings: Settings | None = None) -> Decimal:
    """Power Usage Effectiveness: total facility energy / IT-load energy.

    Heuristic. Hyperscalers self-report fleet-wide PUE around 1.1–1.2, so 1.2 is
    the default for cloud inference — a mildly conservative pick inside the
    published range, and one that ignores the (real, unpublished) spread between
    an efficient new campus and an older leased facility. Self-hosted inference
    on a desktop or workstation has almost no facility overhead to speak of, so
    `local_pue` defaults to 1.05 for fans and room cooling.
    """
    settings = settings or get_settings()
    raw = settings.local_pue if deployment == DEPLOYMENT_LOCAL else settings.datacenter_pue
    pue = Decimal(str(raw))
    # A PUE below 1 is physically impossible (you cannot use less than the IT
    # load); refuse to let a misconfiguration shrink the number.
    return pue if pue >= 1 else Decimal(1)


def grid_factor_for(deployment: str, settings: Settings | None = None) -> float:
    """gCO2e/kWh to apply to this deployment's electricity.

    Self-hosted inference may use the operator's own site/market-based factor
    (`local_grid_co2e_g_per_kwh`); unset, it falls back to the single
    `grid_co2e_g_per_kwh`. Cloud inference always uses `grid_co2e_g_per_kwh`,
    since bench does not know which region served the request.
    """
    settings = settings or get_settings()
    if deployment == DEPLOYMENT_LOCAL and settings.local_grid_co2e_g_per_kwh is not None:
        return float(settings.local_grid_co2e_g_per_kwh)
    return float(settings.grid_co2e_g_per_kwh)


def embodied_g_for(deployment: str, settings: Settings | None = None) -> Decimal:
    """Amortized embodied (capital-goods) carbon per run, in grams.

    Only self-hosted inference gets one: it is the operator's own hardware, so
    manufacturing it is their Scope 3 Cat. 2. Cloud hardware is embedded in the
    purchased service and is not separately estimated (bench has no basis for
    it). Defaults to 0, which *understates* local inference — see the docs.
    """
    settings = settings or get_settings()
    if deployment != DEPLOYMENT_LOCAL:
        return Decimal(0)
    return max(Decimal(str(settings.embodied_g_per_run)), Decimal(0))


# ── frontier baseline ────────────────────────────────────────────────────────
def resolve_baseline_model(
    settings: Settings | None = None, catalog: ModelCatalog | None = None
) -> ModelInfo | None:
    """The counterfactual comparison model, or None if there isn't one.

    `emissions_baseline_model` names it explicitly. Empty (the default) picks the
    highest-energy-class curated non-local model, tie-broken by model id so the
    choice is deterministic across processes. A configured id the catalog does
    not know resolves to None rather than silently falling back to a different
    model: a quiet substitution would make the number unauditable.
    """
    settings = settings or get_settings()
    if catalog is None:
        from bench.providers.catalog import get_catalog

        catalog = get_catalog()
    configured = (settings.emissions_baseline_model or "").strip()
    if configured:
        return catalog.get(configured)
    candidates = [m for m in catalog.all(curated_only=True) if m.provider != LOCAL_PROVIDER]
    if not candidates:
        return None
    return sorted(candidates, key=lambda m: (-(m.energy_wh_per_mtok or Decimal(0)), m.id))[0]


def _no_baseline() -> dict:
    return {
        "model": None,
        "energy_class": None,
        "energy_wh": None,
        "energy_wh_total": None,
        "co2e_g": None,
        "avoided_co2e_g": None,
        "avoided_pct": None,
        "basis": _NO_BASELINE_BASIS,
    }


def _baseline_block(
    model: ModelInfo,
    tokens: tuple[int, int, int, int],
    actual_co2e_g: Decimal,
    actual_energy_wh: Decimal,
    actual_energy_wh_total: Decimal,
    grid_g_per_kwh: float | None,
    settings: Settings,
    catalog: ModelCatalog | None,
) -> dict:
    baseline = resolve_baseline_model(settings, catalog)
    if baseline is None:
        return _no_baseline()
    if baseline.id == model.id:
        # The run *is* the baseline. Avoided is 0 by construction, not by
        # arithmetic: comparing a run to itself has no counterfactual in it.
        return {
            "model": baseline.id,
            "energy_class": baseline.energy_class,
            "energy_wh": _f(actual_energy_wh),
            "energy_wh_total": _f(actual_energy_wh_total),
            "co2e_g": _f(actual_co2e_g),
            "avoided_co2e_g": 0.0,
            "avoided_pct": 0.0,
            "basis": (
                _BASELINE_BASIS + " This run used the baseline model itself, so avoided is 0."
            ),
        }
    deployment = deployment_for(baseline.provider)
    pue = pue_for(deployment, settings)
    grid = grid_g_per_kwh if grid_g_per_kwh is not None else grid_factor_for(deployment, settings)
    compute_wh = baseline.energy_wh(*tokens)
    total_wh = compute_wh * pue
    electricity_g = round(co2e_grams(total_wh, grid), _PLACES)
    embodied_g = round(embodied_g_for(deployment, settings), _PLACES)
    baseline_co2e = electricity_g + embodied_g
    avoided = baseline_co2e - round(actual_co2e_g, _PLACES)
    avoided_pct = (
        _f(Decimal(100) * avoided / baseline_co2e, 3) if baseline_co2e > 0 else 0.0
    )
    return {
        "model": baseline.id,
        "energy_class": baseline.energy_class,
        "energy_wh": _f(compute_wh),
        "energy_wh_total": _f(total_wh),
        "co2e_g": _f(baseline_co2e),
        # Signed on purpose: a run that used something heavier than the baseline
        # avoided nothing, and clamping that to zero would be a greenwash.
        "avoided_co2e_g": _f(avoided),
        "avoided_pct": avoided_pct,
        "basis": _BASELINE_BASIS,
    }


# ── the persisted per-run block ──────────────────────────────────────────────
def scope_split(deployment: str, electricity_g: Decimal, embodied_g: Decimal) -> dict:
    """{scope1_g, scope2_g, scope3_g, basis}, all rounded so they sum exactly.

    Scope 1 is always 0.0 and always present. Cloud electricity is Scope 3
    (purchased service); self-hosted electricity is Scope 2 (purchased power)
    and self-hosted embodied hardware is Scope 3 (capital goods).
    """
    electricity = round(electricity_g, _PLACES)
    embodied = round(embodied_g, _PLACES)
    if deployment == DEPLOYMENT_LOCAL:
        scope2, scope3 = electricity, embodied
    else:
        scope2, scope3 = Decimal(0), electricity + embodied
    return {
        "scope1_g": 0.0,
        "scope2_g": _f(scope2),
        "scope3_g": _f(scope3),
        "basis": _SCOPE_BASIS,
    }


def energy_accounting(
    model: ModelInfo,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    grid_g_per_kwh: float | None = None,
    *,
    settings: Settings | None = None,
    catalog: ModelCatalog | None = None,
) -> dict:
    """The auditable energy/carbon breakdown persisted on a run.

    Every field is an estimate derived from token counts, the model's energy
    class, a heuristic PUE and a grid intensity — nothing here is metered.
    JSON-serializable throughout (floats and strings, no Decimals).

    Key meanings, unchanged from the original contract:

    * `energy_wh` — **compute (IT-load) energy only**, no data-centre overhead.
    * `grid_co2e_g_per_kwh` — the factor actually applied to this run's
      electricity (the local override, when a local run has one configured).
    * `co2e_g` — the run's **total** estimated carbon, and always exactly
      `scopes.scope1_g + scope2_g + scope3_g`.

    Added: `pue`, `energy_wh_total` (compute x PUE), `deployment`, `embodied_g`,
    `scopes`, `baseline`.
    """
    settings = settings or get_settings()
    deployment = deployment_for(model.provider)
    pue = pue_for(deployment, settings)
    grid = grid_g_per_kwh if grid_g_per_kwh is not None else grid_factor_for(deployment, settings)

    compute_wh = model.energy_wh(
        input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
    )
    total_wh = compute_wh * pue
    electricity_g = co2e_grams(total_wh, grid)
    embodied_g = embodied_g_for(deployment, settings)

    scopes = scope_split(deployment, electricity_g, embodied_g)
    # Total is the sum of the *rounded* scopes, so the invariant
    # co2e_g == scope1 + scope2 + scope3 holds exactly rather than nearly.
    total_g = (
        Decimal(str(scopes["scope1_g"]))
        + Decimal(str(scopes["scope2_g"]))
        + Decimal(str(scopes["scope3_g"]))
    )

    return {
        # ── original keys, original meanings ──
        "estimated": True,
        "model": model.id,
        "energy_class": model.energy_class,
        "energy_wh_per_mtok": float(model.energy_wh_per_mtok),
        "weighted_tokens": float(
            weighted_tokens(input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
        ),
        "cache_read_weight": float(ENERGY_CACHE_READ_MULTIPLIER),
        "cache_write_weight": float(ENERGY_CACHE_WRITE_MULTIPLIER),
        "energy_wh": _f(compute_wh),  # compute / IT load only
        "grid_co2e_g_per_kwh": float(grid),
        "co2e_g": _f(total_g),  # == scope1 + scope2 + scope3
        "basis": _ACCOUNTING_BASIS,
        # ── added ──
        "pue": float(pue),
        "energy_wh_total": _f(total_wh),  # compute x PUE
        "deployment": deployment,
        "embodied_g": _f(embodied_g),
        "scopes": scopes,
        "baseline": _baseline_block(
            model,
            (input_tokens, output_tokens, cache_read_tokens, cache_write_tokens),
            total_g,
            compute_wh,
            total_wh,
            grid_g_per_kwh,
            settings,
            catalog,
        ),
    }


# ── reading a stored block back ──────────────────────────────────────────────
def emission_summary_fields(accounting: dict | None) -> dict[str, Any]:
    """The carbon fields a run *summary* carries, read as recorded.

    Missing means missing: a run with no estimate (or one recorded before scopes
    existed) reports None, never 0. `None != 0` is the whole point — a zero would
    claim the run emitted nothing.
    """
    accounting = accounting or {}
    scopes = accounting.get("scopes") or {}
    baseline = accounting.get("baseline") or {}
    return {
        "co2e_g": accounting.get("co2e_g"),
        "scope2_g": scopes.get("scope2_g"),
        "scope3_g": scopes.get("scope3_g"),
        "avoided_co2e_g": baseline.get("avoided_co2e_g"),
    }


def emission_event_fields(accounting: dict | None) -> dict[str, Any]:
    """The carbon fields the SSE `usage`/`done` events carry. Nulls stay null."""
    accounting = accounting or {}
    scopes = accounting.get("scopes") or {}
    baseline = accounting.get("baseline") or {}
    return {
        "co2e_g": accounting.get("co2e_g"),
        "scope2_g": scopes.get("scope2_g"),
        "scope3_g": scopes.get("scope3_g"),
        "baseline_co2e_g": baseline.get("co2e_g"),
        "avoided_co2e_g": baseline.get("avoided_co2e_g"),
    }
