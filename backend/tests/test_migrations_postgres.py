"""The migration chain, exercised against a real Postgres. **This is the suite
that would have caught the create_all/Alembic split-brain bug.**

Skipped unless `TRET_TEST_POSTGRES_URL` points at a Postgres server the tests may
create and drop databases on — CI sets it to the service container, and locally:

    TRET_TEST_POSTGRES_URL=postgresql+asyncpg://tret:tret@localhost:5432/postgres \\
        .venv/bin/python -m pytest tests/test_migrations_postgres.py -q

Every test runs against its own freshly created database and drops it afterwards,
so nothing here can touch a development database. The URL's own database name is
used only as the maintenance connection for CREATE/DROP DATABASE.

What is asserted, and why each one matters:

* `alembic upgrade head` from **empty** builds the whole schema — the state every
  new install is in, and the one nothing verified before.
* the built schema has **no autogenerate drift** against `Base.metadata`. This is
  the root-cause check: a model column added without a migration fails here, which
  is exactly how `packs.content_hash` came to exist in the models while no
  upgraded database ever grew it.
* a **legacy create_all database** (tables present, `alembic_version` absent) is
  adopted by `ensure_schema`: baseline inferred, stamped, migrated to head, rows
  left intact.
* **downgrade one step and upgrade again** works, so the downgrades are real.
* the whole thing is **idempotent** and safe to run concurrently.
"""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from tret.db.migrate import (
    alembic_config,
    current_revisions,
    ensure_schema,
    plan_schema_upgrade,
    read_database_state,
)
from tret.api.chat import _assistant_message
from tret.db.models import Base
from tret.db.models import Run as RunRow
from tret.providers.base import Msg, ToolCall

ADMIN_URL = os.environ.get("TRET_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="set TRET_TEST_POSTGRES_URL to a Postgres server where tests may create databases",
)

# The pre-sprint head — the shape of a v0.1 install, i.e. the release early
# adopters are upgrading *from*.
LEGACY_V01 = "f18e4f74fc33"


def head_revision() -> str:
    (head,) = ScriptDirectory.from_config(alembic_config()).get_heads()
    return head


def _url_for(database: str) -> str:
    base, _, _ = ADMIN_URL.rpartition("/")
    return f"{base}/{database}"


@pytest.fixture
async def database():
    """A throwaway database, dropped afterwards. Yields its URL."""
    name = f"tret_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(ADMIN_URL, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield _url_for(name)
    finally:
        async with admin.connect() as conn:
            await conn.execute(
                text("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = :n"),
                {"n": name},
            )
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        await admin.dispose()


@pytest.fixture
async def engine(database):
    eng = create_async_engine(database)
    try:
        yield eng
    finally:
        await eng.dispose()


# ── helpers, all run through Connection.run_sync ──────────────────────────────
def _autogenerate_diff(sync_conn):
    return compare_metadata(MigrationContext.configure(sync_conn), Base.metadata)


def _upgrade(sync_conn, revision):
    command.upgrade(alembic_config(sync_conn), revision)


def _downgrade(sync_conn, revision):
    command.downgrade(alembic_config(sync_conn), revision)


def _stamp(sync_conn, revision):
    command.stamp(alembic_config(sync_conn), revision)


async def _heads(engine) -> frozenset[str]:
    async with engine.connect() as conn:
        return await conn.run_sync(current_revisions)


async def _diff(engine) -> list:
    async with engine.connect() as conn:
        return await conn.run_sync(_autogenerate_diff)


async def _make_legacy_create_all_database(engine, revision: str = LEGACY_V01) -> None:
    """Reproduce what an older tret release left behind.

    Migrating to `revision` and then removing `alembic_version` gives a database
    with exactly that release's schema and no stamp — which is what
    `Base.metadata.create_all` produced when it ran under that release's models.
    """
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, revision)
        await conn.commit()
        await conn.execute(text("DROP TABLE alembic_version"))
        await conn.commit()


