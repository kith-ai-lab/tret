"""A resolved "factor set": every emissions constant, layered and traced.

`tret/services/emissions.py` already knows how to pick one grid factor out of
three sources (a run override, `TRET_GRID_FACTORS`, the global default) and
records *which rule* won on every run. This module generalises that pattern to
every accounting constant (grid intensity, PUE, embodied hardware, the
uncertainty band, the baseline model) and adds two more rungs above the process
environment: a per-workspace override and a "managed" override an extension
supplies (`tret_cloud`'s hosted product, for instance).

**The ladder, most specific first**::

    run_override > harness > workspace > managed > env > dataset > global_default

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
* `dataset` — grid factor only: a published annual zone average from the
  bundled Electricity Maps table (`tret.services.grid_zones`), reached only
  when a workspace has pinned the run's provider to a region and *nothing an
  operator set* — no document above, no `TRET_*` grid setting — priced that
  provider. It beats only the shipped global default: pinning a region says
  where the load ran, not that the operator's own figure (or its GHG
  Protocol basis) should be discarded. Nothing infers a region.
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

import hashlib
import logging
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from tret.config import GridFactor, Settings, get_settings
from tret.services.embodied_profiles import (
    EmbodiedProfile,
    ProfileError,
    grams_per_run,
    profile_from_dict,
    profile_summary,
)
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
from tret.services.grid_regions import (
    PROVIDER_KEY_RE,
    ProviderKeyError,
    first_matching_entry,
    validate_regions,
)
from tret.services.grid_tables import GridTable, GridTableError, parse_grid_table
from tret.services.grid_zones import grid_entry_for_region
from tret.services.grid_ember import entry_for_region as ember_entry_for_region

logger = logging.getLogger(__name__)

# Cached by a content hash of (csv_text, label, basis): a document's table CSV
# text never changes between calls (only the *reference* to `at` does), so
# parsing it once per distinct CSV and reusing the `GridTable` on every
# subsequent `build_factor_set` call — including a hot loop like the what-if
# endpoint's, which builds many factor sets over the same handful of
# documents — costs nothing beyond the first hit. Also doubles as the parse
# that validates a `grid.tables.<name>.csv` entry when an `EmissionsOverrides`
# document is constructed (`GridBlock._tables_valid` below).
#
# Bounded by total CSV characters resident, not by entry count: an entry-count
# `functools.lru_cache(maxsize=256)` (what this replaced) could retain up to
# 256 * `_MAX_TABLE_CSV_CHARS` (~150MB before the per-document caps below
# existed at all, and worse once a single document could carry many tables) of
# parsed tables at once — a process-global cache with no relationship to how
# much CSV was ever actually configured. `_GridTableCache` caps the *content*
# instead: `_GRID_TABLE_CACHE_MAX_CHARS` total CSV characters across every
# cached table, oldest evicted first once a new parse would exceed it. A
# failed parse (`GridTableError`) is never cached — the caller re-parses (and
# re-fails, identically) every time, matching `functools.lru_cache`'s own
# behaviour of never memoizing a raised exception.
#
# Read this as CSV characters, not resident bytes: a parsed `GridTable` costs
# roughly 8x its CSV text in Python heap (measured), so this 16,000,000-char
# cap holds not 16MB but on the order of 130MB of actual `GridTable` objects
# once the cache is full.
_GRID_TABLE_CACHE_MAX_CHARS = 16_000_000


class _GridTableCacheInfo(NamedTuple):
    hits: int
    misses: int
    total_chars: int  # CSV characters currently resident, across every entry


class _GridTableCache:
    def __init__(self, max_chars: int) -> None:
        self._max_chars = max_chars
        self._entries: OrderedDict[str, tuple[GridTable, int]] = OrderedDict()
        self._total_chars = 0
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()

    @staticmethod
    def _key(csv_text: str, label: str, basis: str) -> str:
        digest = hashlib.sha256()
        for part in (label, basis, csv_text):
            digest.update(part.encode("utf-8", "surrogatepass"))
            digest.update(b"\x00")
        return digest.hexdigest()

    def __call__(self, csv_text: str, label: str, basis: str) -> GridTable:
        key = self._key(csv_text, label, basis)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None:
                self._entries.move_to_end(key)
                self._hits += 1
                return hit[0]
        # Parsed outside the lock: `parse_grid_table` touches no shared
        # state, and a slow parse should not hold the lock against concurrent
        # lookups of unrelated keys. A raised `GridTableError` propagates from
        # here straight to the caller — never caught, never cached.
        table = parse_grid_table(csv_text, label=label, basis=basis)
        size = len(csv_text)
        with self._lock:
            self._misses += 1
            if key not in self._entries:
                self._entries[key] = (table, size)
                self._total_chars += size
                while self._total_chars > self._max_chars and self._entries:
                    _, (_, evicted_size) = self._entries.popitem(last=False)
                    self._total_chars -= evicted_size
            else:
                # Lost a race with another thread parsing the identical key —
                # keep the one already stored rather than double-count it.
                self._entries.move_to_end(key)
        return table

    def cache_info(self) -> _GridTableCacheInfo:
        with self._lock:
            return _GridTableCacheInfo(self._hits, self._misses, self._total_chars)

    def cache_clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._total_chars = 0
            self._hits = 0
            self._misses = 0


_cached_grid_table = _GridTableCache(_GRID_TABLE_CACHE_MAX_CHARS)


_TABLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_TABLE_CSV_CHARS = 600_000
# B1: caps on `grid.tables` as a whole, not just per table — an unbounded
# *number* of tables (each individually under `_MAX_TABLE_CSV_CHARS`) still
# lets one document carry an arbitrarily large combined CSV payload, which is
# what actually drove the measured cost (a multi-second validation, repeated
# every what-if run against the same document) this pair of caps closes off.
_MAX_TABLES_PER_DOCUMENT = 8
_MAX_TABLES_COMBINED_CSV_CHARS = 2_000_000

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
LAYER_DATASET = "dataset"
LAYER_ENV = "env"
LAYER_GLOBAL_DEFAULT = "global_default"

# Most specific first — the order every per-factor resolution below walks in.
LAYER_PRECEDENCE = (
    LAYER_RUN_OVERRIDE,
    LAYER_HARNESS,
    LAYER_WORKSPACE,
    LAYER_MANAGED,
    LAYER_ENV,
    LAYER_DATASET,
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
    plus an optional citation `url` and `as_of` date, and an optional `table`
    naming an hourly table (a key into this same document's `grid.tables`)
    whose value stands in for `g_per_kwh` at a run's actual start time —
    `g_per_kwh` remains required and is the fallback when the table has no
    value for that hour (see `_apply_grid_table`).
    """

    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    as_of: str | None = None
    table: str | None = None

    @field_validator("as_of", mode="after")
    @classmethod
    def _valid_as_of(cls, value: str | None) -> str | None:
        return _iso_date(value)


