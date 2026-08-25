"""Cross-workspace isolation, exercised against a real Postgres.

Skipped unless `TRET_TEST_POSTGRES_URL` points at a server the tests may create
and drop databases on — see tests/test_migrations_postgres.py's module
docstring for the local invocation. A real database rather than a fake session
is deliberate here: the query shapes under test (harnesses.py, runs.py,
findings.py, documents.py, analytics.py's group-bys) are exactly the kind a
hand-rolled fake would get subtly wrong, and what this file checks — that one
workspace's rows are genuinely invisible to another — is a claim about SQL.

Two workspaces (A and B), one member each, one project/harness/run/finding/
document seeded per workspace. Every test logs in as A and reaches for
something that belongs to B: a list must not include it, a get-by-id must 404
(never 403 — existence itself must not leak), and a create that names a
foreign id must be refused the same way.
"""
from __future__ import annotations

import os
import uuid

import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient

from tret.db.models import (
    Document,
    Finding,
    Harness,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceMember,
)

# Same server, same fixtures, same skip condition as test_migrations_postgres.py
# — see that module's docstring for the local invocation.
from test_migrations_postgres import app_against, database  # noqa: F401  (fixtures)

ADMIN_URL = os.environ.get("TRET_TEST_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not ADMIN_URL,
    reason="set TRET_TEST_POSTGRES_URL to a Postgres server where tests may create databases",
)

_HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


class Tenant:
    """Everything seeded for one workspace, so tests can say `a.harness.id`."""

    __slots__ = ("workspace", "user", "project", "harness", "run", "finding", "document")

    def __init__(self, workspace, user, project, harness, run, finding, document):
        self.workspace = workspace
        self.user = user
        self.project = project
        self.harness = harness
        self.run = run
        self.finding = finding
        self.document = document


