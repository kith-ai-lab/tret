"""Schema management. **Alembic is the single source of truth for the schema.**

Startup used to call `Base.metadata.create_all`, which quietly worked forever on
fresh installs and broke every *upgrade*: `create_all` adds missing tables but
never adds a column to a table that already exists, and it never writes an
`alembic_version` row — so a database from an earlier tret release both crashed
on the new code (`column packs.content_hash does not exist`) and could not be
repaired with `alembic upgrade head` either, because Alembic saw no stamp and
tried to re-create `users` from base.

`ensure_schema()` replaces that with a step that recognises the three states a
real deployment can be in, and refuses to guess in any fourth:

* **empty** — no tret tables at all → `alembic upgrade head` builds everything.
* **stamped** — `alembic_version` carries a revision → `alembic upgrade head`
  (a no-op when already current). The normal path.
* **legacy** — tret tables exist but nothing stamped them: a `create_all`
  database. The baseline is inferred from *which columns and tables actually
  exist* (never assumed), `alembic stamp <that revision>` writes the stamp, and
  the upgrade then runs the remaining migrations. Loudly logged.
* **anything else** — a schema that matches no known revision is a hand-edited
  or half-migrated database. Startup fails with the exact recovery commands
  rather than stamping a revision that might silently skip a migration.

Concurrency: the whole step runs under a Postgres session-level advisory lock
(`SCHEMA_LOCK_KEY`), so two instances booting together serialise and the second
one finds the work already done. Today's deployments are single-instance (Fly
runs one machine — the run event bus is in-process, see docs/architecture.md),
so the lock is insurance for the horizontally-scaled case rather than something
currently exercised in production.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from sqlalchemy import inspect, text

from tret.config import get_settings

log = logging.getLogger("tret.schema")

# Arbitrary but fixed: any tret process migrating the same database takes this
# advisory lock first. Chosen once and never changed — a different key in a
# future release would stop serialising against a running older one.
SCHEMA_LOCK_KEY = 0x62656E6368_5343  # "tret" + "SC"

# The opt-out, as an env var name for messages. It is a declared Settings field
# (`skip_migrations`), not a bare os.environ read: an undeclared knob never
# reaches the container under `docker compose up` and is invisible to
# .env.example, so an operator could set it in .env and watch tret migrate the
# database anyway. tests/test_compose_env.py holds Settings, docker-compose.yml
# and .env.example in agreement in both directions.
SKIP_ENV_VAR = "TRET_SKIP_MIGRATIONS"


class SchemaUpgradeError(RuntimeError):
    """Startup-fatal: the database's schema could not be safely brought to head."""


def backend_root() -> Path:
    """The directory holding `alembic.ini` and `alembic/`.

    Normally `backend/` — two levels above this file for an editable install, and
    also `/app` in both shipped images, which copy `tret/`, `alembic/` and
    `alembic.ini` side by side. Looked up rather than assumed because a
    non-editable install puts `tret` in site-packages, where the migrations are
    not; the walk up from `Path.cwd()` covers running from a subdirectory.
    """
    candidates = [Path(__file__).resolve().parents[2], Path.cwd().resolve()]
    candidates += list(Path.cwd().resolve().parents)
    for candidate in candidates:
        if (candidate / "alembic.ini").is_file() and (candidate / "alembic" / "env.py").is_file():
            return candidate
    raise SchemaUpgradeError(
        "cannot find the Alembic migrations: no directory containing both alembic.ini and "
        f"alembic/env.py near {Path(__file__).resolve().parents[2]} or {Path.cwd()}. tret "
        "cannot manage its schema without them — run from the backend/ directory, or from a "
        "container image that copies alembic/ and alembic.ini next to the tret package (see "
        "backend/Dockerfile). Details: docs/upgrading.md"
    )


# ── which schema belongs to which revision ────────────────────────────────────
# A marker is (table, column) — column None means "the table itself". Each entry
# lists what its revision *adds*, so a legacy database can be matched to a
# revision by inspecting the live schema. Every revision below has at least one
# marker unique to it; append a new entry whenever a migration is added, or a
# legacy database created by the release that introduced it will be classified
# as unrecognised (which fails loudly rather than corrupting anything).
Marker = tuple[str, str | None]