# ── empty → head ──────────────────────────────────────────────────────────────
async def test_empty_database_migrates_to_head(engine):
    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "empty"
    assert await _heads(engine) == frozenset({head_revision()})
    async with engine.connect() as conn:
        state = await conn.run_sync(read_database_state)
    assert "runs" in state.tables
    assert {"energy_wh", "energy_accounting", "cache_read_tokens"} <= state.columns["runs"]
    assert "content_hash" in state.columns["packs"]


async def test_migrated_schema_has_no_drift_from_the_models(engine):
    """Models vs migrations. An empty autogenerate diff is the whole point.

    If this fails, someone changed `tret/db/models.py` without writing the
    matching migration: fresh installs (which used to run create_all) would have
    the column and every upgraded install would not.
    """
    await ensure_schema(engine)
    diff = await _diff(engine)
    assert diff == [], (
        "models and migrations have drifted apart. Generate the missing migration:\n"
        "    cd backend && alembic revision --autogenerate -m '<what changed>'\n"
        f"  Alembic reports: {diff}"
    )


async def test_second_startup_is_a_no_op(engine):
    await ensure_schema(engine)
    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "stamped"
    assert await _heads(engine) == frozenset({head_revision()})


async def test_concurrent_startups_serialise_on_the_advisory_lock(database):
    """Two instances booting together must not both try to migrate."""
    engines = [create_async_engine(database) for _ in range(3)]
    try:
        plans = await asyncio.gather(*(ensure_schema(e) for e in engines))
        kinds = sorted(p.kind for p in plans)
        assert kinds == ["empty", "stamped", "stamped"], kinds
        assert await _heads(engines[0]) == frozenset({head_revision()})
        assert await _diff(engines[0]) == []
    finally:
        for e in engines:
            await e.dispose()


# ── legacy create_all database → adopted ──────────────────────────────────────
async def test_legacy_create_all_database_is_stamped_and_upgraded(engine):
    await _make_legacy_create_all_database(engine)

    async with engine.connect() as conn:
        state = await conn.run_sync(read_database_state)
    assert state.stamped == frozenset()  # no stamp: this is the broken state
    assert "content_hash" not in state.columns["packs"]
    assert plan_schema_upgrade(state).stamp == LEGACY_V01

    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "legacy" and plan.stamp == LEGACY_V01
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


async def test_legacy_recovery_preserves_existing_rows(engine):
    """The migrations must adopt real data, not just an empty shell."""
    await _make_legacy_create_all_database(engine)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, created_at) "
                "VALUES (gen_random_uuid(), 'legacy@example.com', 'Legacy', 'analyst', now())"
            )
        )

    await ensure_schema(engine)

    async with engine.connect() as conn:
        survivors = (
            await conn.execute(
                text("SELECT count(*) FROM users WHERE email = 'legacy@example.com'")
            )
        ).scalar()
        # The column whose absence crashed the old code is now queryable.
        await conn.execute(text("SELECT content_hash FROM packs"))
    assert survivors == 1


async def test_workspace_member_backfill_promotes_the_oldest_admin_to_owner(engine):
    """35ed0d08502b's role backfill copies each user's pre-tenancy global role
    (admin|analyst|approver) straight across — none of which is 'owner' — so
    without the extra promotion this migration adds, every upgraded database
    would end up with zero workspace owners and every owner-gated action
    (api/workspaces.py) a dead end. The oldest admin-role member of the one
    workspace the backfill targets must come out as 'owner'; everyone else
    keeps their backfilled role unchanged."""
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "15981123afd0")  # just before workspace_members exists
        await conn.commit()
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, name, settings, created_at) "
                "VALUES ('11111111-1111-1111-1111-111111111111', 'Default', '{}'::jsonb, now())"
            )
        )
        # Three users, oldest first: an analyst, then two admins — the
        # *older* of the two admins must be the one promoted.
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, created_at) VALUES "
                "('22222222-2222-2222-2222-222222222222', 'first-analyst@example.com', 'A', "
                "'analyst', now() - interval '3 hours'), "
                "('33333333-3333-3333-3333-333333333333', 'older-admin@example.com', 'B', "
                "'admin', now() - interval '2 hours'), "
                "('44444444-4444-4444-4444-444444444444', 'newer-admin@example.com', 'C', "
                "'admin', now() - interval '1 hour')"
            )
        )
        await conn.commit()
        await conn.run_sync(_upgrade, "head")
        await conn.commit()

    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT user_id, role FROM workspace_members"))
        ).all()
    roles_by_user = {str(user_id): role for user_id, role in rows}
    assert roles_by_user == {
        "22222222-2222-2222-2222-222222222222": "analyst",  # unchanged
        "33333333-3333-3333-3333-333333333333": "owner",  # oldest admin, promoted
        "44444444-4444-4444-4444-444444444444": "admin",  # younger admin, unchanged
    }


