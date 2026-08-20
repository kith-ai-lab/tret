"""Classification of a database's schema state, and the guards that keep it honest.

These run with no database at all: `plan_schema_upgrade` is a pure function over a
`DatabaseState`, which is the whole reason it is separated from the I/O. The live
Postgres half — empty→head, legacy recovery, models-vs-migrations drift — lives in
tests/test_migrations_postgres.py.
"""
from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory

from tret.db.migrate import (
    KNOWN_TABLES,
    REVISION_MARKERS,
    DatabaseState,
    SchemaUpgradeError,
    alembic_config,
    ensure_schema,
    plan_schema_upgrade,
)

HEAD = REVISION_MARKERS[-1][0]
LEGACY_V01 = "f18e4f74fc33"  # the pre-sprint head: initial + conversations + method_runs


def state_at(index: int, *, stamped: str | None = None) -> DatabaseState:
    """A DatabaseState carrying exactly the schema of REVISION_MARKERS[:index + 1]."""
    tables: set[str] = set()
    columns: dict[str, set[str]] = {}
    for _revision, markers in REVISION_MARKERS[: index + 1]:
        for table, column in markers:
            tables.add(table)
            columns.setdefault(table, set())
            if column is not None:
                columns[table].add(column)
    if stamped:
        tables.add("alembic_version")
    return DatabaseState(
        tables=frozenset(tables),
        columns={t: frozenset(c) for t, c in columns.items()},
        stamped=frozenset([stamped]) if stamped else frozenset(),
    )


# ── the three states ──────────────────────────────────────────────────────────
def test_empty_database_upgrades_from_base():
    plan = plan_schema_upgrade(DatabaseState())
    assert plan.kind == "empty"
    assert plan.stamp is None


def test_stamped_database_is_just_upgraded():
    plan = plan_schema_upgrade(state_at(len(REVISION_MARKERS) - 1, stamped=HEAD))
    assert plan.kind == "stamped"
    assert plan.stamp is None
    assert HEAD in plan.detail


def test_stamped_database_behind_head_is_still_the_normal_path():
    """A stamp is trusted even when the schema is old — that is Alembic's job."""
    plan = plan_schema_upgrade(state_at(2, stamped=LEGACY_V01))
    assert plan.kind == "stamped"


def test_legacy_v01_create_all_database_infers_the_pre_sprint_head():
    """The exact case that broke on upgrade: a v0.1 create_all database."""
    plan = plan_schema_upgrade(state_at(2))
    assert plan.kind == "legacy"
    assert plan.stamp == LEGACY_V01


@pytest.mark.parametrize("index", range(len(REVISION_MARKERS)))
def test_every_revision_is_recoverable_as_a_legacy_baseline(index):
    """A create_all database from *any* past release classifies to that release."""
    plan = plan_schema_upgrade(state_at(index))
    assert plan.kind == "legacy"
    assert plan.stamp == REVISION_MARKERS[index][0]


def test_legacy_at_current_head_stamps_head_and_does_nothing_else():
    plan = plan_schema_upgrade(state_at(len(REVISION_MARKERS) - 1))
    assert plan.kind == "legacy"
    assert plan.stamp == HEAD


def test_alembic_version_table_with_no_row_is_not_a_stamp():
    """An aborted `alembic stamp` leaves an empty table; treat it as unstamped."""
    state = state_at(2)
    state = DatabaseState(
        tables=state.tables | {"alembic_version"},
        columns=dict(state.columns),
        stamped=frozenset(),
    )
    plan = plan_schema_upgrade(state)
    assert plan.kind == "legacy"
    assert plan.stamp == LEGACY_V01


def test_empty_schema_with_an_empty_alembic_version_table_is_empty_not_legacy():
    state = DatabaseState(tables=frozenset({"alembic_version"}))
    assert plan_schema_upgrade(state).kind == "empty"


def test_unrelated_tables_do_not_make_tret_claim_the_database():
    state = DatabaseState(tables=frozenset({"some_other_app_table"}))
    assert plan_schema_upgrade(state).kind == "empty"


