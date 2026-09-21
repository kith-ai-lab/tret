"""Opt-in, aggregate-only anonymous telemetry (shared contract:
`/private/tmp/claude-501/.../scratchpad/telemetry-contract.md`, sections 0-6).

Off by default. With telemetry not enabled this module never opens a socket
and holds no instance id — see `resolve_state` for the full precedence chain
(contract §1) and `tret/net/policy.py`'s `telemetry` egress class for the one
network path this module is ever allowed to use. `build_payload` builds the
POST body field by field (never a serialized model dump of anything else),
and `preview`/`send_once` share that exact same path so what an admin sees in
`GET /preview` is byte-for-byte what `send_once` would POST.

`tret/api/telemetry.py` is the thin `Depends(require_admin)` HTTP wrapper
around `status`/`preview`/`set_enabled` below. `sender_loop` is the one
background task (started next to the catalog warm task in `tret/main.py`'s
lifespan) that ever calls `send_once` on its own.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import random
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import and_, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from tret import __version__
from tret.config import get_settings
from tret.db.models import InstanceState, Pack, Run, User, Workspace, WorkspaceConnection
from tret.engine.extensions import telemetry_forced_off
from tret.net import CLASS_TELEMETRY, effective_mode
from tret.net.client import open_client
from tret.net.policy import MODE_ON
from tret.services.emission_factors import LAYER_PRECEDENCE
from tret.services.emissions import GRID_SOURCE_RULES

# Imported rather than copied — this is the SAME per-run reading the emissions
# analytics uses (contract §3's "energy_wh/co2e_g" rule), so a change to what
# counts as "this run's estimate" can never drift between the two callers.
# Lazy-imported inside build_payload only if this top-level import turns out
# to cycle (tret.api.analytics does not import anything under tret.services
# today, so it does not).
from tret.api.analytics import _recorded_emissions

log = logging.getLogger("tret.telemetry")

# The literal placeholder `preview()` reports for an instance id that has not
# been minted yet. Never a real value, never sent (preview never mints, and
# the ingest Worker rejects it outright — contract §3).
PREVIEW_PLACEHOLDER = "(minted when enabled)"

RECENT_CAP = 10
WINDOW_DEFAULT_DAYS = 7  # first send: last 7 days
WINDOW_MAX_DAYS = 35  # contract §3: window length capped at 35 days

# S4: a permanently-failing collector must not make the instance re-POST
# every sender tick forever — "due" also requires the last *attempt* (sent or
# not) to be stale, checked on its own, shorter cadence than the 7-day send
# window.
ATTEMPT_RETRY_HOURS = 24

# S9: the ingest Worker's own `DB_RE` (telemetry-ingest/src/schema.js) —
# `_db_label` must never be able to produce anything outside this, and the
# `TelemetryPayload.db` field_validator below is the backstop that keeps it
# that way even if a future `_db_label` change forgets.
_DB_LABEL_RE = re.compile(r"^(postgres-[0-9]{1,3}|sqlite)$")

# Closed set of grid-factor "rungs" the emissions code can actually emit —
# the rule-half of `grid_co2e_source` before its first ":" (`"provider:anthropic"`
# -> `"provider"`, `"managed:<name>:provider:x"` -> `"managed"`,
# `"workspace:provider:x@region"` -> `"workspace"`). Union of
# `tret.services.emissions.GRID_SOURCE_RULES` (the pre-factor-layers rules:
# run_override, provider, local_setting, global_default) and
# `tret.services.emission_factors.LAYER_PRECEDENCE` (the FactorSet ladder:
# run_override, harness, workspace, managed, env, dataset, global_default) —
# nine rungs total. Anything else folds to "other"; a missing/null source
# folds to "legacy". The ingest Worker (telemetry-ingest/src/schema.js)
# enforces this exact same nine-rung set and 400s the whole report for any
# other key, so this must not drift from either source constant.
FACTOR_RUNGS = frozenset(GRID_SOURCE_RULES) | frozenset(LAYER_PRECEDENCE)

# ── Payload v1 (contract §3) ─────────────────────────────────────────────────

ProviderKey = Literal["anthropic", "kimi", "openrouter", "local", "other"]
FamilyKey = Literal[
    "claude", "gpt", "gemini", "gemma", "llama", "mistral", "qwen", "deepseek",
    "kimi", "grok", "phi", "command", "local/other", "other",
]
TaskTypeKey = Literal["chat", "freeform", "pack"]
RunStatusKey = Literal[
    "queued", "running", "completed", "completed_without_output", "failed", "cancelled", "other",
]
FactorRungKey = Literal[
    "run_override", "harness", "workspace", "managed", "env", "dataset",
    "provider", "local_setting", "global_default", "legacy", "other",
]
DeployKey = Literal["docker", "fly", "render", "bare"]
SmallBucketKey = Literal["0", "1", "2-5", "6-20", "21-100", "100+"]
RunsBucketKey = Literal["0", "1-10", "11-100", "101-1000", "1001-10000", "10000+"]


class TelemetryFeatures(BaseModel):
    """Exactly the four booleans contract §3 names — nothing else."""

    model_config = ConfigDict(extra="forbid")

    packs: bool
    connections: bool
    delegation: bool
    local_models: bool


class TelemetryPayload(BaseModel):
    """The ONLY body ever POSTed to `telemetry_url` (contract §3).

    `extra="forbid"` is the allowlist: a field that is not explicitly listed
    here cannot be sent even by accident. Closed enums/buckets are `Literal`
    types on purpose, so the JSON schema this model produces (see
    `tests/test_telemetry.py`'s schema snapshot) documents the exact set of
    values a receiver ever has to handle.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    instance_id: str
    window_start: str
    window_end: str
    tret_version: str
    deploy: DeployKey
    db: str
    users_bucket: SmallBucketKey
    workspaces_bucket: SmallBucketKey
    runs_bucket: RunsBucketKey
    tokens_in: int
    tokens_out: int
    providers: dict[ProviderKey, float]
    model_families: dict[FamilyKey, float]
    task_types: dict[TaskTypeKey, float]
    run_status: dict[RunStatusKey, float]
    energy_wh: float | None
    co2e_g: float | None
    factor_rungs: dict[FactorRungKey, float]
    features: TelemetryFeatures

    @field_validator("instance_id")
    @classmethod
    def _instance_id_shape(cls, value: str) -> str:
        """A real uuid4 string, or the preview placeholder — nothing else.

        The ingest Worker independently rejects the placeholder if it is ever
        sent for real; this validator is the local backstop that keeps a
        malformed or non-uuid4 id from ever reaching `model_dump_json()`.
        """
        if value == PREVIEW_PLACEHOLDER:
            return value
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("instance_id must be a uuid4 string") from exc
        if parsed.version != 4:
            raise ValueError("instance_id must be a uuid4 string")
        return value

    @field_validator("db")
    @classmethod
    def _db_shape(cls, value: str) -> str:
        """Same closed shape the ingest Worker's `DB_RE` enforces
        (`telemetry-ingest/src/schema.js`) — `postgres-<major>` or `sqlite`,
        nothing else, so a malformed `_db_label` fallback (a bare "postgres",
        say) is caught here rather than 400ing the whole report."""
        if not _DB_LABEL_RE.fullmatch(value):
            raise ValueError("db must be postgres-<major> or sqlite")
        return value


# ── enablement state (contract §1) ───────────────────────────────────────────


@dataclass
class TelemetryState:
    enabled: bool
    env_mode: str
    db_enabled: bool
    locked_reason: str | None

    @property
    def locked(self) -> bool:
        return self.locked_reason is not None


class TelemetryLocked(Exception):
    """Raised by `set_enabled` when the effective state cannot be changed
    from the DB toggle — carries the same `locked_reason` `resolve_state`
    would report, so a caller never has to re-derive why."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"telemetry is locked ({reason})")


async def _get_value(db: AsyncSession, key: str, default):
    row = await db.get(InstanceState, key)
    return row.value if row is not None else default


async def _set_value(db: AsyncSession, key: str, value) -> None:
    row = await db.get(InstanceState, key)
    if row is None:
        db.add(InstanceState(key=key, value=value))
    else:
        row.value = value
    await db.flush()


async def _delete_value(db: AsyncSession, key: str) -> None:
    row = await db.get(InstanceState, key)
    if row is not None:
        await db.delete(row)
        await db.flush()


async def resolve_state(db: AsyncSession) -> TelemetryState:
    """Effective state, contract §1's precedence order exactly — first match
    wins. Read-only: never mints or deletes the instance id (see
    `set_enabled` and `send_once` for the two places that do)."""
    settings = get_settings()
    env_mode = settings.telemetry
    db_enabled = bool(await _get_value(db, "telemetry_enabled", False))

    # 1. DO_NOT_TRACK — read from os.environ at call time, not a Settings
    # field: it is the ambient convention every tool checks the same way.
    dnt = os.environ.get("DO_NOT_TRACK", "")
    if dnt.strip().lower() not in ("", "0", "false"):
        return TelemetryState(False, env_mode, db_enabled, "do_not_track")
    # 2. extension override (tret-cloud forces this unconditionally)
    if telemetry_forced_off():
        return TelemetryState(False, env_mode, db_enabled, "extension")
    # 3. operator-locked off
    if env_mode == "off":
        return TelemetryState(False, env_mode, db_enabled, "env_off")
    # 4. nowhere to send it
    if not (settings.telemetry_url or "").strip():
        return TelemetryState(False, env_mode, db_enabled, "no_url")
    # 5. egress class `telemetry` must be in force — ask the policy module
    # rather than re-deriving master-switch-and-class-mode logic here.
    if effective_mode(CLASS_TELEMETRY, settings) != MODE_ON:
        return TelemetryState(False, env_mode, db_enabled, "egress_off")
    # 6. operator-locked on
    if env_mode == "on":
        return TelemetryState(True, env_mode, db_enabled, "env_on")
    # 7. admin decides via the DB toggle
    return TelemetryState(db_enabled, env_mode, db_enabled, None)


async def set_enabled(db: AsyncSession, enabled: bool) -> TelemetryState:
    """The admin/CLI toggle. Raises `TelemetryLocked` when the effective
    state cannot be changed from here (contract §6's PUT 409). Enabling
    mints a new uuid4 id; disabling deletes it and keeps `telemetry_recent`
    (local-only log). Commits.

    B1: a disable attempt while locked OFF for a reason other than `env_on`
    (do_not_track, extension, env_off, no_url, egress_off) is let through as
    a no-op that still clears a stale id, instead of raising — an admin
    turning telemetry off through the UI/CLI while, say, DO_NOT_TRACK is set
    must still be able to clear an id minted before the lock took effect.
    `env_on` still always raises (an operator-forced-on instance cannot be
    turned off from the DB toggle at all), and so does every *enable*
    attempt while locked, same as before.
    """
    state = await resolve_state(db)
    if state.locked:
        if enabled or state.locked_reason == "env_on":
            raise TelemetryLocked(state.locked_reason)
        await _delete_value(db, "telemetry_instance_id")
        await db.commit()
        return await resolve_state(db)
    await _set_value(db, "telemetry_enabled", bool(enabled))
    if enabled:
        existing = await _get_value(db, "telemetry_instance_id", None)
        if not existing:
            await _set_value(db, "telemetry_instance_id", str(uuid.uuid4()))
    else:
        await _delete_value(db, "telemetry_instance_id")
    await db.commit()
    return await resolve_state(db)


async def status(db: AsyncSession) -> dict:
    """Contract §6's `GET /api/admin/telemetry` shape."""
    state = await resolve_state(db)
    settings = get_settings()
    return {
        "enabled": state.enabled,
        "env_mode": state.env_mode,
        "db_enabled": state.db_enabled,
        "locked": state.locked,
        "locked_reason": state.locked_reason,
        "instance_id": await _get_value(db, "telemetry_instance_id", None),
        "last_sent_at": await _get_value(db, "telemetry_last_sent_at", None),
        "endpoint": settings.telemetry_url,
        "recent": await _get_value(db, "telemetry_recent", []),
    }


# ── payload construction (contract §3) ───────────────────────────────────────


def _bucket_small(n: int) -> str:
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 5:
        return "2-5"
    if n <= 20:
        return "6-20"
    if n <= 100:
        return "21-100"
    return "100+"


def _bucket_runs(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 10:
        return "1-10"
    if n <= 100:
        return "11-100"
    if n <= 1000:
        return "101-1000"
    if n <= 10000:
        return "1001-10000"
    return "10000+"


def _sig2(x: float) -> float:
    """Round `x` to 2 significant figures. 0 stays 0.

    N11: total — a non-finite (NaN/inf) or absurdly-scaled input (a
    subnormal like `1e-320`, whose 2-sig-fig scale factor overflows a float)
    returns 0.0 rather than raising. One run's malformed stored figure must
    never take the whole payload build down.
    """
    try:
        x = float(x)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(x) or x == 0:
        return 0.0
    try:
        digits = math.ceil(math.log10(abs(x)))
        power = 2 - digits
        factor = 10.0**power
        result = round(x * factor) / factor
    except (ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _sig2_int(n: int) -> int:
    return int(round(_sig2(float(n))))


# First substring match wins, in the order the contract specifies. `mixtral`
# and `moonshot` are aliases folded into `mistral`/`kimi` rather than their
# own keys.
_FAMILY_ORDER: tuple[tuple[str, str], ...] = (
    ("claude", "claude"),
    ("gpt", "gpt"),
    ("gemini", "gemini"),
    ("gemma", "gemma"),
    ("llama", "llama"),
    ("mistral", "mistral"),
    ("mixtral", "mistral"),
    ("qwen", "qwen"),
    ("deepseek", "deepseek"),
    ("kimi", "kimi"),
    ("moonshot", "kimi"),
    ("grok", "grok"),
    ("phi", "phi"),
    ("command", "command"),
)


def _family(model_used: str | None, provider_used: str | None) -> str:
    name = (model_used or "").lower()
    for substr, family in _FAMILY_ORDER:
        if substr in name:
            return family
    # "local", not "local/other", would collide with the ingest Worker's own
    # closed set (telemetry-ingest/src/schema.js's MODEL_FAMILIES_SET), which
    # 400s the whole report on an unknown key.
    return "local/other" if provider_used == "local" else "other"


_KNOWN_PROVIDERS = frozenset({"anthropic", "kimi", "openrouter", "local"})


def _provider_bucket(provider_used: str | None) -> str:
    p = (provider_used or "").lower()
    return p if p in _KNOWN_PROVIDERS else "other"


def _task_type_bucket(task_type: str | None) -> str:
    t = (task_type or "").lower()
    return t if t in ("chat", "freeform") else "pack"


_KNOWN_STATUSES = frozenset(
    {"queued", "running", "completed", "completed_without_output", "failed", "cancelled"}
)


def _status_bucket(status_value: str | None) -> str:
    s = (status_value or "").lower()
    return s if s in _KNOWN_STATUSES else "other"


def _rung(grid_co2e_source: str | None) -> str:
    if not grid_co2e_source:
        return "legacy"
    rule = grid_co2e_source.split(":", 1)[0]
    return rule if rule in FACTOR_RUNGS else "other"


def _deploy() -> str:
    if os.environ.get("FLY_APP_NAME"):
        return "fly"
    if os.environ.get("RENDER"):
        return "render"
    if Path("/.dockerenv").exists():
        return "docker"
    return "bare"


async def _db_label(db: AsyncSession) -> str:
    dialect = db.get_bind().dialect.name
    if dialect != "postgresql":
        return "sqlite"
    raw = (await db.execute(text("SHOW server_version_num"))).scalar()
    try:
        major = int(str(raw)) // 10000
    except (TypeError, ValueError):
        # S9: never bare "postgres" — the ingest Worker's DB_RE (and this
        # model's own `_db_shape` validator) rejects that, 400ing the whole
        # report. "postgres-0" is unparseable-but-shaped.
        return "postgres-0"
    return f"postgres-{major}"


async def _packs_flag(db: AsyncSession) -> bool:
    """S8: `features.packs` = at least one OPERATOR-installed (non-built-in)
    pack, not merely "some task_type is a pack" (that conflates a built-in
    pack every install ships with — see `TRET_SEED_DEFAULT_PACKS` — with an
    operator actually installing something of their own).

    `Pack` carries no built-in/operator column to query directly
    (tret/db/models.py). This infers it from `source_path` instead:
    `install_pack` (tret/packs/loader.py, called by
    `_install_configured_packs` for every pack auto-seeded from
    `TRET_PACKS_DIR`) leaves `source_path` pointing straight at that
    directory, while an archive install repoints it under
    `storage_dir/packs/<id>` (`install_pack_from_archive`) and an
    operator-supplied path-install points wherever they named — both land
    outside every `TRET_PACKS_DIR` root. Global, not window-scoped, same as
    `connections_flag` just below: "has a pack" is a fact about the
    deployment, not about the report window. If a future schema adds a real
    built-in/operator column, prefer it over this inference.
    """
    settings = get_settings()
    builtin_roots: list[Path] = []
    for raw_root in settings.packs_dir.split(":"):
        raw_root = raw_root.strip()
        if not raw_root:
            continue
        try:
            builtin_roots.append(Path(raw_root).resolve())
        except OSError:
            continue
    source_paths = (await db.execute(select(Pack.source_path))).scalars().all()
    for source_path in source_paths:
        if not source_path:
            return True
        try:
            resolved = Path(source_path).resolve()
        except OSError:
            return True
        if not any(resolved == root or root in resolved.parents for root in builtin_roots):
            return True
    return False


def _share_map(counts: dict[str, int], total: int) -> dict[str, float]:
    """Share of `total` each key accounts for, rounded to 2 decimals, with
    zero-share keys omitted (contract §3).

    S6: independently rounding each key can push the map's sum past 1.0 (the
    ingest Worker 400s the whole report above 1.05) — after the per-key
    rounding above, pull any excess off the single largest share, round that
    again, and drop it entirely if the excess reaches or exceeds it.
    """
    if total <= 0:
        return {}
    out: dict[str, float] = {}
    for key, count in counts.items():
        share = round(count / total, 2)
        if share > 0:
            out[key] = share
    while out:
        excess = round(sum(out.values()) - 1.0, 2)
        if excess <= 0:
            break
        largest_key = max(out, key=out.get)
        adjusted = round(out[largest_key] - excess, 2)
        if adjusted <= 0:
            del out[largest_key]
            continue
        out[largest_key] = adjusted
        break
    return out


async def build_payload(
    db: AsyncSession, *, instance_id: str, now: datetime | None = None
) -> TelemetryPayload:
    now = now or datetime.now(timezone.utc)

    last_sent_raw = await _get_value(db, "telemetry_last_sent_at", None)
    if last_sent_raw:
        start_dt = datetime.fromisoformat(last_sent_raw)
        if start_dt.tzinfo is None:
            start_dt = start_dt.replace(tzinfo=timezone.utc)
    else:
        start_dt = now - timedelta(days=WINDOW_DEFAULT_DAYS)
    if now - start_dt > timedelta(days=WINDOW_MAX_DAYS):
        start_dt = now - timedelta(days=WINDOW_MAX_DAYS)
    if start_dt > now:
        start_dt = now

    window = and_(Run.created_at >= start_dt, Run.created_at < now)

    runs_total = await db.scalar(select(func.count()).select_from(Run).where(window)) or 0

    provider_rows = (
        await db.execute(select(Run.provider_used, func.count()).where(window).group_by(Run.provider_used))
    ).all()
    provider_counts: dict[str, int] = {}
    for provider_used, count in provider_rows:
        key = _provider_bucket(provider_used)
        provider_counts[key] = provider_counts.get(key, 0) + count

    # Grouped raw in SQL (model_used, provider_used); folded to families in
    # Python — a custom/private model name must never itself reach the
    # payload, only the family it folds into.
    model_rows = (
        await db.execute(
            select(Run.model_used, Run.provider_used, func.count())
            .where(window)
            .group_by(Run.model_used, Run.provider_used)
        )
    ).all()
    family_counts: dict[str, int] = {}
    for model_used, provider_used, count in model_rows:
        key = _family(model_used, provider_used)
        family_counts[key] = family_counts.get(key, 0) + count

    task_rows = (
        await db.execute(select(Run.task_type, func.count()).where(window).group_by(Run.task_type))
    ).all()
    task_counts: dict[str, int] = {}
    for task_type, count in task_rows:
        key = _task_type_bucket(task_type)
        task_counts[key] = task_counts.get(key, 0) + count

    status_rows = (
        await db.execute(select(Run.status, func.count()).where(window).group_by(Run.status))
    ).all()
    status_counts: dict[str, int] = {}
    for status_value, count in status_rows:
        key = _status_bucket(status_value)
        status_counts[key] = status_counts.get(key, 0) + count

    tokens_in, tokens_out = (
        await db.execute(
            select(
                func.coalesce(func.sum(Run.input_tokens), 0),
                func.coalesce(func.sum(Run.output_tokens), 0),
            ).where(window)
        )
    ).one()

    # Energy/CO2e: streamed in batches rather than one big fetch — a window
    # can hold many thousands of runs and `energy_accounting` is a JSONB blob
    # per row. `_recorded_emissions` is imported from tret.api.analytics, the
    # SAME per-run reading the emissions analytics uses: each Run's own
    # `energy_accounting` records only that run's OWN compute, never its
    # delegated children's (the same split `delegated_cost_usd` keeps `cost_usd`
    # from double-counting on Run — see that column's docstring), so summing
    # straight across every row in the window, parent and child runs alike,
    # never double-counts a delegation tree.
    energy_total = Decimal(0)
    co2e_total = Decimal(0)
    has_energy = False
    has_co2e = False
    rung_counts: dict[str, int] = {}
    co2e_bases: set = set()  # S5: distinct non-null grid_co2e_basis values seen
    # N10: keyset pagination on `Run.id` rather than OFFSET/LIMIT — OFFSET
    # makes Postgres re-walk and discard every row before it on each batch
    # (quadratic over a window with many thousand runs); a `Run.id > last_id`
    # filter reuses the same `runs_created_at`-adjacent index-friendly scan
    # each time and never re-reads what a prior batch already counted.
    last_id = None
    batch_size = 1000
    while True:
        stmt = select(Run.id, Run.energy_accounting).where(window).order_by(Run.id).limit(batch_size)
        if last_id is not None:
            stmt = stmt.where(Run.id > last_id)
        batch = (await db.execute(stmt)).all()
        if not batch:
            break
        for run_id, accounting in batch:
            source = accounting.get("grid_co2e_source") if isinstance(accounting, dict) else None
            rung_key = _rung(source)
            rung_counts[rung_key] = rung_counts.get(rung_key, 0) + 1
            rec = _recorded_emissions(accounting)
            if rec is not None:
                if rec.get("energy_wh") is not None:
                    energy_total += rec["energy_wh"]
                    has_energy = True
                if rec.get("co2e_g") is not None:
                    co2e_total += rec["co2e_g"]
                    has_co2e = True
                basis = rec.get("grid_co2e_basis")
                if basis is not None:
                    co2e_bases.add(basis)
        last_id = batch[-1][0]
        if len(batch) < batch_size:
            break

    # S5: `co2e_g` is a straight sum across every run in the window, but the
    # product's own analytics (`_rollup_emissions` in tret/api/analytics.py)
    # refuses to sum carbon across differing GHG Protocol bases — summing a
    # location-based and a market-based figure produces a number that is not
    # honestly anything. `energy_wh` has no such basis and stays populated.
    mixed_basis = len(co2e_bases) > 1

    packs_flag = await _packs_flag(db)
    connections_flag = (await db.scalar(select(WorkspaceConnection.id).limit(1))) is not None
    delegation_flag = (
        await db.scalar(select(Run.id).where(window, Run.parent_run_id.isnot(None)).limit(1))
    ) is not None
    local_models_flag = (
        await db.scalar(select(Run.id).where(window, Run.provider_used == "local").limit(1))
    ) is not None

    users_total = await db.scalar(select(func.count()).select_from(User)) or 0
    workspaces_total = await db.scalar(select(func.count()).select_from(Workspace)) or 0

    data = {
        "schema_version": 1,
        "instance_id": instance_id,
        "window_start": start_dt.date().isoformat(),
        "window_end": now.date().isoformat(),
        "tret_version": __version__,
        "deploy": _deploy(),
        "db": await _db_label(db),
        "users_bucket": _bucket_small(users_total),
        "workspaces_bucket": _bucket_small(workspaces_total),
        "runs_bucket": _bucket_runs(runs_total),
        "tokens_in": _sig2_int(int(tokens_in or 0)),
        "tokens_out": _sig2_int(int(tokens_out or 0)),
        "providers": _share_map(provider_counts, runs_total),
        "model_families": _share_map(family_counts, runs_total),
        "task_types": _share_map(task_counts, runs_total),
        "run_status": _share_map(status_counts, runs_total),
        "energy_wh": _sig2(float(energy_total)) if has_energy else None,
        "co2e_g": _sig2(float(co2e_total)) if has_co2e and not mixed_basis else None,
        "factor_rungs": _share_map(rung_counts, runs_total),
        "features": {
            "packs": packs_flag,
            "connections": connections_flag,
            "delegation": delegation_flag,
            "local_models": local_models_flag,
        },
    }
    return TelemetryPayload.model_validate(data)


async def preview(db: AsyncSession) -> dict:
    """`{"payload": {...}, "would_send": bool}` — NEVER mints an id. When no
    id exists yet, a throwaway uuid4 is used to build a valid payload and then
    swapped for the placeholder in the returned dict, so the placeholder never
    has to pass the model's own uuid4 validator."""
    existing_id = await _get_value(db, "telemetry_instance_id", None)
    payload = await build_payload(db, instance_id=existing_id or str(uuid.uuid4()))
    data = payload.model_dump(mode="json")
    if not existing_id:
        data["instance_id"] = PREVIEW_PLACEHOLDER
    state = await resolve_state(db)
    return {"payload": data, "would_send": state.enabled}


# ── sending (contract §4) ────────────────────────────────────────────────────


async def send_once(db: AsyncSession, *, now: datetime | None = None) -> dict:
    """Resolve state, build+POST the payload if enabled, record the attempt.

    Never raises: every transport failure (including `EgressDenied` from a
    class that turned off between `resolve_state` and the request) is caught
    broadly and logged at DEBUG, exactly like an HTTP error status — both end
    up as a `"failed"` entry in `telemetry_recent`.
    """
    now = now or datetime.now(timezone.utc)
    state = await resolve_state(db)
    if not state.enabled:
        if state.locked:
            # A locked-OFF reason at sender-tick time: drop any stored id
            # (contract §1) even if nothing here ever gets a chance to send.
            await _delete_value(db, "telemetry_instance_id")
            await db.commit()
        return {"sent": False, "reason": state.locked_reason or "disabled"}

    instance_id = await _get_value(db, "telemetry_instance_id", None)
    if not instance_id:
        # env `on` case: enabled with no admin toggle ever run to mint one.
        instance_id = str(uuid.uuid4())
        await _set_value(db, "telemetry_instance_id", instance_id)
        await db.commit()

    payload = await build_payload(db, instance_id=instance_id, now=now)
    body = payload.model_dump_json()
    entry = {
        "sent_at": now.isoformat(),
        "status": "failed",
        "http_status": None,
        "payload": payload.model_dump(mode="json"),
    }
    try:
        async with open_client(CLASS_TELEMETRY, timeout=5) as client:
            response = await client.post(
                get_settings().telemetry_url,
                content=body,
                headers={"Content-Type": "application/json", "User-Agent": "tret-telemetry/1"},
            )
        entry["http_status"] = response.status_code
        if 200 <= response.status_code < 300:
            entry["status"] = "sent"
    except Exception:
        log.debug("telemetry send failed", exc_info=True)

    # S4: recorded on every attempt, success or failure — distinct from
    # `telemetry_last_sent_at`, which only ever advances on a 2xx. This is
    # what lets `_tick` tell "never tried" from "tried recently and failed"
    # and back off, instead of hammering a permanently-failing collector once
    # per sender tick forever.
    await _set_value(db, "telemetry_last_attempt_at", now.isoformat())
    recent = list(await _get_value(db, "telemetry_recent", []))
    recent = [entry] + recent
    await _set_value(db, "telemetry_recent", recent[:RECENT_CAP])
    if entry["status"] == "sent":
        await _set_value(db, "telemetry_last_sent_at", now.isoformat())
    await db.commit()
    return {"sent": entry["status"] == "sent", "http_status": entry["http_status"]}


def _parse_stored_ts(raw: str) -> datetime:
    """An `InstanceState` ISO timestamp, read back as UTC. These are written
    via an aware UTC `datetime.isoformat()`, but a naive string (hand-edited,
    or left by an older release) is treated as UTC rather than the local
    zone — the same rule `build_payload`'s own window-start parsing uses."""
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def _cleanup_locked_off(db: AsyncSession) -> bool:
    """B1: if the effective state is locked OFF, delete any stray
    `telemetry_instance_id` (contract §1) — the cleanup half of what used to
    be `send_once`'s own dead-code branch: with the old `sender_loop` only
    ever calling `send_once` `if state.enabled`, a state that went from
    ON to locked-OFF (DO_NOT_TRACK set later, egress class switched off,
    ...) would never reach that branch and the id would survive forever.

    Returns `state.enabled`, so `_tick` doesn't have to `resolve_state`
    twice. Commits only when it actually deletes something.
    """
    state = await resolve_state(db)
    if not state.enabled and state.locked:
        await _delete_value(db, "telemetry_instance_id")
        await db.commit()
    return state.enabled


async def _tick(db: AsyncSession, *, now: datetime | None = None) -> None:
    """One sender-loop iteration's worth of work: clean up a stale id if
    locked off, else send if due. `due` (S4) is BOTH last_sent_at missing or
    >7 days old AND last_attempt_at missing or >24h old — the second half is
    what stops a permanently-failing collector from being re-POSTed to every
    6h tick forever; it backs off to roughly once a day instead.
    """
    now = now or datetime.now(timezone.utc)
    enabled = await _cleanup_locked_off(db)
    if not enabled:
        return
    last_sent_raw = await _get_value(db, "telemetry_last_sent_at", None)
    last_attempt_raw = await _get_value(db, "telemetry_last_attempt_at", None)
    sent_due = last_sent_raw is None or (now - _parse_stored_ts(last_sent_raw)) > timedelta(days=7)
    attempt_due = (
        last_attempt_raw is None
        or (now - _parse_stored_ts(last_attempt_raw)) > timedelta(hours=ATTEMPT_RETRY_HOURS)
    )
    if sent_due and attempt_due:
        await send_once(db, now=now)


async def sender_loop() -> None:
    """The one background task that calls `send_once` on its own (contract
    §4).

    B1: a locked-off cleanup pass runs once at startup, BEFORE the initial
    5-60 minute delay — cleanup only, never a send, so a stale id left over
    from a state that locked off while the process was down (or between
    releases) does not sit around for up to an hour before it is cleared.
    Then the normal cadence: a random 5-60 minute delay after boot so a
    fleet of instances does not all report the instant they come up, then
    every 6h a full `_tick` (cleanup-or-send, whichever applies).

    Must never raise into the lifespan or delay shutdown — every exception
    except `CancelledError` is swallowed and logged at DEBUG.
    """
    from tret.db.engine import get_session_factory

    session_factory = get_session_factory()

    try:
        async with session_factory() as db:
            await _cleanup_locked_off(db)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.debug("telemetry sender loop startup cleanup failed", exc_info=True)

    await asyncio.sleep(random.uniform(300, 3600))
    while True:
        try:
            async with session_factory() as db:
                await _tick(db)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.debug("telemetry sender loop iteration failed", exc_info=True)
        await asyncio.sleep(6 * 3600)