async def test_workspace_member_backfill_promotes_no_one_without_an_admin(engine):
    """A workspace whose backfilled members are all non-admin ends up with no
    owner — exactly the pre-migration state, not something this migration can
    invent an owner out of."""
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "15981123afd0")
        await conn.commit()
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, name, settings, created_at) "
                "VALUES ('55555555-5555-5555-5555-555555555555', 'Default', '{}'::jsonb, now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, created_at) VALUES "
                "('66666666-6666-6666-6666-666666666666', 'analyst-only@example.com', 'A', "
                "'analyst', now())"
            )
        )
        await conn.commit()
        await conn.run_sync(_upgrade, "head")
        await conn.commit()

    async with engine.connect() as conn:
        roles = (await conn.execute(text("SELECT role FROM workspace_members"))).scalars().all()
    assert roles == ["analyst"]


async def _insert_workspace_project_harness(conn, workspace_id, project_id, harness_id) -> None:
    await conn.execute(
        text(
            "INSERT INTO workspaces (id, name, settings, created_at) "
            f"VALUES ('{workspace_id}', 'W', '{{}}'::jsonb, now())"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO projects (id, workspace_id, name, created_at) "
            f"VALUES ('{project_id}', '{workspace_id}', 'P', now())"
        )
    )
    await conn.execute(
        text(
            "INSERT INTO harnesses (id, workspace_id, name, task_profile, model_policy, "
            "tool_names, loop_config, is_archived, created_at, updated_at) "
            f"VALUES ('{harness_id}', '{workspace_id}', 'H', 'chat', '{{}}'::jsonb, "
            "'{}'::text[], '{}'::jsonb, false, now(), now())"
        )
    )


async def _insert_run(conn, run_id, project_id, harness_id, *, messages="[]") -> None:
    """Insert a run with the given `messages` — no `conversation_id` column:
    these fixtures are built against schema `5540e56092f1`, the revision just
    before this migration adds that column, so a run can only be attributed
    by the migration itself (via `conversations.messages`, for pass 1) rather
    than pre-seeded with one.
    """
    await conn.execute(
        text(
            "INSERT INTO runs (id, project_id, harness_id, task_type, task_input, "
            "document_ids, status, messages, input_tokens, output_tokens, cost_usd, "
            "iterations, created_at) "
            f"VALUES ('{run_id}', '{project_id}', '{harness_id}', 'chat', '{{}}'::jsonb, "
            f"'{{}}'::uuid[], 'completed', '{messages}'::jsonb, 0, 0, 0, 0, now())"
        )
    )


def _tool_result_message(child_run_id: str, *, tool_call_id: str = "tc1") -> dict:
    """The exact JSON `run_harness_task` (engine/tools.py) returns as its tool
    result, wrapped the way `engine/harness.py` actually persists it — a
    `{"role": "tool", ...}` entry built from the real `Msg` dataclass
    (providers/base.py), not a hand-typed dict — so this fixture is the real
    persisted shape, not a guess at it."""
    result = {
        "child_run_id": child_run_id,
        "status": "completed",
        "model_used": "test-model",
        "cost_usd": 0.01,
        "energy_wh": None,
        "co2e_g": None,
        "error": None,
        "findings": [],
        "note": "Findings are DRAFTS awaiting human approval — say so when you report them.",
    }
    return Msg(role="tool", content=json.dumps(result), tool_call_id=tool_call_id).to_json()