REVISION_MARKERS: tuple[tuple[str, tuple[Marker, ...]], ...] = (
    (
        "c7ad56fb3365",  # initial schema
        (
            ("users", None),
            ("workspaces", None),
            ("packs", None),
            ("projects", None),
            ("provider_credentials", None),
            ("documents", None),
            ("harnesses", None),
            ("datasets", None),
            ("dataset_rows", None),
            ("runs", None),
            ("data_requests", None),
            ("findings", None),
            ("approvals", None),
        ),
    ),
    ("bcfa2baec8e2", (("conversations", None),)),
    ("f18e4f74fc33", (("method_runs", None),)),
    ("a1c4e9d70b52", (("packs", "content_hash"),)),
    ("d2b7f5a91c34", (("runs", "cache_read_tokens"), ("runs", "cache_write_tokens"))),
    ("e5a71c0b93df", (("runs", "context_composition"),)),
    ("f4c1d8ab26e7", (("runs", "energy_wh"), ("runs", "energy_accounting"))),
    ("b3e9f21d5c47", (("egress_calls", None), ("documents", "source_kind"))),
    ("b3e2f90a4c17", (("run_outcomes", None),)),
    ("c4f7a1b23e69", (("runs", "compactions"),)),
    ("d8b3c05fa412", (("runs", "model_timeline"),)),
    ("e2a91f6b7c34", (("run_outcomes", "segment_index"),)),
    ("f6c02d1948ab", (("runs", "overhead"),)),
    ("15981123afd0", (("runs", "reported_cost_usd"),)),
    ("35ed0d08502b", (("workspace_members", None),)),
    ("3ffc3736279c", (("workspaces", "kind"), ("workspaces", "personal_owner_id"))),
    (
        "8eef9d61c7c4",
        (("users", "oidc_sub"), ("users", "session_epoch"), ("users", "disabled")),
    ),
    ("e3c974913c19", (("invites", None),)),
    ("82169de695f6", (("draft_packs", None),)),
    ("84cf41ced91f", (("harness_packs", None),)),
    ("63e11dc72bd7", (("workspace_connections", None),)),
    ("d65064395ad0", (("connection_activity", None),)),
    ("6b018767adc6", (("harnesses", "packs_linked_at"),)),
)

# Every table any known revision creates. Used to tell "empty database" from
# "database with a schema we have to classify"; a stray unrelated table in the
# same schema therefore does not make tret think it owns the database.
KNOWN_TABLES: frozenset[str] = frozenset(
    table for _rev, markers in REVISION_MARKERS for table, _col in markers
)

ALEMBIC_VERSION_TABLE = "alembic_version"


@dataclass(frozen=True)
class DatabaseState:
    """What the live database actually contains. Cheap to fake in tests."""

    tables: frozenset[str] = frozenset()
    # table -> its column names. Only tables tret cares about need be present.
    columns: dict[str, frozenset[str]] = field(default_factory=dict)
    # Revisions found in alembic_version (empty when the table is absent, and
    # also when it exists but holds no row — an aborted stamp looks like that).
    stamped: frozenset[str] = frozenset()

    def has(self, marker: Marker) -> bool:
        table, column = marker
        if table not in self.tables:
            return False
        if column is None:
            return True
        return column in self.columns.get(table, frozenset())


PlanKind = Literal["empty", "stamped", "legacy"]


@dataclass(frozen=True)
class MigrationPlan:
    """What ensure_schema() decided to do, and why. `stamp` runs before upgrade."""

    kind: PlanKind
    stamp: str | None = None
    detail: str = ""


def _revision_status(state: DatabaseState, markers: tuple[Marker, ...]) -> str:
    present = sum(1 for m in markers if state.has(m))
    if present == len(markers):
        return "all"
    if present == 0:
        return "none"
    return "partial"


def _describe(state: DatabaseState, statuses: list[tuple[str, str]]) -> str:
    words = {"all": "fully present", "none": "absent", "partial": "PARTIALLY present"}
    lines = []
    for revision, status in statuses:
        markers = dict(REVISION_MARKERS)[revision]
        detail = ""
        if status == "partial":
            missing = ", ".join(
                f"{t}.{c}" if c else t for t, c in markers if not state.has((t, c))
            )
            detail = f" — missing {missing}"
        lines.append(f"    {revision}: {words[status]}{detail}")
    return "\n".join(lines)


def plan_schema_upgrade(state: DatabaseState) -> MigrationPlan:
    """Classify `state` into one of the three handled cases, or raise.

    Pure: no I/O, so the classification is unit-testable without a database.
    """
    statuses = [(rev, _revision_status(state, markers)) for rev, markers in REVISION_MARKERS]

    if state.stamped:
        current = ", ".join(sorted(state.stamped))
        return MigrationPlan("stamped", detail=f"alembic_version = {current}")

    if not (state.tables & KNOWN_TABLES):
        # Nothing of ours here. An alembic_version table with no row (aborted
        # stamp) lands here too, and upgrading from base is right in both cases.
        return MigrationPlan("empty", detail="no tret tables present")

    # Unstamped, but our tables exist: a create_all database. Infer the newest
    # revision whose schema is *completely* present, and require that no later
    # revision has left any trace — a partial match means someone has been
    # editing by hand and we must not paper over it.
    prefix = 0
    while prefix < len(statuses) and statuses[prefix][1] == "all":
        prefix += 1
    baseline_index = prefix - 1
    trailing_evidence = [(rev, st) for rev, st in statuses[prefix:] if st != "none"]

    if baseline_index < 0 or trailing_evidence:
        raise SchemaUpgradeError(
            "tret found an existing database that no Alembic revision matches, and it has "
            f"no {ALEMBIC_VERSION_TABLE} stamp to go on. Refusing to guess a baseline, "
            "because stamping the wrong one would skip a migration silently.\n"
            "  Schema found (per revision, oldest first):\n"
            f"{_describe(state, statuses)}\n"
            "  If you know which tret release created this database, stamp it yourself and "
            "upgrade:\n"
            "      cd backend\n"
            "      alembic stamp <revision>   # `alembic history` lists them\n"
            "      alembic upgrade head\n"
            "  If the data is expendable, drop the database and let tret build it from "
            "scratch.\n"
            "  Full instructions: docs/upgrading.md#a-database-no-revision-matches"
        )

    baseline = statuses[baseline_index][0]
    return MigrationPlan(
        "legacy",
        stamp=baseline,
        detail=(
            f"{len(state.tables & KNOWN_TABLES)} tret tables, no {ALEMBIC_VERSION_TABLE} "
            f"stamp; schema matches revision {baseline}"
        ),
    )