# ── the fourth state: refuse to guess ─────────────────────────────────────────
def test_half_migrated_database_fails_with_recovery_instructions():
    """One of a revision's two columns present: someone migrated by hand."""
    state = state_at(4)
    state = DatabaseState(
        tables=state.tables,
        columns={**state.columns, "runs": state.columns["runs"] - {"cache_write_tokens"}},
        stamped=frozenset(),
    )
    with pytest.raises(SchemaUpgradeError) as excinfo:
        plan_schema_upgrade(state)
    message = str(excinfo.value)
    assert "alembic stamp" in message
    assert "alembic upgrade head" in message
    assert "docs/upgrading.md" in message
    # The operator is shown what was actually found, per revision.
    assert "d2b7f5a91c34: PARTIALLY present" in message
    assert "runs.cache_write_tokens" in message


def test_a_later_revision_present_over_an_incomplete_earlier_one_fails():
    """Newest-first inference must not skip a gap in the middle of the chain."""
    state = state_at(len(REVISION_MARKERS) - 1)
    state = DatabaseState(
        tables=state.tables - {"method_runs"},  # revision 3's table is missing
        columns=dict(state.columns),
        stamped=frozenset(),
    )
    with pytest.raises(SchemaUpgradeError):
        plan_schema_upgrade(state)


def test_missing_core_tables_fails_rather_than_stamping_the_initial_revision():
    state = DatabaseState(
        tables=frozenset({"users", "runs"}),
        columns={"users": frozenset({"id"}), "runs": frozenset({"id"})},
    )
    with pytest.raises(SchemaUpgradeError):
        plan_schema_upgrade(state)


# ── the guard that keeps the marker table in step with the migration chain ────
def test_revision_markers_match_the_alembic_chain_exactly():
    """A new migration with no marker entry would make legacy detection wrong.

    Classification works by inspecting live columns, so it can only recognise
    revisions it has markers for. This test fails the moment a migration is added
    without one, which is the failure mode that has to be caught at review time.
    """
    script = ScriptDirectory.from_config(alembic_config())
    heads = script.get_heads()
    assert len(heads) == 1, f"expected a single Alembic head, found {heads}"
    chain = [rev.revision for rev in script.walk_revisions()][::-1]  # base → head
    assert [r for r, _ in REVISION_MARKERS] == chain


def test_every_revision_has_at_least_one_marker_no_earlier_revision_has():
    """Markers must be *discriminating*, or two revisions become indistinguishable."""
    seen: set[tuple[str, str | None]] = set()
    for revision, markers in REVISION_MARKERS:
        assert markers, f"{revision} has no markers"
        assert set(markers) - seen, f"{revision} adds nothing not already in an earlier revision"
        seen |= set(markers)


def test_known_tables_covers_every_model_table():
    """Otherwise a database full of tret tables could be misread as empty."""
    from tret.db.models import Base

    assert set(Base.metadata.tables) == set(KNOWN_TABLES)


# ── the escape hatches ────────────────────────────────────────────────────────
async def test_skip_env_var_short_circuits_the_whole_step(monkeypatch):
    from tret import config

    monkeypatch.setenv("TRET_SKIP_MIGRATIONS", "1")
    # TRET_SKIP_MIGRATIONS is a Settings field, and get_settings() is lru_cached,
    # so the env var only lands in a freshly built Settings.
    config.get_settings.cache_clear()

    class Exploding:
        @property
        def dialect(self):  # pragma: no cover - must never be reached
            raise AssertionError("ensure_schema touched the engine despite the skip flag")

    try:
        assert await ensure_schema(Exploding()) is None
    finally:
        monkeypatch.undo()
        config.get_settings.cache_clear()


async def test_non_postgres_url_creates_tables_from_the_models_and_skips_alembic(
    tmp_path, monkeypatch
):
    """Dev convenience: the migrations are Postgres-flavoured (JSONB, ARRAY, GIN),
    so a sqlite URL is served straight from the model metadata. Production is
    always the migration path.

    The real `Base.metadata` needs the sqlite type shims that
    tests/evals/golden_world.py installs; this test stands in a plain table
    instead, because what is under test is the *branch* — create_all from the
    declared metadata, no Alembic, no advisory lock.
    """
    from sqlalchemy import Column, Integer, MetaData, Table, inspect
    from sqlalchemy.ext.asyncio import create_async_engine

    from tret.db import models

    metadata = MetaData()
    Table("stand_in", metadata, Column("id", Integer, primary_key=True))
    monkeypatch.setattr(models, "Base", type("Base", (), {"metadata": metadata}))

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'tret.db'}")
    try:
        assert await ensure_schema(engine) is None
        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: set(inspect(c).get_table_names()))
        assert tables == {"stand_in"}  # no alembic_version: Alembic never ran
    finally:
        await engine.dispose()
