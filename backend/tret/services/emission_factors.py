"""A resolved "factor set": every emissions constant, layered and traced.

`tret/services/emissions.py` already knows how to pick one grid factor out of
three sources (a run override, `TRET_GRID_FACTORS`, the global default) and
records *which rule* won on every run. This module generalises that pattern to
every accounting constant (grid intensity, PUE, embodied hardware, the
uncertainty band, the baseline model) and adds two more rungs above the process
environment: a per-workspace override and a "managed" override an extension
supplies (`tret_cloud`'s hosted product, for instance).

**The ladder, most specific first**::

    run_override > harness > workspace > managed > env > global_default

* `run_override` — an explicit value handed to one accounting call, e.g. the
  `grid_g_per_kwh` argument `energy_accounting()` has always accepted.
* `harness` — reserved for a future per-harness override. Nothing populates it
  yet; it is accepted and resolved today so the ladder does not need a second
  migration when something does.
* `workspace` — an operator's own override document for one workspace.
* `managed` — an override a hosting extension supplies (`tret_cloud`'s admin
  console, say). Named `managed:<source_name>` on the run so two managed
  layers are never confused for one.
* `env` — a `TRET_*` setting the operator actually set (via a real environment
  variable or `.env` file, detected through `Settings.model_fields_set`).
- `global_default` — the shipped constant, when nothing above chose otherwise.

Resolution is **per factor, not per document**: a workspace override that only
sets its grid factor still takes its PUE from `env`/`global_default`. Within one
document, `grid.providers[<provider>]` beats `grid.default`; across documents, a
more specific layer's *default* still beats a less specific layer's
*provider-specific* entry — a workspace default is more the operator's word for
this workspace than a managed layer's guess for one particular provider.

Nothing here computes carbon. `build_factor_set()` returns a `FactorSet` that
`energy_accounting()` reads for the numbers it has always used; every helper
this module builds on (`pue_for`, `resolve_grid_factor`, `embodied_g_for`,
`band_factors`) is unchanged and still reachable directly.

Imported lazily by `emissions.py` (inside the functions that need it), the
mirror image of how `emissions.py` itself imports the model catalog lazily:
this module imports plain functions from `emissions.py` at the top level, so
the load has to go this module -> emissions.py first, never the other way
at import time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tret.config import GridFactor, Settings, get_settings
from tret.services.emissions import (
    DEPLOYMENT_CLOUD,
    DEPLOYMENT_LOCAL,
    GRID_SOURCE_GLOBAL_DEFAULT,
    GRID_SOURCE_RUN_OVERRIDE,
    LOCAL_PUE_PROFILES,
    PUE_PROFILE_CLOUD,
    PUE_PROFILE_ONPREM,
    PUE_PROFILE_WORKSTATION,
    band_factors,
    deployment_for,
    embodied_g_for,
    pue_for,
    pue_profile_for,
    resolve_grid_factor,
)

# ── the ladder ────────────────────────────────────────────────────────────────
# `LAYER_RUN_OVERRIDE` is deliberately the same string as
# `emissions.GRID_SOURCE_RUN_OVERRIDE` ("run_override") — the two vocabularies
# describe the same thing for the grid factor, and keeping them textually equal
# means `factor_records()` can tell "this run's grid factor was a run override"
# without importing a second constant for it.
LAYER_RUN_OVERRIDE = "run_override"
LAYER_HARNESS = "harness"
LAYER_WORKSPACE = "workspace"
LAYER_MANAGED = "managed"
LAYER_ENV = "env"
LAYER_GLOBAL_DEFAULT = "global_default"

# Most specific first — the order every per-factor resolution below walks in.
LAYER_PRECEDENCE = (
    LAYER_RUN_OVERRIDE,
    LAYER_HARNESS,
    LAYER_WORKSPACE,
    LAYER_MANAGED,
    LAYER_ENV,
    LAYER_GLOBAL_DEFAULT,
)


# ── override documents ───────────────────────────────────────────────────────
def _iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"as_of must be an ISO date (YYYY-MM-DD), got {value!r}") from exc
    return text


class GridEntry(GridFactor):
    """One grid factor entry inside an override document: `grid.default` or one
    `grid.providers.<name>` entry. Same fields as `GridFactor` (`g_per_kwh`,
    `basis`, `label` — reusing its validators rather than duplicating them)
    plus an optional citation `url` and `as_of` date.
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    as_of: str | None = None

    @field_validator("as_of", mode="after")
    @classmethod
    def _valid_as_of(cls, value: str | None) -> str | None:
        return _iso_date(value)


