"""Auto-approval policy: resolver, config, the three creation sites, the "auto"
purpose (never accepted from the public API, never a review) and the settings
endpoint. Real sqlite throughout; the SharePoint upload is mocked on
`tret.services.connections.upload_connected_file`."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, auto_approve as auto_approve_api, findings as findings_api
from tret.config import Settings, get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Approval, Base, Finding, Harness, Project, Run, User, Workspace, WorkspaceMember,
)
from tret.engine import tools as tools_module
from tret.engine.tools import RunContext, draft_section, propose_connected_write, record_finding, record_verdict
from tret.services import connections as connections_service
from tret.services.auto_approve import (
    AUTO_APPROVE_CATEGORIES, category_for, maybe_auto_approve, resolve_auto_approve,
)
from tret.services.outcomes import _finding_counts, latest_approval_purposes

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"
URL = "/api/workspace/settings/auto-approve"
BANNER_MD = "**Draft status:** awaiting review by a named human reviewer.\n\n# Governance\n\n" + "Some drafted prose. " * 5
SCHEMAS = {"finding": {"type": "object"}, "verdict": {"type": "object"}}


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    monkeypatch.delenv("TRET_AUTO_APPROVE_DEFAULT", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def world(session_factory):
    """A workspace + project + harness + user, and a run that user started."""
    user = User(id=uuid.uuid4(), email="u@example.com", display_name="U",
                password_hash=HASHER.hash(PASSWORD), role="admin")
    ws = Workspace(id=uuid.uuid4(), name="WS", kind="team", settings={})
    project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
    harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="H")
    async with session_factory() as db:
        db.add_all([user, ws, WorkspaceMember(user_id=user.id, workspace_id=ws.id, role="owner")])
        await db.flush()
        db.add_all([project, harness])
        await db.flush()
        run = Run(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, task_type="freeform",
                  task_input={}, document_ids=[], status="running", messages=[], created_by=user.id)
        orphan = Run(id=uuid.uuid4(), project_id=project.id, harness_id=harness.id, task_type="freeform",
                     task_input={}, document_ids=[], status="running", messages=[], created_by=None)
        db.add_all([run, orphan])
        await db.commit()
    return SimpleNamespace(user=user, ws=ws, project=project, run=run, orphan=orphan)


def _ctx(db, world, run=None) -> RunContext:
    return RunContext(
        db=db, run_id=(run or world.run).id, project_id=world.project.id, pack_id=None,
        doctrine_sha=None, model_used=None, document_ids=[], output_schemas=SCHEMAS,
        workspace_id=world.ws.id,
    )


async def _set_ws_settings(session_factory, world, settings):
    async with session_factory() as db:
        ws = await db.get(Workspace, world.ws.id)
        ws.settings = settings
        await db.commit()


async def _approvals(session_factory, finding_id):
    async with session_factory() as db:
        return (await db.execute(select(Approval).where(Approval.finding_id == finding_id))).scalars().all()


async def _only_finding(session_factory, slug=None) -> Finding:
    async with session_factory() as db:
        q = select(Finding)
        if slug:
            q = q.where(Finding.schema_slug == slug)
        return (await db.execute(q)).scalars().one()


# ── resolver + config ────────────────────────────────────────────────────────
def test_resolver_default_override_empty_and_unknown():
    assert resolve_auto_approve(None, []) == frozenset()
    assert resolve_auto_approve({}, ["findings"]) == {"findings"}
    assert resolve_auto_approve({"auto_approve": ["draft_section"]}, ["findings"]) == {"draft_section"}
    # An empty list is "off for this workspace", not "fall back to the default".
    assert resolve_auto_approve({"auto_approve": []}, ["findings"]) == frozenset()
    assert resolve_auto_approve({"auto_approve": ["bogus", "findings", "package_map"]}, []) == {"findings"}
    assert resolve_auto_approve({"auto_approve": "findings"}, ["draft_section"]) == {"draft_section"}


def test_category_for():
    def f(slug):
        return SimpleNamespace(schema_slug=slug)

    assert category_for(f("draft_section")) == "draft_section"
    assert category_for(f("connected_write")) == "connected_write"
    assert category_for(f("package_map")) is None
    assert category_for(f("verdict")) == "findings"
    assert category_for(f("anything_else")) == "findings"


def test_config_parses_and_validates(monkeypatch):
    assert Settings().auto_approve_default == []
    assert Settings(auto_approve_default="").auto_approve_default == []
    assert Settings(auto_approve_default="findings, draft_section").auto_approve_default == [
        "findings", "draft_section"]
    assert set(Settings(auto_approve_default=",".join(AUTO_APPROVE_CATEGORIES)).auto_approve_default) == set(
        AUTO_APPROVE_CATEGORIES)
    with pytest.raises(ValueError):
        Settings(auto_approve_default="findings,package_map")
    monkeypatch.setenv("TRET_AUTO_APPROVE_DEFAULT", "connected_write")
    assert Settings().auto_approve_default == ["connected_write"]


# ── the three creation sites ─────────────────────────────────────────────────
async def test_draft_section_auto_approved_strips_banner(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["draft_section"]})
    async with session_factory() as db:
        text = await draft_section(_ctx(db, world), "report", "gov", BANNER_MD)
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "approved"
    assert "approved automatically" in text and "awaiting" not in text and str(f.id) in text
    assert "Draft status" not in f.payload["markdown"] and f.payload["stripped_draft_banner"]
    [a] = await _approvals(session_factory, f.id)
    assert (a.purpose, a.action, a.approver_id) == ("auto", "approve", world.user.id)


async def test_disabled_category_stays_draft(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["findings"]})
    async with session_factory() as db:
        text = await draft_section(_ctx(db, world), "report", "gov", BANNER_MD)
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "draft" and "draft finding" in text
    assert await _approvals(session_factory, f.id) == []


async def test_deployment_default_applies_without_workspace_key(session_factory, world, monkeypatch):
    monkeypatch.setenv("TRET_AUTO_APPROVE_DEFAULT", "draft_section")
    get_settings.cache_clear()
    async with session_factory() as db:
        await draft_section(_ctx(db, world), "report", "gov", BANNER_MD)
        await db.commit()
    assert (await _only_finding(session_factory)).status == "approved"


async def test_workspace_empty_override_beats_default(session_factory, world, monkeypatch):
    monkeypatch.setenv("TRET_AUTO_APPROVE_DEFAULT", "draft_section")
    get_settings.cache_clear()
    await _set_ws_settings(session_factory, world, {"auto_approve": []})
    async with session_factory() as db:
        await draft_section(_ctx(db, world), "report", "gov", BANNER_MD)
        await db.commit()
    assert (await _only_finding(session_factory)).status == "draft"


@pytest.mark.parametrize("tool", [record_finding, record_verdict])
async def test_findings_and_verdicts_auto_approved(session_factory, world, tool):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["findings"]})
    async with session_factory() as db:
        text = await tool(_ctx(db, world), "finding", {"site_id": "S1"}, {"verdict": "ok"})
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "approved" and "approved automatically" in text
    [a] = await _approvals(session_factory, f.id)
    assert a.purpose == "auto" and a.approver_id == world.user.id


async def test_noted_finding_is_untouched(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": list(AUTO_APPROVE_CATEGORIES)})
    async with session_factory() as db:
        text = await record_verdict(
            _ctx(db, world), "verdict", {"site_id": "S1"}, {"verdict": "insufficient_data"})
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "noted" and "noted" in text
    assert await _approvals(session_factory, f.id) == []


async def test_run_without_creator_stays_draft(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": list(AUTO_APPROVE_CATEGORIES)})
    async with session_factory() as db:
        text = await draft_section(_ctx(db, world, world.orphan), "report", "gov", BANNER_MD)
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "draft" and "approved automatically" not in text
    # Direct call too: no actor, no approval.
    async with session_factory() as db:
        assert await maybe_auto_approve(db, f.id, workspace_id=world.ws.id, actor_user_id=None) is False


async def test_package_map_is_never_auto_approved(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": list(AUTO_APPROVE_CATEGORIES)})
    async with session_factory() as db:
        f = Finding(run_id=world.run.id, project_id=world.project.id, schema_slug="package_map",
                    subject={}, payload={}, provenance={}, status="draft")
        db.add(f)
        await db.commit()
        assert await maybe_auto_approve(
            db, f.id, workspace_id=world.ws.id, actor_user_id=world.user.id) is False
        assert (await db.get(Finding, f.id)).status == "draft"
    assert await _approvals(session_factory, f.id) == []


async def test_failure_leaves_a_draft_and_never_raises(session_factory, world, monkeypatch):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["draft_section"]})

    async def _boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(findings_api, "_decide", _boom)
    async with session_factory() as db:
        text = await draft_section(_ctx(db, world), "report", "gov", BANNER_MD)
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "draft" and "draft finding" in text


async def test_connected_write_auto_approval_uploads(session_factory, world, monkeypatch):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["connected_write"]})
    target = SimpleNamespace(slug="fin", label="Finance", path="/Documents/tret")

    async def _targets(db, workspace_id):
        return [target]

    uploads: list[dict] = []

    async def _upload(db, **kw):
        uploads.append(kw)
        return SimpleNamespace(item_id="item-1", web_url="https://sp.example/x")

    monkeypatch.setattr(connections_service, "list_write_targets", _targets)
    monkeypatch.setattr(connections_service, "safe_upload_path", lambda n: n, raising=False)
    monkeypatch.setattr(connections_service, "upload_connected_file", _upload)
    async with session_factory() as db:
        text = await propose_connected_write(
            _ctx(db, world), target="fin", filename="notes.md", content="hello")
        await db.commit()
    f = await _only_finding(session_factory)
    assert f.status == "approved"
    assert len(uploads) == 1 and uploads[0]["actor_user_id"] == world.user.id
    assert uploads[0]["filename"] == "notes.md"
    assert f.payload["upload"]["status"] == "uploaded"
    assert "approved automatically" in text and "https://sp.example/x" in text
    [a] = await _approvals(session_factory, f.id)
    assert a.purpose == "auto"


async def test_connected_write_disabled_does_not_upload(session_factory, world, monkeypatch):
    target = SimpleNamespace(slug="fin", label="Finance", path="/Documents/tret")

    async def _targets(db, workspace_id):
        return [target]

    async def _upload(db, **kw):
        raise AssertionError("must not upload an unapproved write")

    monkeypatch.setattr(connections_service, "list_write_targets", _targets)
    monkeypatch.setattr(connections_service, "safe_upload_path", lambda n: n, raising=False)
    monkeypatch.setattr(connections_service, "upload_connected_file", _upload)
    async with session_factory() as db:
        text = await propose_connected_write(
            _ctx(db, world), target="fin", filename="notes.md", content="hello")
        await db.commit()
    assert (await _only_finding(session_factory)).status == "draft" and "awaiting approval" in text
    assert tools_module  # imported for the shared module object


# ── "auto" is not a review ───────────────────────────────────────────────────
async def test_auto_approval_is_not_review_evidence(session_factory, world):
    await _set_ws_settings(session_factory, world, {"auto_approve": ["findings"]})
    async with session_factory() as db:
        await record_finding(_ctx(db, world), "finding", {"site_id": "S1"}, {"x": 1})
    f = await _only_finding(session_factory)
    async with session_factory() as db:
        assert (await latest_approval_purposes(db, [f.id]))[f.id] == "auto"
        counts = await _finding_counts(db, world.run.id)
    assert counts.get("approved", 0) == 0 and counts["approved_packaging"] == 1


# ── public API ───────────────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.include_router(auto_approve_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _login_as(client, session_factory, world, role):
    user = User(id=uuid.uuid4(), email=f"{role}-{uuid.uuid4().hex[:6]}@example.com", display_name=role,
                password_hash=HASHER.hash(PASSWORD), role="analyst")
    async with session_factory() as db:
        db.add_all([user, WorkspaceMember(user_id=user.id, workspace_id=world.ws.id, role=role)])
        await db.commit()
    r = await client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert r.status_code == 200, r.text


async def test_public_api_rejects_auto_purpose(client, session_factory, world):
    await _login_as(client, session_factory, world, "owner")
    async with session_factory() as db:
        f = Finding(run_id=world.run.id, project_id=world.project.id, schema_slug="verdict",
                    subject={}, payload={}, provenance={}, status="draft")
        db.add(f)
        await db.commit()
    r = await client.post(f"/api/findings/{f.id}/approval", json={"action": "approve", "purpose": "auto"})
    assert r.status_code == 422
    r = await client.post("/api/findings/decide-batch",
                          json={"finding_ids": [str(f.id)], "action": "approve", "purpose": "auto"})
    assert r.status_code == 422
    assert (await _only_finding(session_factory)).status == "draft"


@pytest.mark.parametrize("role,code", [("analyst", 403), ("approver", 403), ("admin", 200), ("owner", 200)])
async def test_put_role_checks(client, session_factory, world, role, code):
    await _login_as(client, session_factory, world, role)
    r = await client.put(URL, json={"categories": ["findings"]})
    assert r.status_code == code, r.text
    # Every member can read it.
    assert (await client.get(URL)).status_code == 200


async def test_get_put_and_clear(client, session_factory, world, monkeypatch):
    monkeypatch.setenv("TRET_AUTO_APPROVE_DEFAULT", "draft_section")
    get_settings.cache_clear()
    await _login_as(client, session_factory, world, "admin")
    assert (await client.get(URL)).json() == {
        "override": None, "effective": ["draft_section"], "default": ["draft_section"]}
    r = await client.put(URL, json={"categories": ["findings", "connected_write"]})
    assert r.json() == {
        "override": ["connected_write", "findings"],
        "effective": ["connected_write", "findings"], "default": ["draft_section"]}
    r = await client.put(URL, json={"categories": []})
    assert r.json()["override"] == [] and r.json()["effective"] == []
    r = await client.put(URL, json={"categories": None})
    assert r.json() == {"override": None, "effective": ["draft_section"], "default": ["draft_section"]}
    assert (await client.put(URL, json={"categories": ["package_map"]})).status_code == 422
    assert (await client.put(URL, json={"categories": [], "x": 1})).status_code == 422
    assert (await client.put(URL, json={})).status_code == 422


# ── actor must still be an active member ─────────────────────────────────────
async def test_removed_member_actor_stays_draft(session_factory, world):
    async with session_factory() as db:
        db.add(Finding(id=uuid.uuid4(), project_id=world.project.id, run_id=world.run.id,
                       schema_slug="draft_section", subject={"deliverable": "r", "section": "s"},
                       payload={"markdown": "x"}, status="draft", provenance={}))
        await db.commit()
    await _set_ws_settings(session_factory, world, {"auto_approve": ["draft_section"]})
    fid = (await _only_finding(session_factory)).id
    async with session_factory() as db:
        m = await db.get(WorkspaceMember, (world.user.id, world.ws.id))
        await db.delete(m)
        await db.commit()
    async with session_factory() as db:
        assert await maybe_auto_approve(db, fid, workspace_id=world.ws.id, actor_user_id=world.user.id) is False
    assert (await _only_finding(session_factory)).status == "draft"
    assert await _approvals(session_factory, fid) == []


async def test_disabled_actor_stays_draft_and_member_approves(session_factory, world):
    async with session_factory() as db:
        db.add(Finding(id=uuid.uuid4(), project_id=world.project.id, run_id=world.run.id,
                       schema_slug="draft_section", subject={"deliverable": "r", "section": "s"},
                       payload={"markdown": "x"}, status="draft", provenance={}))
        await db.commit()
    await _set_ws_settings(session_factory, world, {"auto_approve": ["draft_section"]})
    fid = (await _only_finding(session_factory)).id
    async with session_factory() as db:
        (await db.get(User, world.user.id)).disabled = True
        await db.commit()
    async with session_factory() as db:
        assert await maybe_auto_approve(db, fid, workspace_id=world.ws.id, actor_user_id=world.user.id) is False
    assert (await _only_finding(session_factory)).status == "draft"
    async with session_factory() as db:
        (await db.get(User, world.user.id)).disabled = False
        await db.commit()
    async with session_factory() as db:
        assert await maybe_auto_approve(db, fid, workspace_id=world.ws.id, actor_user_id=world.user.id) is True
    assert (await _only_finding(session_factory)).status == "approved"


# ── race guard ───────────────────────────────────────────────────────────────
async def test_concurrent_human_rejection_is_not_overwritten(session_factory, world, monkeypatch):
    """Session A holds the committed draft in its identity map (so a plain
    `db.get` would hand back the stale 'draft' row); session B rejects it with a
    human Approval; the auto path on A must then lose. Proves `_decide`
    re-reads the row (populate_existing) instead of trusting the identity map.
    Does NOT prove the row lock itself: sqlite ignores FOR UPDATE and both
    sessions share one StaticPool connection, so there is no real concurrency."""
    target = SimpleNamespace(slug="fin", label="Finance", path="/Documents/tret")

    async def _targets(db, workspace_id):
        return [target]

    async def _upload(db, **kw):
        raise AssertionError("must not upload a finding a human rejected")

    monkeypatch.setattr(connections_service, "list_write_targets", _targets)
    monkeypatch.setattr(connections_service, "safe_upload_path", lambda n: n, raising=False)
    monkeypatch.setattr(connections_service, "upload_connected_file", _upload)
    async with session_factory() as a:
        await propose_connected_write(_ctx(a, world), target="fin", filename="n.md", content="hi")
        await a.commit()
        fid = (await a.execute(select(Finding.id))).scalar_one()
        stale = await a.get(Finding, fid)
        assert stale.status == "draft"
        async with session_factory() as b:
            row = await b.get(Finding, fid)
            row.status = "rejected"
            b.add(Approval(finding_id=fid, action="reject", purpose="review", approver_id=world.user.id))
            await b.commit()
        assert stale.status == "draft"  # session A really is stale
        await _set_ws_settings(session_factory, world, {"auto_approve": ["connected_write"]})
        assert await maybe_auto_approve(a, fid, workspace_id=world.ws.id, actor_user_id=world.user.id) is False
    assert (await _only_finding(session_factory)).status == "rejected"
    [only] = await _approvals(session_factory, fid)
    assert (only.action, only.purpose) == ("reject", "review")


# ── export provenance ────────────────────────────────────────────────────────
async def _two_section_deliverable(session_factory, world):
    from tret.api.findings import _decide

    await _set_ws_settings(session_factory, world, {"auto_approve": ["draft_section"]})
    async with session_factory() as db:
        await draft_section(_ctx(db, world), "report", "auto_one", "Automatic body.")
        await db.commit()
    await _set_ws_settings(session_factory, world, {"auto_approve": []})
    async with session_factory() as db:
        await draft_section(_ctx(db, world), "report", "human_one", "Human body.")
        await db.commit()
        fid = (await db.execute(select(Finding.id).where(Finding.subject["section"].as_string() == "human_one"))).scalar_one()
        await _decide(db, fid, "approve", None, approver_id=world.user.id,
                      approver_name="U", workspace_id=world.ws.id)


async def test_export_labels_automatic_sections(session_factory, world, monkeypatch):
    import sys
    import types

    from tret.services.export import assemble_deliverable, render_html, render_pdf

    await _two_section_deliverable(session_factory, world)
    async with session_factory() as db:
        out = await assemble_deliverable(db, world.project.id, "report")
    by = {s["section"]: s for s in out["sections"]}
    assert by["auto_one"]["approval_purpose"] == "auto" and by["human_one"]["approval_purpose"] == "review"
    report = render_html(out["html"], "R", out["sections"], None, theme="report")
    assert report.count("approved (automatic)") == 1
    assert "1 of 2 sections were approved automatically" in report
    assert "not reviewed by a person" in report
    assert "approved by a named reviewer before inclusion" not in report

    captured = {}

    class FakeHTML:
        def __init__(self, string=None, url_fetcher=None):
            captured["doc"] = string

        def write_pdf(self):
            return b"%PDF"

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    for theme in ("plain", "report"):
        render_pdf(out["html"], "R", out["sections"], None, theme=theme)
        assert captured["doc"].count("approved (automatic)") == 1
        assert "1 of 2 sections were approved automatically" in captured["doc"]


async def test_export_all_human_sections_keep_the_original_sentence(session_factory, world):
    from tret.api.findings import _decide
    from tret.services.export import assemble_deliverable, render_html

    async with session_factory() as db:
        await draft_section(_ctx(db, world), "report", "only", "Body.")
        await db.commit()
        fid = (await db.execute(select(Finding.id))).scalar_one()
        await _decide(db, fid, "approve", None, approver_id=world.user.id,
                      approver_name="U", workspace_id=world.ws.id)
    async with session_factory() as db:
        out = await assemble_deliverable(db, world.project.id, "report")
    report = render_html(out["html"], "R", out["sections"], None, theme="report")
    assert "validated, and approved by a named reviewer before inclusion in this document." in report
    assert "automatic" not in report.split('class="provenance"')[1]
