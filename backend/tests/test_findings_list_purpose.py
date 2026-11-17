"""`GET /findings` rows carry `approval_purpose`, fetched in one batched query.

The chat result cards need to know whether a finding was approved by policy
("auto") or by a person ("review") without fetching every finding's detail, so
the list rows include the latest approval's purpose (null when none).
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import ARRAY, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.api import findings as findings_api
from tret.api.workspace import WorkspaceContext
from tret.db.models import Approval, Base, Finding, Workspace

T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'purpose.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    project_id, run_id, approver = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _project(db, workspace_id):
        return types.SimpleNamespace(id=project_id)

    monkeypatch.setattr(findings_api, "current_project", _project)
    statements: list[str] = []

    @event.listens_for(engine.sync_engine, "before_cursor_execute")
    def _count(conn, cursor, statement, *args):
        statements.append(statement)

    async def add_finding(n: int, approvals: list[tuple[str, str]]) -> uuid.UUID:
        """`approvals` is [(action, purpose)] oldest first."""
        fid = uuid.uuid4()
        async with factory() as db:
            db.add(
                Finding(
                    id=fid,
                    run_id=run_id,
                    project_id=project_id,
                    schema_slug="draft_section",
                    subject={"n": n},
                    payload={},
                    provenance={},
                    status="approved",
                    created_at=T0 + timedelta(minutes=n),
                )
            )
            await db.flush()
            for i, (action, purpose) in enumerate(approvals):
                db.add(
                    Approval(
                        finding_id=fid,
                        action=action,
                        approver_id=approver,
                        purpose=purpose,
                        created_at=T0 + timedelta(minutes=n, seconds=i + 1),
                    )
                )
            await db.commit()
        return fid

    async def listing(**params):
        async with factory() as db:
            statements.clear()
            ctx = WorkspaceContext(Workspace(id=uuid.uuid4(), name="W", kind="team"), "owner")
            rows = await findings_api.list_findings(user=None, ctx=ctx, db=db, **{"run_id": run_id, **params})
            return rows, list(statements)

    yield types.SimpleNamespace(add=add_finding, listing=listing)
    await engine.dispose()


async def test_rows_carry_review_auto_and_none(env):
    review = await env.add(1, [("approve", "review")])
    auto = await env.add(2, [("approve", "auto")])
    none = await env.add(3, [])
    rows, _ = await env.listing(limit=100)
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(review)]["approval_purpose"] == "review"
    assert by_id[str(auto)]["approval_purpose"] == "auto"
    assert by_id[str(none)]["approval_purpose"] is None
    # additive: the existing fields are all still there
    assert {"id", "run_id", "schema_slug", "subject", "payload", "provenance", "status", "created_at"} <= set(
        by_id[str(auto)]
    )


async def test_the_latest_approval_row_decides(env):
    fid = await env.add(1, [("approve", "auto"), ("reject", "review"), ("approve", "packaging")])
    rows, _ = await env.listing()
    assert rows[0]["id"] == str(fid) and rows[0]["approval_purpose"] == "packaging"


async def test_purposes_cost_one_extra_query_regardless_of_row_count(env):
    await env.add(1, [("approve", "review")])
    _, few = await env.listing()
    for n in range(2, 12):
        await env.add(n, [("approve", "auto" if n % 2 else "review")])
    rows, many = await env.listing()
    assert len(rows) == 11
    assert len(many) == len(few) == 2  # the findings query + one approvals query
    assert sum("approvals" in s for s in many) == 1


async def test_empty_listing_makes_no_approval_query(env):
    rows, statements = await env.listing()
    assert rows == []
    assert not any("approvals" in s for s in statements)