class GridBlock(BaseModel):
    """`grid.default` and `grid.providers` in an override document."""

    model_config = ConfigDict(extra="forbid")

    default: GridEntry | None = None
    providers: dict[str, GridEntry] | None = None

    @model_validator(mode="after")
    def _labels_required(self) -> "GridBlock":
        if self.default is not None and not (self.default.label or "").strip():
            raise ValueError(
                "grid.default.label is required when grid.default.g_per_kwh is set"
            )
        for name, entry in (self.providers or {}).items():
            if not (entry.label or "").strip():
                raise ValueError(
                    f"grid.providers.{name}.label is required when "
                    f"grid.providers.{name}.g_per_kwh is set"
                )
        return self


class PueBlock(BaseModel):
    """`pue` in an override document. One `label` covers both `cloud` and
    `local` — an override document describes one operator's configuration,
    not two independent citations.
    """

    model_config = ConfigDict(extra="forbid")

    cloud: float | None = None
    local: float | None = None
    local_profile: str | None = None
    label: str | None = None

    @field_validator("cloud", "local", mode="after")
    @classmethod
    def _at_least_one(cls, value: float | None, info) -> float | None:
        if value is not None and value < 1.0:
            raise ValueError(f"pue.{info.field_name} must be >= 1.0, got {value}")
        return value

    @field_validator("local_profile", mode="after")
    @classmethod
    def _known_profile(cls, value: str | None) -> str | None:
        if value is None:
            return None
        profile = value.strip().lower()
        if profile not in LOCAL_PUE_PROFILES:
            raise ValueError(
                f"pue.local_profile must be one of {LOCAL_PUE_PROFILES}, got {value!r}"
            )
        return profile

    @model_validator(mode="after")
    def _label_required(self) -> "PueBlock":
        if (self.cloud is not None or self.local is not None) and not (self.label or "").strip():
            raise ValueError("pue.label is required when pue.cloud or pue.local is set")
        return self