# ── live inspection ───────────────────────────────────────────────────────────
def read_database_state(sync_connection) -> DatabaseState:
    """Inspect a (sync-facade) connection into a DatabaseState."""
    inspector = inspect(sync_connection)
    tables = frozenset(inspector.get_table_names())
    columns = {
        table: frozenset(c["name"] for c in inspector.get_columns(table))
        for table in tables & KNOWN_TABLES
    }
    stamped: frozenset[str] = frozenset()
    if ALEMBIC_VERSION_TABLE in tables:
        # Interpolated from a module constant, never from input.
        rows = sync_connection.execute(
            text(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}")
        ).scalars()
        stamped = frozenset(r for r in rows if r)
    return DatabaseState(tables=tables, columns=columns, stamped=stamped)


def alembic_config(sync_connection=None):
    """An Alembic Config wired to backend/alembic.ini.

    Passing `sync_connection` puts it in `config.attributes["connection"]`, which
    alembic/env.py picks up instead of opening its own async engine — that is the
    seam that lets the app run migrations inside a connection it already owns
    (and inside the advisory lock) without nesting event loops.
    """
    from alembic.config import Config

    root = backend_root()
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    if sync_connection is not None:
        config.attributes["connection"] = sync_connection
    return config


def apply_plan(sync_connection, plan: MigrationPlan) -> None:
    """Run `plan` against an open connection using Alembic's Python API."""
    from alembic import command

    config = alembic_config(sync_connection)
    if plan.stamp is not None:
        command.stamp(config, plan.stamp)
    command.upgrade(config, "head")


def current_revisions(sync_connection) -> frozenset[str]:
    from alembic.migration import MigrationContext

    return frozenset(MigrationContext.configure(sync_connection).get_current_heads())


# ── the startup step ──────────────────────────────────────────────────────────
async def ensure_schema(engine) -> MigrationPlan | None:
    """Bring `engine`'s database to the head revision. Returns what it did.

    Returns None when the step was skipped (see TRET_SKIP_MIGRATIONS and the
    non-Postgres fallback below). Raises SchemaUpgradeError when the database
    cannot be classified — startup must not continue in that case.
    """
    if get_settings().skip_migrations:
        log.warning(
            "%s=1: skipping the schema migration step. The database is assumed to already "
            "be at head; tret will fail later if it is not.",
            SKIP_ENV_VAR,
        )
        return None

    if engine.dialect.name != "postgresql":
        # Development/test convenience only. The migrations are Postgres-flavoured
        # (JSONB, ARRAY, GIN) and advisory locks do not exist elsewhere, so a
        # sqlite URL gets the old create_all behaviour — never a production path.
        from tret.db.models import Base

        log.warning(
            "database dialect is %r, not postgresql: creating tables directly from the "
            "models and NOT running migrations. Supported deployments use Postgres.",
            engine.dialect.name,
        )
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        return None

    async with engine.connect() as conn:
        # Session-level, so it outlives the commits below; released in `finally`.
        await conn.execute(text("SELECT pg_advisory_lock(:key)"), {"key": SCHEMA_LOCK_KEY})
        await conn.commit()
        try:
            state = await conn.run_sync(read_database_state)
            plan = plan_schema_upgrade(state)  # raises SchemaUpgradeError
            if plan.kind == "legacy":
                log.warning(
                    "LEGACY DATABASE DETECTED: this database was created by an older tret "
                    "release that built its schema directly from the models and left no "
                    "Alembic stamp (%s). Inferred baseline revision %s by inspecting the "
                    "columns that actually exist; stamping it and applying every migration "
                    "after it. Nothing is dropped and no data is rewritten. "
                    "See docs/upgrading.md.",
                    plan.detail,
                    plan.stamp,
                )
            else:
                log.info("schema state: %s (%s)", plan.kind, plan.detail)

            await conn.run_sync(apply_plan, plan)
            await conn.commit()

            heads = await conn.run_sync(current_revisions)
            log.info("schema is at revision %s", ", ".join(sorted(heads)) or "(none)")
            return plan
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": SCHEMA_LOCK_KEY})
            await conn.commit()