def _assistant_turn_message(run_id: str) -> dict:
    """The exact JSON a completed chat turn's assistant entry gets in
    `conversations.messages`, built by calling the real
    `api/chat.py::_assistant_message` against a transient (unpersisted) `Run`
    — proof, not assumption, that its `activity` entries never carry a
    `child_run_id` (see the migration's own docstring)."""
    run = RunRow(
        id=uuid.UUID(run_id),
        status="completed",
        task_input={},
        compactions=[],
        messages=[
            Msg(
                role="assistant",
                content="Done.",
                tool_calls=[ToolCall(id="tc1", name="run_harness_task", arguments={"task_type": "x"})],
            ).to_json(),
            {"role": "tool", "content": "irrelevant", "tool_call_id": "tc1"},
            Msg(role="assistant", content="Done.").to_json(),
        ],
        model_used="test-model",
        cost_usd=0,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        energy_wh=None,
        energy_accounting=None,
        routing=None,
        grounding=None,
    )
    out = _assistant_message(run)
    out["run_id"] = run_id  # _assistant_message always stamps its own run's id
    return out


async def test_conversation_id_backfill_attributes_the_top_level_turn(engine):
    """Pass 1 of 9ba228f09f91's backfill: a conversation's own `messages`
    names the run for its turn via `run_id`. The assistant entry is built
    through the real `_assistant_message` to also prove, positively, that its
    `activity` list carries no `child_run_id` for the backfill's second pass
    to (wrongly) rely on — see `test_conversation_id_backfill_propagates_to_
    delegated_runs_via_parent_messages` for how a delegated run is actually
    reached."""
    workspace_id = "10000000-0000-0000-0000-000000000001"
    project_id = "10000000-0000-0000-0000-000000000002"
    harness_id = "10000000-0000-0000-0000-000000000003"
    conversation_id = "10000000-0000-0000-0000-000000000004"
    turn_run_id = "10000000-0000-0000-0000-000000000005"
    orphan_run_id = "10000000-0000-0000-0000-000000000007"

    assistant_entry = _assistant_turn_message(turn_run_id)
    assert assistant_entry["activity"] and "child_run_id" not in assistant_entry["activity"][0]
    messages = json.dumps(
        [{"role": "user", "content": "hi", "run_id": None}, assistant_entry]
    )

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "5540e56092f1")  # just before conversation_id exists
        await _insert_workspace_project_harness(conn, workspace_id, project_id, harness_id)
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                f"VALUES ('{conversation_id}', '{project_id}', '{harness_id}', 'Convo', "
                f"'{messages}'::jsonb, now(), now())"
            )
        )
        await _insert_run(conn, turn_run_id, project_id, harness_id)
        await _insert_run(conn, orphan_run_id, project_id, harness_id)
        await conn.commit()

        await conn.run_sync(_upgrade, "9ba228f09f91")
        await conn.commit()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, conversation_id FROM runs"))).all()
    attributed = {str(rid): str(cid) if cid else None for rid, cid in rows}
    assert attributed[turn_run_id] == conversation_id
    assert attributed[orphan_run_id] is None