async def _seed_tenant(db, *, name: str, email: str) -> Tenant:
    workspace = Workspace(name=name, kind="team")
    user = User(
        email=email,
        display_name=name,
        password_hash=_HASHER.hash(PASSWORD),
        role="admin",
    )
    db.add_all([workspace, user])
    await db.flush()
    db.add(WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="owner"))

    project = Project(workspace_id=workspace.id, name=f"{name} Project")
    db.add(project)
    await db.flush()

    harness = Harness(
        workspace_id=workspace.id,
        name=f"{name} Harness",
        task_profile="freeform",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    db.add(harness)
    await db.flush()

    run = Run(
        project_id=project.id,
        harness_id=harness.id,
        task_type="freeform",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        created_by=user.id,
    )
    db.add(run)
    await db.flush()

    finding = Finding(
        run_id=run.id,
        project_id=project.id,
        schema_slug="draft_section",
        subject={"deliverable": "d", "section": "s"},
        payload={"markdown": "body"},
        provenance={"model": "anthropic/test"},
        status="draft",
    )
    document = Document(
        project_id=project.id,
        filename="notes.txt",
        content_type="text/plain",
        byte_size=5,
        storage_path=f"/tmp/{uuid.uuid4()}.txt",
        sha256=uuid.uuid4().hex,
        extraction_status="done",
    )
    db.add_all([finding, document])
    await db.commit()

    return Tenant(workspace, user, project, harness, run, finding, document)


@pytest.fixture
async def tenants(database):  # noqa: F811  (pytest fixture, not a redefinition)
    """(tenant_a, tenant_b): two fully-seeded, isolated workspaces.

    Migrated and seeded through a throwaway engine of our own, entirely
    before `client` below ever creates the app. `TestClient` runs the app's
    lifespan (and every request) on a portal thread with its own event loop;
    reusing a connection made in this fixture's loop from inside that portal
    would cross loops and asyncpg refuses that outright. Disposing this
    engine before `client` opens its `TestClient` keeps the two fully apart —
    the app migrates again on boot (a no-op: already at head) and bootstraps
    its own separate Default workspace, which these tests never touch.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from tret.db.migrate import ensure_schema

    engine = create_async_engine(database)
    try:
        await ensure_schema(engine)
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as db:
            tenant_a = await _seed_tenant(db, name="Alpha", email="alpha@example.com")
            tenant_b = await _seed_tenant(db, name="Bravo", email="bravo@example.com")
    finally:
        await engine.dispose()
    return tenant_a, tenant_b


@pytest.fixture
def client(database, app_against, tenants) -> TestClient:  # noqa: F811  (pytest fixture params)
    app = app_against(database)
    with TestClient(app) as c:  # entering the context runs the lifespan (migrate + bootstrap)
        yield c


def login_as(client: TestClient, user: User) -> None:
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text


# ── harnesses ──────────────────────────────────────────────────────────────
async def test_harness_list_is_scoped_to_the_current_workspace(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    ids = {h["id"] for h in client.get("/api/harnesses").json()}
    assert str(a.harness.id) in ids
    assert str(b.harness.id) not in ids


async def test_a_foreign_harness_is_a_404_not_a_403(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    response = client.get(f"/api/harnesses/{b.harness.id}")
    assert response.status_code == 404


async def test_a_created_harness_belongs_to_the_creators_workspace(client, tenants):
    a, _b = tenants
    login_as(client, a.user)
    response = client.post(
        "/api/harnesses",
        json={"name": "New Harness", "task_profile": "freeform", "tool_names": []},
    )
    assert response.status_code == 200
    listed = {h["id"] for h in client.get("/api/harnesses").json()}
    assert response.json()["id"] in listed
    # And Bravo, logged in separately, never sees it.


async def test_a_foreign_harness_cannot_be_archived(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    response = client.delete(f"/api/harnesses/{b.harness.id}")
    assert response.status_code == 404


# ── runs ───────────────────────────────────────────────────────────────────
async def test_run_list_is_scoped_to_the_current_workspace(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    ids = {r["id"] for r in client.get("/api/runs").json()}
    assert str(a.run.id) in ids
    assert str(b.run.id) not in ids


async def test_a_foreign_run_is_a_404(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    assert client.get(f"/api/runs/{b.run.id}").status_code == 404
    assert client.post(f"/api/runs/{b.run.id}/cancel").status_code == 404


async def test_a_run_cannot_be_created_against_a_foreign_project(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    response = client.post(
        "/api/runs",
        json={"harness_id": str(a.harness.id), "project_id": str(b.project.id)},
    )
    assert response.status_code == 404


async def test_a_run_cannot_be_created_with_a_foreign_document_id(client, tenants):
    """document_ids is attached to a run unvalidated at the engine (which loads
    them by id alone, unscoped) — the API must refuse a foreign id itself."""
    a, b = tenants
    login_as(client, a.user)
    response = client.post(
        "/api/runs",
        json={"harness_id": str(a.harness.id), "document_ids": [str(b.document.id)]},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Document not found"
    # And no run was created as a side effect of the rejected request.
    ids = {r["id"] for r in client.get("/api/runs").json()}
    assert len(ids) == 1
    assert str(a.run.id) in ids


# ── findings ───────────────────────────────────────────────────────────────
async def test_finding_list_is_scoped_to_the_current_workspace(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    ids = {f["id"] for f in client.get("/api/findings").json()}
    assert str(a.finding.id) in ids
    assert str(b.finding.id) not in ids


async def test_a_foreign_finding_is_a_404(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    assert client.get(f"/api/findings/{b.finding.id}").status_code == 404


async def test_a_foreign_finding_cannot_be_approved(client, tenants):
    """404, not 403: an owner of workspace A ranks high enough to approve —
    what stops this is that the finding is not in A's workspace at all."""
    a, b = tenants
    login_as(client, a.user)
    response = client.post(f"/api/findings/{b.finding.id}/approval", json={"action": "approve"})
    assert response.status_code == 404


# ── documents ──────────────────────────────────────────────────────────────
async def test_document_list_is_scoped_to_the_current_workspace(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    ids = {d["id"] for d in client.get("/api/documents").json()}
    assert str(a.document.id) in ids
    assert str(b.document.id) not in ids


async def test_a_foreign_document_is_a_404(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    assert client.get(f"/api/documents/{b.document.id}").status_code == 404


# ── analytics ──────────────────────────────────────────────────────────────
async def test_guardrails_defaults_to_the_current_workspaces_project(client, tenants):
    a, _b = tenants
    login_as(client, a.user)
    response = client.get("/api/analytics/guardrails")
    assert response.status_code == 200
    assert response.json()["project_id"] == str(a.project.id)


async def test_a_foreign_project_id_is_refused_on_every_analytics_endpoint(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    for path in (
        "/api/analytics/guardrails",
        "/api/analytics/emissions",
        "/api/analytics/routing",
        "/api/analytics/routing/history",
    ):
        response = client.get(path, params={"project_id": str(b.project.id)})
        assert response.status_code == 404, path


async def test_analytics_never_reports_a_foreign_workspaces_project(client, tenants):
    """Without a project_id, the default must be *this* workspace's project —
    the pre-tenancy hole where an unscoped scan quietly spanned every
    workspace in the database."""
    a, b = tenants
    login_as(client, b.user)
    response = client.get("/api/analytics/guardrails")
    assert response.status_code == 200
    assert response.json()["project_id"] == str(b.project.id)
    assert response.json()["project_id"] != str(a.project.id)


# ── the switch itself never crosses a boundary ────────────────────────────
async def test_switching_to_a_foreign_workspace_is_refused(client, tenants):
    a, b = tenants
    login_as(client, a.user)
    response = client.post("/api/auth/workspace", json={"workspace_id": str(b.workspace.id)})
    assert response.status_code == 404