class EmbodiedBlock(BaseModel):
    """`embodied` in an override document."""

    model_config = ConfigDict(extra="forbid")

    g_per_run: float | None = None
    label: str | None = None

    @field_validator("g_per_run", mode="after")
    @classmethod
    def _nonneg(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError(f"embodied.g_per_run must be >= 0, got {value}")
        return value

    @model_validator(mode="after")
    def _label_required(self) -> "EmbodiedBlock":
        if self.g_per_run is not None and not (self.label or "").strip():
            raise ValueError("embodied.label is required when embodied.g_per_run is set")
        return self


# The three ways `emissions.energy_constant_for_model` can price a token,
# resolved the same layered way as every other factor (see `_resolve_energy_strategy`).
# "class_ladder" is the shipped default; "active_params" opts into the EcoLogits
# formula for any model that carries `active_params_b`; "measured" is a config-time
# signal that per-model measured constants (`model_overrides`) are expected to be
# kept current for this workspace — it does not by itself change the arithmetic
# beyond what a `model_overrides` entry already would (see emissions.py).
EnergyStrategy = Literal["class_ladder", "active_params", "measured"]


class ModelOverride(BaseModel):
    """One entry in `model_overrides`: an operator-supplied per-token energy
    constant for a single catalog model id, replacing whatever
    `emissions.wh_per_mtok_for_model` would otherwise resolve (the model's own
    `energy_wh_per_mtok`, the active-parameter formula, or the class ladder)
    for that one model.

    `label` is required for exactly the reason `GridEntry`/`PueBlock`/etc.
    require one: a number with nowhere to say where it came from is worse than
    no override at all. `confidence` defaults to `"measured"` because the
    expected use is "I metered my own deployment"; `"calibrated"` and `"low"`
    are accepted for an operator who wants to record a less certain figure
    (someone else's benchmark, a vendor's own estimate) without it reading as
    a real measurement.
    """

    model_config = ConfigDict(extra="forbid")

    energy_wh_per_mtok: float
    label: str
    confidence: Literal["measured", "calibrated", "low"] = "measured"
    url: str | None = None
    as_of: str | None = None

    @field_validator("energy_wh_per_mtok", mode="after")
    @classmethod
    def _positive_finite(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError(
                f"model_overrides.<id>.energy_wh_per_mtok must be positive and finite, got {value!r}"
            )
        return value

    @field_validator("label", mode="after")
    @classmethod
    def _label_required(cls, value: str) -> str:
        if not (value or "").strip():
            raise ValueError("model_overrides.<id>.label is required")
        return value

    @field_validator("as_of", mode="after")
    @classmethod
    def _valid_as_of(cls, value: str | None) -> str | None:
        return _iso_date(value)


class BandBlock(BaseModel):
    """`band` in an override document. One `label` covers both bounds, for the
    same reason `PueBlock`'s does.
    """

    model_config = ConfigDict(extra="forbid")

    low: float | None = None
    high: float | None = None
    label: str | None = None

    @field_validator("low", "high", mode="after")
    @classmethod
    def _at_least_one(cls, value: float | None, info) -> float | None:
        if value is not None and value < 1.0:
            raise ValueError(f"band.{info.field_name} must be >= 1.0, got {value}")
        return value

    @model_validator(mode="after")
    def _label_required(self) -> "BandBlock":
        if (self.low is not None or self.high is not None) and not (self.label or "").strip():
            raise ValueError("band.label is required when band.low or band.high is set")
        return self


class EmissionsOverrides(BaseModel):
    """One operator-configured override document — a workspace's, a managed
    layer's, or (reserved, unpopulated today) a harness's. Every key is
    optional: a document that only sets `grid` leaves every other factor to
    fall through to the layers beneath it.

    `baseline_model` is recorded as a plain string and is **not** validated
    against the model catalog here — resolving it to an actual model, and
    rejecting an id the catalog does not know, stays the API's job (and
    `resolve_baseline_model`'s), same as it already is for
    `TRET_EMISSIONS_BASELINE_MODEL`.

    `source_name`, `updated_by` and `updated_at` are accepted so a document the
    API attaches audit metadata to still validates; `source_name` feeds the
    `managed:<source_name>` label a managed-layer win is recorded under.

    `energy_strategy` and `model_overrides` are resolved per model id, not per
    document, exactly like every other factor here — see
    `emissions.energy_constant_for_model` for how they change the arithmetic.
    `model_overrides` keys are catalog model ids (`"anthropic/claude-fable-5"`),
    validated here only as non-empty strings; rejecting an id the catalog does
    not know stays the API's job, same as `baseline_model`.
    """

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    grid: GridBlock | None = None
    pue: PueBlock | None = None
    embodied: EmbodiedBlock | None = None
    band: BandBlock | None = None
    baseline_model: str | None = None
    energy_strategy: EnergyStrategy | None = None
    model_overrides: dict[str, ModelOverride] | None = None
    source_name: str | None = None
    updated_by: str | None = None
    updated_at: str | None = None

    @field_validator("model_overrides", mode="after")
    @classmethod
    def _keys_are_model_ids(
        cls, value: dict[str, ModelOverride] | None
    ) -> dict[str, ModelOverride] | None:
        if value is None:
            return None
        for model_id in value:
            if not (model_id or "").strip():
                raise ValueError("model_overrides keys must be non-empty model ids")
        return value


def _parse_overrides(raw: dict[str, Any] | None) -> EmissionsOverrides | None:
    """`None`/`{}` -> `None` (that layer contributes nothing); otherwise
    validated, raising `pydantic.ValidationError` on anything wrong with it.
    """
    if not raw:
        return None
    return EmissionsOverrides(**raw)


# ── the result ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Resolved:
    """One factor's winning value and where it came from.

    `setting` is the one path that actually applied — a `TRET_*` env var name
    when `layer` is `env`/`global_default`, or a dotted override path
    (`workspace.emissions.grid.providers.anthropic`) otherwise — never a list
    of the paths that might have, mirroring what `resolve_grid_factor` already
    did for the grid factor alone.
    """

    value: Any
    layer: str
    source: str
    label: str | None
    url: str | None
    as_of: str | None
    setting: str | None


@dataclass(frozen=True)
class FactorSet:
    """Every accounting constant, resolved for one run, and where each one
    came from. `energy_accounting()` reads this instead of re-deriving each
    figure from `Settings` directly, so the arithmetic and the provenance it
    records cannot drift apart.
    """

    grid: Resolved  # value: Decimal, gCO2e/kWh
    grid_basis: str
    pue: Resolved  # value: Decimal
    pue_profile: str
    embodied_g: Resolved  # value: Decimal, grams
    band_low: Resolved  # value: Decimal
    band_high: Resolved  # value: Decimal
    baseline_model: Resolved  # value: str, "" means auto-select
    deployment: str
    provider: str | None
    # Which layers contributed *something* to this factor set, most specific
    # first — for a run's `factor_layers` field and for a quick "was anything
    # overridden at all" check without walking every factor.
    layers_present: tuple[str, ...] = ()
    # value: str, one of EnergyStrategy — how emissions.energy_constant_for_model
    # should price this run's tokens. Defaults to "class_ladder", the shipped
    # behaviour, at LAYER_GLOBAL_DEFAULT: there is no TRET_* env var for it (out
    # of scope for `tret/config.py` this phase), so this factor's ladder stops
    # one rung short of every other one — harness/workspace/managed/global_default,
    # never `env`.
    energy_strategy: Resolved = field(
        default_factory=lambda: Resolved(
            "class_ladder", LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT, None, None, None, None
        )
    )
    # The `model_overrides` entry for *this run's* model (by catalog id), from
    # the most specific layer that has one — or None when no layer configured
    # one for this model. Unlike every other factor, there is no shipped
    # default and no env rung: a model with no configured override simply uses
    # whatever `energy_strategy` and the catalog otherwise resolve.
    model_override: Resolved | None = None


# ── per-layer lookups ─────────────────────────────────────────────────────────
def _first_doc_hit(harness, workspace, managed, getter):
    """(layer, doc, hit) for the first of harness/workspace/managed whose
    `getter(doc)` returns something other than `None` — i.e. the most specific
    layer that set this particular factor.
    """
    for layer, doc in (
        (LAYER_HARNESS, harness),
        (LAYER_WORKSPACE, workspace),
        (LAYER_MANAGED, managed),
    ):
        if doc is None:
            continue
        hit = getter(doc)
        if hit is not None:
            return layer, doc, hit
    return None


def _managed_name(doc: EmissionsOverrides) -> str:
    return doc.source_name or "ext"


def _layer_source(layer: str, doc: EmissionsOverrides, provider_matched: str | None) -> str:
    """The `source` string for a harness/workspace/managed win.

    Bare (`workspace`) for that layer's own default; `<layer>:provider:<name>`
    for a per-provider entry; the managed layer additionally carries its own
    name (`managed:<source_name or 'ext'>`), so two managed layers are never
    confused for one on a stored run.
    """
    prefix = f"managed:{_managed_name(doc)}" if layer == LAYER_MANAGED else layer
    return f"{prefix}:provider:{provider_matched}" if provider_matched else prefix


def _env_or_global(field_is_decisive: bool, field_name: str, settings: Settings) -> str:
    """`env` if this Settings field is what decided the value and it was
    explicitly set (env var or `.env`, per `model_fields_set`); `global_default`
    if the shipped constant applied untouched. A rule that only ever fires when
    an operator *did* set something (a per-provider grid entry, the legacy
    local grid setting) is always `env` — there is no shipped default for it.
    """
    if not field_is_decisive:
        return LAYER_ENV
    return LAYER_ENV if field_name in settings.model_fields_set else LAYER_GLOBAL_DEFAULT


# ── grid ──────────────────────────────────────────────────────────────────────
def _grid_from_doc(doc: EmissionsOverrides, provider: str | None):
    if doc.grid is None:
        return None
    block = doc.grid
    if provider and block.providers and provider in block.providers:
        e = block.providers[provider]
        return (Decimal(str(e.g_per_kwh)), e.basis, e.label, e.url, e.as_of, provider)
    if block.default is not None:
        e = block.default
        return (Decimal(str(e.g_per_kwh)), e.basis, e.label, e.url, e.as_of, None)
    return None


def _resolve_grid(
    provider: str | None,
    deployment: str,
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> tuple[Resolved, str]:
    override = run_overrides.get("grid_g_per_kwh")
    if override is not None:
        return (
            Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                      None, None, None, None),
            "unspecified",
        )

    hit = _first_doc_hit(harness, workspace, managed, lambda d: _grid_from_doc(d, provider))
    if hit is not None:
        layer, doc, (value, basis, label, url, as_of, matched) = hit
        source = _layer_source(layer, doc, matched)
        setting = (
            f"{layer}.emissions.grid.providers.{matched}"
            if matched
            else f"{layer}.emissions.grid.default"
        )
        return Resolved(value, layer, source, label, url, as_of, setting), basis

    resolution = resolve_grid_factor(provider, deployment, settings, override=None)
    layer = _env_or_global(
        resolution["source"] == GRID_SOURCE_GLOBAL_DEFAULT, "grid_co2e_g_per_kwh", settings
    )
    resolved = Resolved(
        Decimal(str(resolution["value"])), layer, resolution["source"],
        resolution["label"], None, None, resolution["setting"],
    )
    return resolved, resolution["basis"]


# ── PUE ───────────────────────────────────────────────────────────────────────
def _effective_local_profile(
    deployment: str,
    settings: Settings,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> str:
    if deployment != DEPLOYMENT_LOCAL:
        return PUE_PROFILE_CLOUD
    for doc in (harness, workspace, managed):
        if doc is not None and doc.pue is not None and doc.pue.local_profile:
            return doc.pue.local_profile
    return pue_profile_for(deployment, settings)


_PUE_ENV_FIELD = {
    PUE_PROFILE_CLOUD: ("datacenter_pue", "TRET_DATACENTER_PUE"),
    PUE_PROFILE_WORKSTATION: ("local_pue", "TRET_LOCAL_PUE"),
    PUE_PROFILE_ONPREM: ("onprem_pue", "TRET_ONPREM_PUE"),
}


def _resolve_pue(
    deployment: str,
    profile: str,
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved:
    override = run_overrides.get("pue")
    if override is not None:
        return Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    key = "local" if deployment == DEPLOYMENT_LOCAL else "cloud"

    def _getter(doc: EmissionsOverrides):
        if doc.pue is None:
            return None
        value = getattr(doc.pue, key)
        return None if value is None else (Decimal(str(value)), doc.pue.label)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, (value, label) = hit
        source = _layer_source(layer, doc, None)
        return Resolved(value, layer, source, label, None, None, f"{layer}.emissions.pue.{key}")

    raw = pue_for(deployment, settings)
    field_name, env_name = _PUE_ENV_FIELD[profile]
    layer = LAYER_ENV if field_name in settings.model_fields_set else LAYER_GLOBAL_DEFAULT
    return Resolved(raw, layer, layer, None, None, None, env_name)


# ── embodied hardware ─────────────────────────────────────────────────────────
def _resolve_embodied(
    deployment: str,
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved:
    # Only self-hosted inference is ever amortized against the operator's own
    # hardware (see `embodied_g_for`) — no layer changes that, so a cloud run's
    # figure is unconditionally 0 with nothing to configure.
    if deployment != DEPLOYMENT_LOCAL:
        return Resolved(Decimal(0), LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT,
                         None, None, None, "TRET_EMBODIED_G_PER_RUN")

    override = run_overrides.get("embodied_g")
    if override is not None:
        return Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    def _getter(doc: EmissionsOverrides):
        if doc.embodied is None or doc.embodied.g_per_run is None:
            return None
        return (Decimal(str(doc.embodied.g_per_run)), doc.embodied.label)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, (value, label) = hit
        source = _layer_source(layer, doc, None)
        return Resolved(value, layer, source, label, None, None,
                         f"{layer}.emissions.embodied.g_per_run")

    raw = embodied_g_for(deployment, settings)
    layer = LAYER_ENV if "embodied_g_per_run" in settings.model_fields_set else LAYER_GLOBAL_DEFAULT
    return Resolved(raw, layer, layer, None, None, None, "TRET_EMBODIED_G_PER_RUN")


# ── uncertainty band ──────────────────────────────────────────────────────────
def _resolve_band_side(
    which: str,
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
    both: tuple[Decimal, Decimal],
) -> Resolved:
    override = run_overrides.get(f"band_{which}")
    if override is not None:
        return Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    def _getter(doc: EmissionsOverrides):
        if doc.band is None:
            return None
        value = getattr(doc.band, which)
        return None if value is None else (Decimal(str(value)), doc.band.label)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, (value, label) = hit
        source = _layer_source(layer, doc, None)
        return Resolved(value, layer, source, label, None, None, f"{layer}.emissions.band.{which}")

    raw = both[0] if which == "low" else both[1]
    field_name = f"uncertainty_band_{which}"
    env_name = f"TRET_UNCERTAINTY_BAND_{which.upper()}"
    layer = LAYER_ENV if field_name in settings.model_fields_set else LAYER_GLOBAL_DEFAULT
    return Resolved(raw, layer, layer, None, None, None, env_name)


# ── baseline model ────────────────────────────────────────────────────────────
def _resolve_baseline_model(
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved:
    if "baseline_model" in run_overrides:
        value = run_overrides["baseline_model"] or ""
        return Resolved(str(value), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    def _getter(doc: EmissionsOverrides):
        return doc.baseline_model

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, value = hit
        source = _layer_source(layer, doc, None)
        return Resolved(str(value), layer, source, None, None, None,
                         f"{layer}.emissions.baseline_model")

    value = settings.emissions_baseline_model or ""
    layer = LAYER_ENV if "emissions_baseline_model" in settings.model_fields_set else LAYER_GLOBAL_DEFAULT
    return Resolved(value, layer, layer, None, None, None, "TRET_EMISSIONS_BASELINE_MODEL")


# ── energy strategy ───────────────────────────────────────────────────────────
def _resolve_energy_strategy(
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved:
    """Which of `EnergyStrategy` this run should price tokens under.

    No `env` rung: there is no `TRET_*` setting for this factor (adding one is
    `tret/config.py`'s call, not this module's), so a run with no
    harness/workspace/managed layer simply gets the shipped
    `"class_ladder"` default straight from `LAYER_GLOBAL_DEFAULT` — one rung
    shorter than every other factor in this file.
    """
    override = run_overrides.get("energy_strategy")
    if override is not None:
        return Resolved(str(override), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    def _getter(doc: EmissionsOverrides):
        return doc.energy_strategy

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, value = hit
        source = _layer_source(layer, doc, None)
        return Resolved(str(value), layer, source, None, None, None,
                         f"{layer}.emissions.energy_strategy")

    return Resolved("class_ladder", LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT,
                     None, None, None, None)


# ── per-model energy override ─────────────────────────────────────────────────
def _resolve_model_override(
    model_id: str | None,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved | None:
    """The `model_overrides.<model_id>` entry for this run's model, from the
    most specific layer that has one — `None` when no layer configured one for
    this exact model id (a different model's override never applies here, and
    there is no per-provider or default fallback the way grid/pue have).
    """
    if not model_id:
        return None

    def _getter(doc: EmissionsOverrides):
        if not doc.model_overrides:
            return None
        return doc.model_overrides.get(model_id)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is None:
        return None
    layer, doc, entry = hit
    source = _layer_source(layer, doc, None)
    return Resolved(entry, layer, source, entry.label, entry.url, entry.as_of,
                     f"{layer}.emissions.model_overrides.{model_id}")


# ── the entry point ───────────────────────────────────────────────────────────
def build_factor_set(
    *,
    provider: str | None,
    settings: Settings | None = None,
    workspace_settings: dict[str, Any] | None = None,
    managed_settings: dict[str, Any] | None = None,
    harness_settings: dict[str, Any] | None = None,
    run_overrides: dict[str, Any] | None = None,
    model_id: str | None = None,
) -> FactorSet:
    """Resolve every accounting constant for one run, layer by layer.

    `workspace_settings`, `managed_settings` and `harness_settings` are each a
    dict in the `EmissionsOverrides` shape, validated here (a `None` or `{}`
    contributes nothing — indistinguishable from omitting it). `harness_settings`
    is a reserved layer: accepted and resolved like the others, but nothing
    populates it yet.

    `run_overrides` carries explicit per-run values that outrank every
    configured layer: `grid_g_per_kwh` (what `energy_accounting`'s own
    `grid_g_per_kwh` argument has always meant), and optionally `pue`,
    `embodied_g`, `band_low`, `band_high`, `baseline_model`, `energy_strategy`.
    A key that is absent (or `None`, for every key but `baseline_model` — `""`
    there means "use auto-select for this run specifically") falls through to
    the layers beneath it.

    Resolution happens **per factor**: a `workspace_settings` document that
    only sets `grid` still takes its `pue`, `embodied`, `band` and
    `baseline_model` from `managed_settings` / the environment / the shipped
    default, whichever is the most specific layer that actually set them.

    `model_id` — the catalog id of the model this run is (or will be) using —
    is what `model_overrides` is resolved against; omitted (the default), no
    layer's `model_overrides` can ever match and `model_override` on the
    returned `FactorSet` is always `None`.
    """
    settings = settings or get_settings()
    run_overrides = run_overrides or {}
    deployment = deployment_for(provider) if provider else DEPLOYMENT_CLOUD

    harness = _parse_overrides(harness_settings)
    workspace = _parse_overrides(workspace_settings)
    managed = _parse_overrides(managed_settings)

    profile = _effective_local_profile(deployment, settings, harness, workspace, managed)

    grid, grid_basis = _resolve_grid(
        provider, deployment, settings, run_overrides, harness, workspace, managed
    )
    pue = _resolve_pue(deployment, profile, settings, run_overrides, harness, workspace, managed)
    embodied_g = _resolve_embodied(deployment, settings, run_overrides, harness, workspace, managed)
    both = band_factors(settings)
    band_low = _resolve_band_side(
        "low", settings, run_overrides, harness, workspace, managed, both
    )
    band_high = _resolve_band_side(
        "high", settings, run_overrides, harness, workspace, managed, both
    )
    baseline_model = _resolve_baseline_model(settings, run_overrides, harness, workspace, managed)
    energy_strategy = _resolve_energy_strategy(run_overrides, harness, workspace, managed)
    model_override = _resolve_model_override(model_id, harness, workspace, managed)

    won = {grid.layer, pue.layer, embodied_g.layer, band_low.layer, band_high.layer,
           baseline_model.layer, energy_strategy.layer}
    if model_override is not None:
        won.add(model_override.layer)
    layers_present = tuple(layer for layer in LAYER_PRECEDENCE if layer in won)

    return FactorSet(
        grid=grid,
        grid_basis=grid_basis,
        pue=pue,
        pue_profile=profile,
        embodied_g=embodied_g,
        band_low=band_low,
        band_high=band_high,
        baseline_model=baseline_model,
        deployment=deployment,
        provider=provider,
        layers_present=layers_present,
        energy_strategy=energy_strategy,
        model_override=model_override,
    )