async def test_conversation_id_backfill_propagates_to_delegated_runs_via_parent_messages(engine):
    """9ba228f09f91's second pass: a delegated run's id is NOT in
    `conversations.messages` (nothing ever writes it there — see the
    migration's docstring and the previous test). It IS in the delegating
    run's own `messages`, as the `child_run_id` field of the `run_harness_task`
    tool result. This backfills a child from its parent's messages, and a
    grandchild from the child's — proving the pass repeats far enough to match
    `MAX_DELEGATION_DEPTH` (engine/tools.py)."""
    workspace_id = "11000000-0000-0000-0000-000000000001"
    project_id = "11000000-0000-0000-0000-000000000002"
    harness_id = "11000000-0000-0000-0000-000000000003"
    conversation_id = "11000000-0000-0000-0000-000000000004"
    parent_run_id = "11000000-0000-0000-0000-000000000005"
    child_run_id = "11000000-0000-0000-0000-000000000006"
    grandchild_run_id = "11000000-0000-0000-0000-000000000007"
    unrelated_run_id = "11000000-0000-0000-0000-000000000008"

    # parent_run_id is attributed by pass 1, the normal way (its conversation
    # names it via `run_id`) — propagation then has a starting point to walk
    # from for the rest of this test, exactly as it would for a real
    # already-attributed run.
    parent_messages = json.dumps(
        [
            {"role": "user", "content": "go", "tool_calls": [], "tool_call_id": None, "meta": {}},
            _tool_result_message(child_run_id),
        ]
    )
    child_messages = json.dumps([_tool_result_message(grandchild_run_id, tool_call_id="tc2")])

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "5540e56092f1")
        await _insert_workspace_project_harness(conn, workspace_id, project_id, harness_id)
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                f"VALUES ('{conversation_id}', '{project_id}', '{harness_id}', 'Convo', "
                f"'{json.dumps([_assistant_turn_message(parent_run_id)])}'::jsonb, now(), now())"
            )
        )
        await _insert_run(conn, parent_run_id, project_id, harness_id, messages=parent_messages)
        await _insert_run(conn, child_run_id, project_id, harness_id, messages=child_messages)
        await _insert_run(conn, grandchild_run_id, project_id, harness_id)
        await _insert_run(conn, unrelated_run_id, project_id, harness_id)
        await conn.commit()

        await conn.run_sync(_upgrade, "9ba228f09f91")
        await conn.commit()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, conversation_id FROM runs"))).all()
    attributed = {str(rid): str(cid) if cid else None for rid, cid in rows}
    assert attributed[parent_run_id] == conversation_id
    assert attributed[child_run_id] == conversation_id
    assert attributed[grandchild_run_id] == conversation_id
    assert attributed[unrelated_run_id] is None


async def test_conversation_id_backfill_survives_malformed_messages(engine):
    """A `conversations.messages` or `runs.messages` value that is not a JSON
    array (a hand edit, a partial restore) must not abort the migration — the
    `jsonb_typeof` guard on both should make the migration complete and skip
    only that row's contribution, not crash the whole batch's transaction."""
    workspace_id = "12000000-0000-0000-0000-000000000001"
    project_id = "12000000-0000-0000-0000-000000000002"
    harness_id = "12000000-0000-0000-0000-000000000003"
    bad_conversation_id = "12000000-0000-0000-0000-000000000004"
    good_conversation_id = "12000000-0000-0000-0000-000000000009"
    # Attributed by pass 1 (a well-formed conversation names it), then its
    # OWN `messages` — the malformed value pass 2 must survive — is scanned
    # as a delegation parent once that attribution lands.
    malformed_run_id = "12000000-0000-0000-0000-000000000005"

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "5540e56092f1")
        await _insert_workspace_project_harness(conn, workspace_id, project_id, harness_id)
        # messages is a bare JSON object, not an array — must not abort pass 1.
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                f"VALUES ('{bad_conversation_id}', '{project_id}', '{harness_id}', 'Convo', "
                "'{\"not\": \"an array\"}'::jsonb, now(), now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                f"VALUES ('{good_conversation_id}', '{project_id}', '{harness_id}', 'Convo', "
                f"'{json.dumps([_assistant_turn_message(malformed_run_id)])}'::jsonb, now(), now())"
            )
        )
        # This run's own messages is a bare JSON object, not an array — must
        # not abort pass 2 once this run is a delegation-parent candidate.
        await _insert_run(
            conn, malformed_run_id, project_id, harness_id,
            messages='{"not": "an array either"}',
        )
        await conn.commit()

        # The point of this test: this must not raise.
        await conn.run_sync(_upgrade, "9ba228f09f91")
        await conn.commit()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, conversation_id FROM runs"))).all()
    assert {str(rid): str(cid) if cid else None for rid, cid in rows} == {
        malformed_run_id: good_conversation_id
    }


