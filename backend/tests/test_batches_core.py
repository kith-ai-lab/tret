"""`tret/batches/core.py` driven by a stub profile (no disclosure code), plus the
two P0 operational guards: finalize refuses infrastructure-failed targets unless
forced, and create warns about known URLs the research allowlist would refuse.

Same shape as `test_collections_api.py`: a real sqlite file, a fake engine that
completes (or fails) a target's run instantly, the upload faked at
`services/connections.py`, auth overridden.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import collections as collections_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.batches import core
from tret.batches.profiles import register_profile
from tret.db.engine import get_db
from tret.db.models import Base, Harness, Project, Run, User, Workspace
from tret.services import collections as service
from tret.services import connections as connections_service


class StubProfile:
    slug = "stub"
    review_schema = None
    no_harness_error = "stub: no harness"
    missing_harness_message = "stub: launching stopped"

    async def parse_targets(self, body):
        rows = [
            {
                "position": i,
                "slug": name,
                "label": name.title(),
                "task_input": {"name": name},
                "known_urls": urls,
            }
            for i, (name, urls) in enumerate(body.rows, start=1)
        ]
        return rows, ["stub warning"]

    async def validate_settings(self, db, workspace_id, body):
        return {
            "target_slug": body.target_slug,
            "write_root": "runs",
            "concurrency": 2,
            "first_batch_size": 0,
            "auto_write": False,
        }

    async def harness_for(self, db, batch):
        return (
            await db.execute(select(Harness).where(Harness.workspace_id == batch.workspace_id))
        ).scalars().first()

    def task_input(self, batch, target, total):
        return {"message": f"do {target.label}", "_label": f"stub:{target.slug}"}

    def derive_status(self, run_status, outcome, findings, *, run_error=None, auto_write=False, written=False):
        if run_status in ("queued", "running"):
            return "running", None
        if run_status in core.SUCCESS_RUN_STATUSES:
            return "completed", None
        return "failed", run_error or f"run {run_status or 'missing'}"

    async def write_outputs(self, db, batch, target, findings):  # pragma: no cover - never pending
        raise AssertionError("the stub never asks for a write")

    def all_written(self, target, findings):
        return True

    async def after_approve(self, db, finding):  # pragma: no cover - no review step
        raise AssertionError("the stub has no review step")

    def target_extra(self, target, run, findings):
        return {"stub_name": (target.task_input or {}).get("name")}

    def counts_extra(self, targets):
        return {}

    def settings_keys(self):
        return ("target_slug", "concurrency")

    async def records(self, db, batch, *, finalized_at=None):
        return {"batch": batch.run_slug, "at": (finalized_at or core._now()).isoformat()}

    def record_files(self, records):
        return {"manifest.json": (repr(records).encode(), "application/json")}

    def finalize_blockers(self, targets, findings_by_run):
        return []


register_profile(StubProfile())


class FakeEngine:
    def __init__(self, factory):
        self.factory = factory
        self.failures: dict[str, str] = {}  # slug -> run error

    async def execute(self, run_id):
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            slug = run.task_input["_label"].split(":", 1)[1]
            error = self.failures.get(slug)
            run.status = "failed" if error else "completed"
            run.error = error
            await db.commit()


@pytest.fixture
def world(tmp_path, monkeypatch):
    import asyncio

    install_sqlite_type_shims()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'batches.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws = Workspace(id=uuid.uuid4(), name="Kith", kind="team")
    user = User(id=uuid.uuid4(), email="a@example.com", display_name="Rex", role="approver")
    project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
    harness = Harness(id=uuid.uuid4(), workspace_id=ws.id, name="Stub harness", tool_names=[])

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all([ws, user])
            await db.flush()
            db.add_all([project, harness])
            await db.commit()

    asyncio.run(setup())
    fake = FakeEngine(factory)
    uploads: list[dict] = []

    async def fake_upload(db, **kw):
        uploads.append(kw)
        return connections_service.UploadResult(
            item_id="i", web_url=f"https://sp.example/{kw['filename']}",
            name=kw["filename"].rsplit("/", 1)[-1], size=len(kw["data"]),
            target_slug=kw["target_slug"], path=kw["filename"],
        )

    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "get_harness_engine", lambda: fake)
    monkeypatch.setattr(connections_service, "upload_connected_file", fake_upload)
    yield SimpleNamespace(factory=factory, engine=fake, uploads=uploads, ws=ws, user=user)
    asyncio.run(engine.dispose())


@pytest.fixture
def client(world):
    app = FastAPI()
    app.include_router(collections_api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user():
        return world.user

    async def _ws():
        return WorkspaceContext(Workspace(id=world.ws.id, name="Kith", kind="team"), "approver")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as c:
        yield c


def make(client, world, rows=(("alpha", []), ("beta", [])), target_slug="out", run_slug="stub-run-1"):
    body = SimpleNamespace(
        name="Stub", run_slug=run_slug, rows=list(rows), target_slug=target_slug, first_batch_size=0
    )

    async def _go():
        async with world.factory() as db:
            c, _ = await core.create_batch(
                db, workspace_id=world.ws.id, user_id=world.user.id, body=body, profile_slug="stub"
            )
            return str(c.id), list(c.warnings)

    return client.portal.call(_go)


def run_all(client, cid):
    assert client.post(f"/api/collections/{cid}/start").status_code == 200
    assert client.portal.call(service.drain_collection_tasks, 20)
    return client.get(f"/api/collections/{cid}").json()


def test_a_stub_profile_drives_the_orchestrator(client, world):
    cid, warnings = make(client, world)
    assert warnings == ["stub warning"]
    d = client.get(f"/api/collections/{cid}").json()
    assert d["profile"] == "stub" and d["harness_id"] is None and d["parent_run_id"] is None
    assert d["settings"] == {"target_slug": "out", "concurrency": 2}
    assert [t["label"] for t in d["targets"]] == ["Alpha", "Beta"]
    assert d["targets"][0]["task_input"] == {"name": "alpha"}

    d = run_all(client, cid)
    assert d["status"] == "completed"
    assert [t["status"] for t in d["targets"]] == ["completed", "completed"]
    assert [t["stub_name"] for t in d["targets"]] == ["alpha", "beta"]
    assert d["counts"]["by_status"]["completed"] == 2 and "by_market" not in d["counts"]

    async def messages(db):
        runs = (await db.execute(select(Run))).scalars().all()
        return sorted(r.task_input["message"] for r in runs)

    async def _go():
        async with world.factory() as db:
            return await messages(db)

    assert client.portal.call(_go) == ["do Alpha", "do Beta"]

    r = client.post(f"/api/collections/{cid}/finalize")  # bodiless, as before
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "finalized"
    assert [u["filename"] for u in world.uploads] == ["runs/stub-run-1/manifest.json"]


def test_finalize_without_a_write_target_keeps_the_records_as_documents(client, world, monkeypatch, tmp_path):
    from tret.config import get_settings
    from tret.db.models import Document

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    cid, _ = make(client, world, target_slug=None)
    run_all(client, cid)
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 200, r.text
    files = r.json()["finalize"]["files"]
    assert [f["name"] for f in files] == ["manifest.json"] and files[0]["document_id"]
    assert world.uploads == []

    async def docs():
        async with world.factory() as db:
            return (await db.execute(select(Document))).scalars().all()

    [doc] = client.portal.call(docs)
    assert doc.source_kind == "batch_record" and doc.meta["batch_id"] == cid


@pytest.mark.parametrize(
    "text",
    [
        "OSError: [Errno 28] No space left on device",
        "upload failed: ENOSPC",
        "Read-only file system",
        "PermissionError: [Errno 13]",
        "capture store is full",
        "IOError: broken pipe",
        "disk full",
        "storage unavailable while writing the capture",
        "HTTP 507 Insufficient Storage",
    ],
)
def test_infrastructure_errors_are_recognised(text):
    assert core.is_infra_error(text)


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "timeout",
        "provider exploded",
        "run ended without a package map",
        "packaging failed: source bytes changed",
        # The word "storage" alone is not an infrastructure fault: hosts and
        # product names carry it, and a 403 from one is the target's problem.
        "HTTPStatusError: 403 Forbidden for https://storage.googleapis.com/x.pdf",
        "Azure Blob Storage returned 404",
    ],
)
def test_target_errors_are_not_infrastructure(text):
    assert not core.is_infra_error(text)


def test_finalize_refuses_infra_failed_targets_unless_forced(client, world):
    world.engine.failures["beta"] = "OSError: [Errno 28] No space left on device"
    cid, _ = make(client, world)
    d = run_all(client, cid)
    assert [t["status"] for t in d["targets"]] == ["completed", "failed"]
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "1 target(s) failed on an I/O or storage error" in detail
    assert "beta: " in detail and "force=true" in detail
    assert world.uploads == []
    r = client.post(f"/api/collections/{cid}/finalize", json={"force": False})
    assert r.status_code == 409
    r = client.post(f"/api/collections/{cid}/finalize", json={"force": True})
    assert r.status_code == 200 and r.json()["status"] == "finalized"
    assert len(world.uploads) == 1


def test_a_target_that_failed_for_its_own_reasons_does_not_block_finalize(client, world):
    world.engine.failures["beta"] = "provider exploded"
    cid, _ = make(client, world)
    run_all(client, cid)
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 200 and r.json()["status"] == "finalized"


def _research(monkeypatch, mode, hosts):
    from tret.config import get_settings

    monkeypatch.setenv("TRET_EGRESS", "on")
    monkeypatch.setenv("TRET_EGRESS_RESEARCH", mode)
    monkeypatch.setenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS", hosts)
    get_settings.cache_clear()
    return get_settings


def test_create_warns_about_known_url_hosts_outside_the_research_allowlist(client, world, monkeypatch):
    get_settings = _research(monkeypatch, "on", "allowed.example")
    try:
        rows = (
            ("alpha", ["https://www.allowed.example/a.pdf", "https://other.example/x", "https://other.example/y"]),
            ("beta", ["https://blocked.example/z.pdf"]),
        )
        _, warnings = make(client, world, rows=rows)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert warnings == [
        "stub warning",
        "Alpha: known URL host other.example is not in the research allowlist "
        "(TRET_EGRESS_RESEARCH_ALLOW_HOSTS); fetch_url will refuse it",
        "Beta: known URL host blocked.example is not in the research allowlist "
        "(TRET_EGRESS_RESEARCH_ALLOW_HOSTS); fetch_url will refuse it",
    ]


def test_no_allowlist_warning_when_research_is_off_or_the_list_is_empty(client, world, monkeypatch):
    rows = (("alpha", ["https://other.example/x"]),)
    for mode, hosts in (("off", "allowed.example"), ("on", "")):
        get_settings = _research(monkeypatch, mode, hosts)
        try:
            _, warnings = make(client, world, rows=rows, run_slug=f"stub-{mode}-{int(bool(hosts))}")
        finally:
            monkeypatch.undo()
            get_settings.cache_clear()
        assert warnings == ["stub warning"]


def test_a_malformed_known_url_never_breaks_create():
    from tret.config import get_settings

    rows = [{"label": "T", "known_urls": ["https://[acme.com/report.pdf", "https://other.example/x"]}]
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("TRET_EGRESS", "on")
        mp.setenv("TRET_EGRESS_RESEARCH", "on")
        mp.setenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS", "allowed.example")
        get_settings.cache_clear()
        out = core._allowlist_warnings(rows)
    finally:
        mp.undo()
        get_settings.cache_clear()
    assert len(out) == 1 and "other.example" in out[0]


def test_allowlist_warnings_are_capped():
    from tret.config import get_settings

    rows = [{"label": f"T{i}", "known_urls": [f"https://h{i}.example/x"]} for i in range(25)]
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("TRET_EGRESS", "on")
        mp.setenv("TRET_EGRESS_RESEARCH", "on")
        mp.setenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS", "allowed.example")
        get_settings.cache_clear()
        out = core._allowlist_warnings(rows)
    finally:
        mp.undo()
        get_settings.cache_clear()
    assert len(out) == 21 and out[-1] == "... and 5 more"


@pytest.mark.parametrize(
    ("routing", "expected"),
    [
        ({"exploration": {"explored": True}}, True),
        (None, False),
        ({}, False),
        ({"exploration": None}, False),
        ({"exploration": {"explored": False}}, False),
        ("not a dict", False),
        ({"exploration": "not a dict"}, False),
    ],
)
def test_target_out_marks_exploration_runs(routing, expected):
    from datetime import datetime, timezone

    from tret.db.models import BatchTarget

    now = datetime.now(timezone.utc)
    t = BatchTarget(
        id=uuid.uuid4(), collection_id=uuid.uuid4(), position=0, slug="s", label="S", status="completed",
        task_input={}, history=[], written_files=[], finding_ids=[], attempts=1, started_at=now, finished_at=now,
    )
    run = Run(id=uuid.uuid4(), routing=routing, cost_usd=0, iterations=1, model_used="m/x")
    assert core.target_out(t, run, [], profile=SimpleNamespace(target_extra=lambda *a: {}))["explored"] is expected
    assert core.target_out(t, None, [], profile=SimpleNamespace(target_extra=lambda *a: {}))["explored"] is False