class GridTableEntry(BaseModel):
    """One named entry in `grid.tables`: an operator-pasted CSV of grid
    carbon intensity by hour, parsed and validated once here (see
    `GridBlock._tables_valid`) so a malformed table is rejected when the
    document is written, not the first time a run needs it. `label` and
    `basis` describe the table the same way `GridEntry`'s do; `csv` is the
    raw text handed to `parse_grid_table` (never fetched — always
    operator-supplied).
    """

    model_config = ConfigDict(extra="forbid")

    label: str
    basis: str
    csv: str
    url: str | None = None
    as_of: str | None = None

    @field_validator("as_of", mode="after")
    @classmethod
    def _valid_as_of(cls, value: str | None) -> str | None:
        return _iso_date(value)


class GridBlock(BaseModel):
    """`grid.default`, `grid.providers`, `grid.regions` and `grid.tables` in
    an override document.

    `providers` keys may be a bare provider (`"anthropic"`) or a provider
    pinned to a region (`"anthropic@us-east"`, see
    `tret.services.grid_regions`); `regions` is a separate `{provider:
    region}` map an operator uses to pin a provider to a region for lookup
    purposes without necessarily having a `provider@region` entry in this
    same layer — see `_resolve_grid` for how the two interact across layers.
    """

    model_config = ConfigDict(extra="forbid")

    default: GridEntry | None = None
    providers: dict[str, GridEntry] | None = None
    regions: dict[str, str] | None = None
    tables: dict[str, GridTableEntry] | None = None

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

    @model_validator(mode="after")
    def _provider_keys_valid(self) -> "GridBlock":
        for key in self.providers or {}:
            if not PROVIDER_KEY_RE.match(key):
                raise ValueError(
                    f"grid.providers.{key} is not a valid provider key "
                    "(expected 'provider' or 'provider@region')"
                )
        return self

    @model_validator(mode="after")
    def _regions_valid(self) -> "GridBlock":
        if self.regions:
            try:
                self.regions = validate_regions(self.regions)
            except ProviderKeyError as exc:
                raise ValueError(f"grid.regions: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _tables_valid(self) -> "GridBlock":
        tables = self.tables or {}
        if len(tables) > _MAX_TABLES_PER_DOCUMENT:
            raise ValueError(
                f"grid.tables: at most {_MAX_TABLES_PER_DOCUMENT} tables per document"
            )
        combined = 0
        for name, entry in tables.items():
            if not _TABLE_NAME_RE.match(name):
                raise ValueError(
                    f"grid.tables key {name!r} must match "
                    "^[a-z0-9][a-z0-9_-]{0,63}$"
                )
            if len(entry.csv) > _MAX_TABLE_CSV_CHARS:
                raise ValueError(
                    f"grid.tables.{name}.csv exceeds {_MAX_TABLE_CSV_CHARS} characters"
                )
            combined += len(entry.csv)
        if combined > _MAX_TABLES_COMBINED_CSV_CHARS:
            raise ValueError(
                f"grid.tables: combined csv exceeds {_MAX_TABLES_COMBINED_CSV_CHARS} characters"
            )
        for name, entry in tables.items():
            try:
                _cached_grid_table(entry.csv, entry.label, entry.basis)
            except GridTableError as exc:
                raise ValueError(f"grid.tables.{name}.csv: {exc}") from exc
        return self

    @model_validator(mode="after")
    def _table_refs_valid(self) -> "GridBlock":
        tables = self.tables or {}
        if self.default is not None and self.default.table is not None:
            table_name = self.default.table
            if table_name not in tables:
                raise ValueError(
                    f"grid.default.table references unknown table {table_name!r}"
                )
            table = tables[table_name]
            if table.basis != self.default.basis:
                raise ValueError(
                    f"grid.default.table {table_name!r} has basis {table.basis} "
                    f"but the entry is {self.default.basis}"
                )
        for name, entry in (self.providers or {}).items():
            if entry.table is not None:
                if entry.table not in tables:
                    raise ValueError(
                        f"grid.providers.{name}.table references unknown table "
                        f"{entry.table!r}"
                    )
                table = tables[entry.table]
                if table.basis != entry.basis:
                    raise ValueError(
                        f"grid.providers.{name}.table {entry.table!r} has basis "
                        f"{table.basis} but the entry is {entry.basis}"
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
    upstreams: dict[str, "PueDisclosure"] | None = None

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


class PueDisclosure(BaseModel):
    """An upstream operator's disclosed fleet-average PUE."""

    model_config = ConfigDict(extra="forbid")
    value: float
    label: str
    url: str
    as_of: str
    evidence_type: Literal["provider_asserted"] = "provider_asserted"
    statistic: Literal["operating_fleet_average"] = "operating_fleet_average"

    @field_validator("value", mode="after")
    @classmethod
    def _valid_value(cls, value: float) -> float:
        if not math.isfinite(value) or value < 1:
            raise ValueError("upstream PUE must be finite and >= 1")
        return value

    @field_validator("as_of", mode="after")
    @classmethod
    def _valid_date(cls, value: str) -> str:
        return _iso_date(value) or ""


class EmbodiedProfileBlock(BaseModel):
    """`embodied.profile` in an override document: the same fields as
    `tret.services.embodied_profiles.EmbodiedProfile`, validated through
    `profile_from_dict` (`_valid` below) so the two can never drift apart —
    this class only supplies the pydantic shape (required keys, `extra`
    forbidden); every actual constraint (gpu count, lifetime, known GPU
    model) lives in `EmbodiedProfile.__post_init__`.
    """

    model_config = ConfigDict(extra="forbid")

    gpus: int
    runs_over_lifetime: int
    batch_size: int = 64
    include_server: bool = True
    gpu_model: str = "h100"
    label: str | None = None

    @model_validator(mode="after")
    def _valid(self) -> "EmbodiedProfileBlock":
        try:
            profile_from_dict(self.model_dump())
        except ProfileError as exc:
            raise ValueError(f"embodied.profile: {exc}") from exc
        return self

    def to_profile(self) -> EmbodiedProfile:
        return profile_from_dict(self.model_dump())


class EmbodiedBlock(BaseModel):
    """`embodied` in an override document. Either `g_per_run` (a flat figure)
    or `profile` (a described hardware setup `grams_per_run` computes the
    figure from) may be set — never both, since each is a complete answer to
    "what does this run's embodied carbon cost" on its own.
    """

    model_config = ConfigDict(extra="forbid")

    g_per_run: float | None = None
    profile: EmbodiedProfileBlock | None = None
    label: str | None = None

    @field_validator("g_per_run", mode="after")
    @classmethod
    def _nonneg(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError(f"embodied.g_per_run must be >= 0, got {value}")
        return value

    @model_validator(mode="after")
    def _one_or_other(self) -> "EmbodiedBlock":
        if self.g_per_run is not None and self.profile is not None:
            raise ValueError("embodied: set g_per_run or profile, not both")
        return self

    @model_validator(mode="after")
    def _label_required(self) -> "EmbodiedBlock":
        has_value = self.g_per_run is not None or self.profile is not None
        has_label = bool((self.label or "").strip()) or bool(
            self.profile is not None and (self.profile.label or "").strip()
        )
        if has_value and not has_label:
            raise ValueError(
                "embodied.label is required when embodied.g_per_run or "
                "embodied.profile is set"
            )
        return self


# The three ways `emissions.energy_constant_for_model` can price a token,
# resolved the same layered way as every other factor (see `_resolve_energy_strategy`).
# "class_ladder" is the shipped default; "active_params" opts into the EcoLogits
# formula for any model that carries `active_params_b`; "measured" is a config-time
# signal that per-model measured constants (`model_overrides`) are expected to be
# kept current for this workspace — it does not by itself change the arithmetic
# beyond what a `model_overrides` entry already would (see emissions.py).
EnergyStrategy = Literal[
    "class_ladder", "class_ladder_v1", "class_ladder_v2", "active_params", "measured"
]


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

    `derived`, when true, switches this run's headline band from the plain
    configured `low`/`high` to one narrowed by whatever evidence the run
    actually has — see `_resolve_band_derived` and
    `tret.services.uncertainty_derivation`.
    """

    model_config = ConfigDict(extra="forbid")

    low: float | None = None
    high: float | None = None
    label: str | None = None
    derived: bool | None = None

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


def _parse_overrides(
    raw: dict[str, Any] | EmissionsOverrides | None,
) -> EmissionsOverrides | None:
    """`None`/`{}` -> `None` (that layer contributes nothing); a dict is
    validated, raising `pydantic.ValidationError` on anything wrong with it —
    exactly as before. An already-validated `EmissionsOverrides` instance
    (B1: a caller that validates once per request/run rather than once per
    `build_factor_set` call — the what-if endpoint over many runs, the
    harness over many segments/model switches) is returned as-is, skipping
    re-validation entirely.
    """
    if raw is None:
        return None
    if isinstance(raw, EmissionsOverrides):
        return raw
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
    # ── Phase 3, all additive: every existing positional construction of
    # `Resolved` (7 args) still works unchanged; these only ever arrive by
    # keyword, from the one resolver that fills them in.
    # Grid only: the region actually applied to this win (the part after "@"
    # in a matched `provider@region` key), or None when no region pinned this
    # particular value (see `_resolve_grid`/`grid_regions.py`).
    region: str | None = None
    requested_region: str | None = None
    region_resolution_status: str | None = None
    # Grid only: "annual_average" (no table, or `at` wasn't given, or the
    # table had no value for `at`) or "hourly" (the table had one) — see
    # `_apply_grid_table`. Left None on every non-grid factor.
    temporal: str | None = None
    # Grid only: the name of the `grid.tables` entry the winning grid entry
    # referenced, or None if it referenced none. Populated even when
    # `temporal` stayed "annual_average" (no `at`, or a miss), so a GET
    # response computed without `at` can still say "table X will apply at
    # run time".
    table: str | None = None
    # Grid only: `GridTable.summary()` for `table`, or None when no table was
    # referenced.
    table_summary: dict | None = None
    # Grid only: True when a table was referenced, `at` was given, and the
    # table had no value within its lookup gap — the entry's own `g_per_kwh`
    # applied instead (temporal stays "annual_average").
    table_miss: bool = False
    # Embodied only: `profile_summary(...)` when the winning embodied value
    # came from a named hardware profile, else None.
    profile: dict | None = None
    # Band only (mirrored onto both `band_low` and `band_high`): whether this
    # run's headline uncertainty band is evidence-derived rather than the
    # plain configured low/high — see `_resolve_band_derived`.
    derived: bool = False
    factor_boundary: str = "unknown"
    gas_coverage: str = "unknown"
    gwp_horizon_years: int | None = None
    gwp_assessment_basis: str = "unknown"
    includes_td_losses: bool | None = None
    electricity_mix_basis: str = "unknown"
    dataset_version: str | None = None
    observation_year: int | None = None
    disclosure: dict | None = None


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
            "class_ladder_v2", LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT, None, None, None, None
        )
    )
    # The `model_overrides` entry for *this run's* model (by catalog id), from
    # the most specific layer that has one — or None when no layer configured
    # one for this model. Unlike every other factor, there is no shipped
    # default and no env rung: a model with no configured override simply uses
    # whatever `energy_strategy` and the catalog otherwise resolve.
    model_override: Resolved | None = None
    # Inputs retained solely so a counterfactual model can resolve through the
    # exact same ladder (including region/hourly data) at the same instant.
    # Excluded from equality/repr to preserve the public value-object contract.
    resolution_context: "FactorResolutionContext | None" = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class FactorResolutionContext:
    settings_values: tuple
    settings_fields_set: tuple[str, ...]
    workspace_settings: tuple | None
    managed_settings: tuple | None
    harness_settings: tuple | None
    run_overrides: tuple
    at: datetime | None
    interval_end: datetime | None


# Observed OpenRouter endpoint slugs that report a hosting identity distinct
# from the model's own provider — never model brands, and never the direct
# (non-OpenRouter) provider name: `anthropic` is deliberately absent, because
# the direct Anthropic API is not hosted by either upstream disclosure below.
UPSTREAM_PUE_ALIASES = {
    "google-vertex": "google",
    "amazon-bedrock": "aws",
}


_CONTEXT_SETTINGS_FIELDS = {
    "grid_co2e_g_per_kwh", "grid_co2e_basis", "grid_factors",
    "local_grid_co2e_g_per_kwh", "local_grid_co2e_basis",
    "datacenter_pue", "local_pue", "onprem_pue", "local_deployment_profile",
    "embodied_g_per_run", "uncertainty_band_low", "uncertainty_band_high",
    "emissions_baseline_model",
}


def _freeze(value: Any):
    if isinstance(value, dict):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value):
    if isinstance(value, tuple):
        if all(isinstance(item, tuple) and len(item) == 2 and isinstance(item[0], str) for item in value):
            return {key: _thaw(item) for key, item in value}
        return [_thaw(item) for item in value]
    return value


def _context_settings(context: FactorResolutionContext) -> Settings:
    settings = Settings(**_thaw(context.settings_values))
    object.__setattr__(settings, "__pydantic_fields_set__", set(context.settings_fields_set))
    return settings


def context_with_run_overrides(
    context: FactorResolutionContext, updates: dict[str, Any]
) -> FactorResolutionContext:
    values = _thaw(context.run_overrides)
    values.update(updates)
    return replace(context, run_overrides=_freeze(values))


def factor_set_for_model(
    factors: FactorSet, *, provider: str | None, model_id: str | None
) -> FactorSet | None:
    """Re-resolve ``factors`` for another model through its original ladder.

    Manually constructed legacy ``FactorSet`` objects have no context.  They
    return ``None`` rather than pretending their provider-specific wins apply
    to a counterfactual provider.
    """
    context = factors.resolution_context
    if context is None:
        return None
    return build_factor_set(
        provider=provider,
        settings=_context_settings(context),
        workspace_settings=_thaw(context.workspace_settings),
        managed_settings=_thaw(context.managed_settings),
        harness_settings=_thaw(context.harness_settings),
        run_overrides=_thaw(context.run_overrides),
        model_id=model_id,
        at=context.at,
        interval_end=context.interval_end,
    )


def factor_set_for_call(
    factors: FactorSet,
    *,
    provider: str | None,
    model_id: str | None,
    served_by: str | None,
    at: datetime | None = None,
    interval_end: datetime | None = None,
) -> FactorSet | None:
    """Replay a segment's immutable factor context for one upstream call.

    Only the exact supported ``served_by`` identity can select an upstream
    disclosure. Geography remains evidence and is never used for resolution.
    Manually constructed factor sets have no replayable context and return None.
    """
    context = factors.resolution_context
    if context is None:
        return None
    return build_factor_set(
        provider=provider,
        settings=_context_settings(context),
        workspace_settings=_thaw(context.workspace_settings),
        managed_settings=_thaw(context.managed_settings),
        harness_settings=_thaw(context.harness_settings),
        run_overrides=_thaw(context.run_overrides),
        model_id=model_id,
        at=at if at is not None else context.at,
        interval_end=(interval_end if interval_end is not None else context.interval_end),
        served_by=served_by,
    )


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
def _resolve_grid_region(
    provider: str | None,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> str | None:
    """The region pinned to `provider`, from the most specific layer that
    pins one — resolved once, per factor, exactly like every other value in
    this module, then handed to `_grid_from_doc` so it applies to the
    provider-entry lookup in *every* layer, not only the layer that pinned
    it (see `GridBlock.regions`'s docstring and this module's own docstring
    on cross-document precedence).
    """
    if not provider:
        return None
    for doc in (harness, workspace, managed):
        if doc is None or doc.grid is None or not doc.grid.regions:
            continue
        region = doc.grid.regions.get(provider)
        if region:
            return region
    return None


def _grid_from_doc(doc: EmissionsOverrides, provider: str | None, region: str | None):
    if doc.grid is None:
        return None
    block = doc.grid
    if provider and block.providers:
        hit = first_matching_entry(
            block.providers, provider, {provider: region} if region else None
        )
        if hit is not None:
            key, e = hit
            return (Decimal(str(e.g_per_kwh)), e.basis, e.label, e.url, e.as_of, key, e.table, e)
    if block.default is not None:
        e = block.default
        return (Decimal(str(e.g_per_kwh)), e.basis, e.label, e.url, e.as_of, None, e.table, e)
    return None


def _apply_grid_table(
    value: Decimal,
    basis: str,
    table_name: str | None,
    doc: EmissionsOverrides,
    at: datetime | None,
    interval_end: datetime | None = None,
) -> tuple[Decimal, str, str, str | None, dict | None, bool]:
    """`(value, basis, temporal, table, table_summary, table_miss)` for a
    winning grid entry that may reference an hourly table.

    A no-op — `(value, basis, "annual_average", None, None, False)` — when
    the entry named no table. Otherwise the table is parsed once (cached by
    its own CSV text, see `_cached_grid_table`) and:

    * `at` is `None` — annual: `(value, basis, "annual_average", table_name,
      summary, False)`. The table's name and summary are still returned so a
      caller computing "what would apply" without a run's actual start time
      (the settings API's GET) can still say "an hourly table is configured
      here".
    * `at` is given and the table has a value for it — `(table_value,
      table's own basis, "hourly", table_name, summary, False)`.
    * `at` is given and the table has no value for it (a `series` table with
      a gap past `max_gap`) — the entry's own value applies, unchanged:
      `(value, basis, "annual_average", table_name, summary, True)`.
    """
    if table_name is None:
        return value, basis, "annual_average", None, None, False
    entry = (doc.grid.tables or {})[table_name]  # existence checked at parse time
    table = _cached_grid_table(entry.csv, entry.label, entry.basis)
    summary = table.summary()
    if at is None:
        return value, basis, "annual_average", table_name, summary, False
    if interval_end is not None:
        mean, coverage = table.average(at, interval_end, fallback=value)
        summary = {
            **summary,
            "coverage_fraction": coverage,
            "uniform_power_assumption": True,
        }
        if coverage < 1 and entry.basis != basis:
            summary["interval_fallback_reason"] = "partial_coverage_incompatible_basis"
            return value, basis, "annual_average", table_name, summary, True
        return mean, entry.basis, "interval_weighted", table_name, summary, coverage < 1
    hit = table.lookup(at)
    if hit is not None:
        return hit, entry.basis, "hourly", table_name, summary, False
    return value, basis, "annual_average", table_name, summary, True


def _resolve_grid(
    provider: str | None,
    deployment: str,
    settings: Settings,
    run_overrides: dict,
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
    at: datetime | None = None,
    interval_end: datetime | None = None,
) -> tuple[Resolved, str]:
    override = run_overrides.get("grid_g_per_kwh")
    if override is not None:
        return (
            Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                      None, None, None, None, temporal="annual_average"),
            "unspecified",
        )

    region = _resolve_grid_region(provider, harness, workspace, managed)
    hit = _first_doc_hit(
        harness, workspace, managed, lambda d: _grid_from_doc(d, provider, region)
    )
    if hit is not None:
        layer, doc, (value, basis, label, url, as_of, matched, table_name, entry_meta) = hit
        source = _layer_source(layer, doc, matched)
        setting = (
            f"{layer}.emissions.grid.providers.{matched}"
            if matched
            else f"{layer}.emissions.grid.default"
        )
        region_used = matched.split("@", 1)[1] if matched and "@" in matched else None
        value, basis, temporal, table, table_summary, table_miss = _apply_grid_table(
            value, basis, table_name, doc, at, interval_end
        )
        resolved = Resolved(
            value, layer, source, label, url, as_of, setting,
            region=region_used, temporal=temporal, table=table,
            table_summary=table_summary, table_miss=table_miss,
            factor_boundary=entry_meta.factor_boundary,
            gas_coverage=entry_meta.gas_coverage,
            gwp_horizon_years=entry_meta.gwp_horizon_years,
            gwp_assessment_basis=entry_meta.gwp_assessment_basis,
            includes_td_losses=entry_meta.includes_td_losses,
            electricity_mix_basis=entry_meta.electricity_mix_basis,
            dataset_version=entry_meta.dataset_version,
            observation_year=entry_meta.observation_year,
        )
        return resolved, basis

    resolution = resolve_grid_factor(provider, deployment, settings, override=None)
    layer = _env_or_global(
        resolution["source"] == GRID_SOURCE_GLOBAL_DEFAULT, "grid_co2e_g_per_kwh", settings
    )

    # The `dataset` rung: a pinned region that nothing an operator set priced
    # — no document above, no `TRET_*` grid setting — resolves against the
    # bundled zone table (see `tret.services.grid_zones`). It displaces only
    # the shipped global default: an env figure keeps both its value and its
    # GHG Protocol basis, because a region pin says where the load ran, not
    # that the operator's own factor should go. Only ever reached *because*
    # an operator pinned a region; a pinned region the table has no zone for,
    # or a table that cannot be read at all, falls through unchanged.
    if region and layer == LAYER_GLOBAL_DEFAULT:
        try:
            ember_hit = ember_entry_for_region(region)
        except (OSError, ValueError) as exc:
            logger.warning("bundled ember grid table unavailable (%s); using %s", exc, layer)
            ember_hit = None
        if ember_hit is not None:
            iso3, entry = ember_hit
            return Resolved(
                Decimal(str(entry["g_per_kwh"])), LAYER_DATASET, f"dataset:ember:country-{iso3}",
                entry["label"], entry["url"], entry["as_of"],
                f"dataset.grid_ember.country-{iso3}", region=region,
                requested_region=region, region_resolution_status="resolved",
                temporal="annual_average",
                factor_boundary=entry["factor_boundary"], gas_coverage=entry["gas_coverage"],
                gwp_horizon_years=entry["gwp_horizon_years"],
                gwp_assessment_basis=entry["gwp_assessment_basis"],
                includes_td_losses=entry["includes_td_losses"],
                electricity_mix_basis=entry["electricity_mix_basis"],
                dataset_version=entry["dataset_version"], observation_year=entry["observation_year"],
            ), entry["basis"]
        try:
            zone_hit = grid_entry_for_region(region)
        except (OSError, ValueError) as exc:
            logger.warning("bundled grid zone table unavailable (%s); using %s", exc, layer)
            zone_hit = None
        if zone_hit is not None:
            zone, entry = zone_hit
            resolved = Resolved(
                Decimal(str(entry["g_per_kwh"])), LAYER_DATASET, f"dataset:zone:{zone}",
                entry["label"], entry["url"], entry["as_of"], f"dataset.grid_zones.{zone}",
                region=region, requested_region=region, region_resolution_status="resolved",
                temporal="annual_average",
                factor_boundary=entry["factor_boundary"],
                gas_coverage=entry["gas_coverage"],
                gwp_horizon_years=entry["gwp_horizon_years"],
                gwp_assessment_basis=entry["gwp_assessment_basis"],
                includes_td_losses=entry["includes_td_losses"],
                electricity_mix_basis=entry["electricity_mix_basis"],
                dataset_version=entry["dataset_version"],
                observation_year=entry["observation_year"],
            )
            return resolved, entry["basis"]

    resolved = Resolved(
        Decimal(str(resolution["value"])), layer, resolution["source"],
        resolution["label"], None, None, resolution["setting"],
        temporal="annual_average",
        factor_boundary=("lifecycle_electricity_generation" if layer == LAYER_GLOBAL_DEFAULT else "unknown"),
        gas_coverage=("co2e" if layer == LAYER_GLOBAL_DEFAULT else "unknown"),
        gwp_horizon_years=(100 if layer == LAYER_GLOBAL_DEFAULT else None),
        gwp_assessment_basis="unknown",
        includes_td_losses=None,
        electricity_mix_basis=("production" if layer == LAYER_GLOBAL_DEFAULT else "unknown"),
        dataset_version=("ember-yearly-2026-release" if layer == LAYER_GLOBAL_DEFAULT else None),
        observation_year=(2025 if layer == LAYER_GLOBAL_DEFAULT else None),
        requested_region=region,
        region_resolution_status=("fallback_unknown_region" if region else None),
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
    served_by: str | None,
) -> Resolved:
    override = run_overrides.get("pue")
    if override is not None:
        return Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    key = "local" if deployment == DEPLOYMENT_LOCAL else "cloud"

    def _getter(doc: EmissionsOverrides):
        if doc.pue is None:
            return None
        if served_by and doc.pue.upstreams and served_by in doc.pue.upstreams:
            disclosure = doc.pue.upstreams[served_by]
            return (
                Decimal(str(disclosure.value)),
                disclosure.label,
                disclosure.model_dump(),
                f"upstreams.{served_by}",
            )
        value = getattr(doc.pue, key)
        return None if value is None else (Decimal(str(value)), doc.pue.label, None, key)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, (value, label, disclosure, setting_key) = hit
        source = _layer_source(layer, doc, None)
        return Resolved(
            value, layer, source, label,
            disclosure.get("url") if disclosure else None,
            disclosure.get("as_of") if disclosure else None,
            f"{layer}.emissions.pue.{setting_key}",
            disclosure=disclosure,
        )

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
    override = run_overrides.get("embodied_g")
    if override is not None:
        return Resolved(Decimal(str(override)), LAYER_RUN_OVERRIDE, GRID_SOURCE_RUN_OVERRIDE,
                         None, None, None, None)

    def _getter(doc: EmissionsOverrides):
        if doc.embodied is None:
            return None
        block = doc.embodied
        if block.g_per_run is not None:
            return (Decimal(str(block.g_per_run)), block.label, None, "g_per_run")
        if block.profile is not None:
            profile = block.profile.to_profile()
            return (
                grams_per_run(profile),
                block.label or profile.label,
                profile_summary(profile),
                "profile",
            )
        return None

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, (value, label, profile_dict, which) = hit
        source = _layer_source(layer, doc, None)
        return Resolved(
            value, layer, source, label, None, None,
            f"{layer}.emissions.embodied.{which}",
            profile=profile_dict,
        )

    # Cloud hardware is unknown by default. Keep the historical numeric zero
    # for compatibility, but only after all explicit run/document layers have
    # had a chance to supply a supported allocation.
    if deployment != DEPLOYMENT_LOCAL:
        return Resolved(Decimal(0), LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT,
                         None, None, None, "TRET_EMBODIED_G_PER_RUN")

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


def _resolve_band_derived(
    harness: EmissionsOverrides | None,
    workspace: EmissionsOverrides | None,
    managed: EmissionsOverrides | None,
) -> Resolved:
    """Whether this run's headline band should be evidence-derived
    (`BandBlock.derived`), resolved the same layered way as `low`/`high` —
    but as its own factor, since a document may set `band.derived` without
    setting `low`/`high` at all (narrowing the shipped defaults rather than
    a configured band). No `run_override` or `env` rung: there is no
    per-run or `TRET_*` way to ask for this, so an unconfigured run gets the
    shipped `False` straight from `LAYER_GLOBAL_DEFAULT`.
    """

    def _getter(doc: EmissionsOverrides):
        if doc.band is None or doc.band.derived is None:
            return None
        return bool(doc.band.derived)

    hit = _first_doc_hit(harness, workspace, managed, _getter)
    if hit is not None:
        layer, doc, value = hit
        source = _layer_source(layer, doc, None)
        return Resolved(value, layer, source, None, None, None,
                         f"{layer}.emissions.band.derived", derived=value)
    return Resolved(False, LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT,
                     None, None, None, None, derived=False)


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

    return Resolved("class_ladder_v2", LAYER_GLOBAL_DEFAULT, LAYER_GLOBAL_DEFAULT,
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
    workspace_settings: dict[str, Any] | EmissionsOverrides | None = None,
    managed_settings: dict[str, Any] | EmissionsOverrides | None = None,
    harness_settings: dict[str, Any] | EmissionsOverrides | None = None,
    run_overrides: dict[str, Any] | None = None,
    model_id: str | None = None,
    at: datetime | None = None,
    interval_end: datetime | None = None,
    served_by: str | None = None,
) -> FactorSet:
    """Resolve every accounting constant for one run, layer by layer.

    `workspace_settings`/`managed_settings`/`harness_settings` each accept
    either a raw dict (validated here, same as always) or an already-validated
    `EmissionsOverrides` instance (B1: validate once per request/run and reuse
    the instance across every `build_factor_set` call that shares it, instead
    of re-validating — including re-parsing every `grid.tables` CSV entry's
    shape — on every single call. A hot caller with many calls sharing one
    document (the what-if endpoint over a window of runs, the harness over
    one run's segments) is exactly who this matters for; a caller with one
    document per call may keep passing a plain dict unchanged.

    `at` — the run's actual start time, timezone-aware — is what an hourly
    `grid.tables` entry is looked up against (see `_apply_grid_table`); a
    naive `at` raises `ValueError` rather than silently guessing a UTC
    offset. Omitted (the default), any winning grid entry that names a table
    resolves to its own annual `g_per_kwh` (`temporal="annual_average"`) —
    this is what the settings API's GET (computed without a specific run)
    uses, so it can still say a table is configured without claiming a hint
    of which hour's value would apply.

    A `None` or `{}` (dict form) contributes nothing — indistinguishable from
    omitting it. `harness_settings` is a reserved layer: accepted and resolved
    like the others, but nothing populates it yet.

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
    if at is not None and at.tzinfo is None:
        raise ValueError("build_factor_set(at=...) requires a timezone-aware datetime")
    if interval_end is not None:
        if interval_end.tzinfo is None:
            raise ValueError("build_factor_set(interval_end=...) requires a timezone-aware datetime")
        if at is None or interval_end <= at:
            raise ValueError("interval_end requires at and must be later than at")

    settings = settings or get_settings()
    run_overrides = run_overrides or {}
    deployment = deployment_for(provider) if provider else DEPLOYMENT_CLOUD

    harness = _parse_overrides(harness_settings)
    workspace = _parse_overrides(workspace_settings)
    managed = _parse_overrides(managed_settings)

    profile = _effective_local_profile(deployment, settings, harness, workspace, managed)

    grid, grid_basis = _resolve_grid(
        provider, deployment, settings, run_overrides, harness, workspace, managed,
        at=at, interval_end=interval_end,
    )
    # OpenRouter's reviewed canonical identity for cloud-hosted calls (see
    # `UPSTREAM_PUE_ALIASES`). OpenRouter slugs can carry a region suffix
    # (e.g. `google-vertex/eu`), so the alias lookup keys off just the first
    # path segment; `served_by` itself — the raw slug — stays untouched and
    # is what call provenance records.
    pue_upstream_key = served_by.split("/", 1)[0] if served_by else served_by
    pue_served_by = UPSTREAM_PUE_ALIASES.get(pue_upstream_key, pue_upstream_key)
    pue = _resolve_pue(
        deployment, profile, settings, run_overrides, harness, workspace, managed,
        pue_served_by if pue_served_by in {"aws", "google"} else None,
    )
    embodied_g = _resolve_embodied(deployment, settings, run_overrides, harness, workspace, managed)
    both = band_factors(settings)
    band_low = _resolve_band_side(
        "low", settings, run_overrides, harness, workspace, managed, both
    )
    band_high = _resolve_band_side(
        "high", settings, run_overrides, harness, workspace, managed, both
    )
    band_derived = _resolve_band_derived(harness, workspace, managed)
    band_low = replace(band_low, derived=band_derived.value)
    band_high = replace(band_high, derived=band_derived.value)
    baseline_model = _resolve_baseline_model(settings, run_overrides, harness, workspace, managed)
    energy_strategy = _resolve_energy_strategy(run_overrides, harness, workspace, managed)
    model_override = _resolve_model_override(model_id, harness, workspace, managed)

    won = {grid.layer, pue.layer, embodied_g.layer, band_low.layer, band_high.layer,
           band_derived.layer, baseline_model.layer, energy_strategy.layer}
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
        resolution_context=FactorResolutionContext(
            settings_values=_freeze(settings.model_dump(include=_CONTEXT_SETTINGS_FIELDS)),
            settings_fields_set=tuple(sorted(settings.model_fields_set & _CONTEXT_SETTINGS_FIELDS)),
            workspace_settings=_freeze(workspace.model_dump()) if workspace else None,
            managed_settings=_freeze(managed.model_dump()) if managed else None,
            harness_settings=_freeze(harness.model_dump()) if harness else None,
            run_overrides=_freeze(run_overrides),
            at=at,
            interval_end=interval_end,
        ),
    )