async def test_conversation_id_backfill_ignores_malformed_run_ids(engine):
    """A `run_id`/`child_run_id` that is not uuid-shaped must not abort the
    migration (the uuid cast is gated inside a CASE, structurally unreachable
    for a non-matching value) — the migration completes and simply leaves
    that entry unattributed."""
    workspace_id = "13000000-0000-0000-0000-000000000001"
    project_id = "13000000-0000-0000-0000-000000000002"
    harness_id = "13000000-0000-0000-0000-000000000003"
    conversation_id = "13000000-0000-0000-0000-000000000004"
    parent_run_id = "13000000-0000-0000-0000-000000000005"

    # A malformed top-level run_id, alongside the well-formed entry that
    # attributes parent_run_id via pass 1 so pass 2 has a real parent to walk
    # from.
    messages = json.dumps(
        [
            {"role": "user", "content": "hi", "run_id": "not-a-uuid"},
            _assistant_turn_message(parent_run_id),
        ]
    )
    parent_messages = json.dumps([_tool_result_message("also-not-a-uuid")])

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "5540e56092f1")
        await _insert_workspace_project_harness(conn, workspace_id, project_id, harness_id)
        await conn.execute(
            text(
                "INSERT INTO conversations "
                "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                f"VALUES ('{conversation_id}', '{project_id}', '{harness_id}', 'Convo', "
                f"'{messages}'::jsonb, now(), now())"
            )
        )
        await _insert_run(conn, parent_run_id, project_id, harness_id, messages=parent_messages)
        await conn.commit()

        await conn.run_sync(_upgrade, "9ba228f09f91")  # must not raise
        await conn.commit()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, conversation_id FROM runs"))).all()
    assert {str(rid): str(cid) if cid else None for rid, cid in rows} == {
        parent_run_id: conversation_id
    }


async def test_conversation_id_backfill_is_batched_across_many_conversations(engine):
    """The backfill pages through `conversations` rather than joining the
    whole table in one statement — assert it still attributes every run
    correctly across more conversations than one batch (BATCH_SIZE=500 in the
    migration; a small multiple here keeps the test itself fast)."""
    n = 12
    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "5540e56092f1")
        await conn.execute(
            text(
                "INSERT INTO workspaces (id, name, settings, created_at) "
                "VALUES ('20000000-0000-0000-0000-000000000001', 'W', '{}'::jsonb, now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, workspace_id, name, created_at) "
                "VALUES ('20000000-0000-0000-0000-000000000002', "
                "'20000000-0000-0000-0000-000000000001', 'P', now())"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO harnesses (id, workspace_id, name, task_profile, model_policy, "
                "tool_names, loop_config, is_archived, created_at, updated_at) "
                "VALUES ('20000000-0000-0000-0000-000000000003', "
                "'20000000-0000-0000-0000-000000000001', 'H', 'chat', '{}'::jsonb, "
                "'{}'::text[], '{}'::jsonb, false, now(), now())"
            )
        )
        pairs = []
        for i in range(n):
            conv_id = f"30000000-0000-0000-0000-{i:012d}"
            run_id = f"40000000-0000-0000-0000-{i:012d}"
            pairs.append((conv_id, run_id))
            await conn.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, project_id, harness_id, title, messages, created_at, updated_at) "
                    f"VALUES ('{conv_id}', '20000000-0000-0000-0000-000000000002', "
                    "'20000000-0000-0000-0000-000000000003', 'Convo', "
                    f"'[{{\"role\": \"assistant\", \"content\": \"ok\", "
                    f"\"run_id\": \"{run_id}\"}}]'::jsonb, now(), now())"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO runs (id, project_id, harness_id, task_type, task_input, "
                    "document_ids, status, messages, input_tokens, output_tokens, cost_usd, "
                    "iterations, created_at) "
                    f"VALUES ('{run_id}', '20000000-0000-0000-0000-000000000002', "
                    "'20000000-0000-0000-0000-000000000003', 'chat', '{}'::jsonb, "
                    "'{}'::uuid[], 'completed', '[]'::jsonb, 0, 0, 0, 0, now())"
                )
            )
        await conn.commit()

        await conn.run_sync(_upgrade, "9ba228f09f91")
        await conn.commit()

    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT id, conversation_id FROM runs"))).all()
    attributed = {str(rid): str(cid) for rid, cid in rows}
    for conv_id, run_id in pairs:
        assert attributed[run_id] == conv_id


async def test_legacy_database_already_at_head_is_stamped_without_migrating(engine):
    """A create_all database built by the *current* release: schema is right,
    stamp is missing. Stamp head; run nothing."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    plan = await ensure_schema(engine)
    assert plan is not None and plan.kind == "legacy"
    assert plan.stamp == head_revision()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


async def test_the_documented_manual_recovery_actually_works(engine):
    """docs/upgrading.md tells operators to run `alembic stamp <rev>` then
    `alembic upgrade head`. Assert that path, not just the automatic one."""
    await _make_legacy_create_all_database(engine)
    async with engine.connect() as conn:
        await conn.run_sync(_stamp, LEGACY_V01)
        await conn.commit()
        await conn.run_sync(_upgrade, "head")
        await conn.commit()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


# ── downgrades are real ───────────────────────────────────────────────────────
async def test_downgrade_one_step_then_upgrade_again(engine):
    await ensure_schema(engine)
    script = ScriptDirectory.from_config(alembic_config())
    previous = script.get_revision(head_revision()).down_revision
    assert previous, "head has no down_revision; adjust this test"

    async with engine.connect() as conn:
        await conn.run_sync(_downgrade, "-1")
        await conn.commit()
    assert await _heads(engine) == frozenset({previous})

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade, "head")
        await conn.commit()
    assert await _heads(engine) == frozenset({head_revision()})
    assert await _diff(engine) == []


# ── the app itself boots on both ──────────────────────────────────────────────
@pytest.fixture
def app_against(monkeypatch, tmp_path):
    """Point the app's cached engine/settings at `url`, and return create_app()."""

    def build(url: str):
        from tret import config
        from tret.db import engine as engine_module

        config.get_settings.cache_clear()
        monkeypatch.setenv("TRET_DATABASE_URL", url)
        monkeypatch.setenv("TRET_STORAGE_DIR", str(tmp_path / "storage"))
        monkeypatch.setattr(engine_module, "_engine", None)
        monkeypatch.setattr(engine_module, "_session_factory", None)

        from tret.main import create_app

        return create_app()

    yield build
    from tret import config

    config.get_settings.cache_clear()


def _boot_and_check(app) -> None:
    from fastapi.testclient import TestClient

    with TestClient(app) as client:  # entering the context runs the lifespan
        assert client.get("/api/healthz").json() == {"ok": True}


async def test_app_boots_against_an_empty_database(database, app_against):
    await asyncio.to_thread(_boot_and_check, app_against(database))
    engine = create_async_engine(database)
    try:
        assert await _heads(engine) == frozenset({head_revision()})
    finally:
        await engine.dispose()


async def test_app_boots_against_a_legacy_create_all_database(database, app_against):
    """The crash-loop scenario, end to end: an unstamped v0.1 database, the real
    startup path, and a working app afterwards."""
    engine = create_async_engine(database)
    try:
        await _make_legacy_create_all_database(engine)
    finally:
        await engine.dispose()

    await asyncio.to_thread(_boot_and_check, app_against(database))

    engine = create_async_engine(database)
    try:
        assert await _heads(engine) == frozenset({head_revision()})
        assert await _diff(engine) == []
    finally:
        await engine.dispose()
