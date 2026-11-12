"""Collection runs end to end through the API (`api/collections.py`,
`services/collections.py`, `POST /findings/decide-batch`).

A real sqlite database (every session the background tasks open reads the same
file), a fake engine whose `execute` completes a target's run instantly with the
findings/outcome a test asks for, and the SharePoint upload faked at
`services/connections.py`. Auth is overridden at `current_user` /
`current_workspace`, so a test switches roles by setting `world.role`.
"""
from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import collections as collections_api
from tret.api import findings as findings_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Collection,
    CollectionTarget,
    Document,
    Finding,
    Harness,
    Project,
    Run,
    User,
    Workspace,
)
from tret.services import collections as service
from tret.services import connections as connections_service
from tret.services.reconcile import ORPHAN_ERROR
from tret.services.workspace import COLLECTION_HARNESS_NAME, COLLECTION_HARNESS_TOOLS

LEAKY_ERROR = (
    '[openrouter] {"error":{"message":"Key limit exceeded (total limit). Manage it using '
    'https://openrouter.ai/workspaces/default/keys/' + "ab" * 32 + ' $15.49 of $15.38 used; '
    'does not reset.","code":402}}'
)
HEADER = "company,market,domicile_country,sector,category,expected_form,fiscal_year,known_urls,notes\n"
REGISTER = HEADER + (
    "Allianz SE,EU,Germany,Insurance,financial,embedded,,https://allianz.example/r.pdf,\n"
    "Shell plc,UK,United Kingdom,Energy,industrial,embedded,,,\n"
    "Apple Inc.,US,,Tech,industrial,standalone,,,\n"
    "Siemens AG,EU,Germany,Industrials,industrial,embedded,,,\n"
    "HSBC Holdings plc,UK,,Banking,financial,embedded,,,\n"
)


# ── the fake engine ───────────────────────────────────────────────────────────
class FakeEngine:
    """`execute` completes the run and applies the behaviour registered for the
    target's slug: "map" (one draft package_map, outcome captured; default),
    "two_maps", "gap" (no map, outcome gap), "fail" (run failed), "fail_with_map"
    (run failed after proposing a map), "hang", "hang_then_complete" (finishes
    with a map as soon as it is cancelled)."""

    def __init__(self, factory):
        self.factory = factory
        self.behaviour: dict[str, str] = {}
        self.executed: list[uuid.UUID] = []
        self.cancelled: list[uuid.UUID] = []

    async def execute(self, run_id):
        import asyncio

        self.executed.append(run_id)
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            target = await db.get(CollectionTarget, uuid.UUID(run.task_input["_collection_target_id"]))
            mode = self.behaviour.get(target.slug, "map")
            if mode == "hang":
                await asyncio.sleep(3600)
            if mode == "hang_then_complete":
                while run_id not in self.cancelled:
                    await asyncio.sleep(0.02)
                mode = "map"
            failing = mode in ("fail", "fail_with_map", "fail_leaky")
            run.status = "failed" if failing else "completed"
            run.error = LEAKY_ERROR if mode == "fail_leaky" else ("provider exploded" if failing else None)
            run.model_used = "test/model"
            run.iterations = 7
            run.cost_usd = Decimal("0.0100")
            run.delegated_cost_usd = Decimal("0.0025")
            if mode in ("map", "two_maps", "fail_with_map"):
                for stem in [target.slug] + ([f"{target.slug}-assurance"] if mode == "two_maps" else []):
                    db.add(_map_finding(run, stem))
                target.outcome = {"status": "captured", "summary": "found it"}
            elif mode == "gap":
                target.outcome = {"status": "gap", "summary": "not published", "blocked_urls": []}
            await db.commit()

    def cancel(self, run_id):
        self.cancelled.append(run_id)
        return True

    def forget_cancelled(self, run_id):
        pass


def _map_finding(run: Run, stem: str) -> Finding:
    return Finding(
        id=uuid.uuid4(),
        run_id=run.id,
        project_id=run.project_id,
        schema_slug="package_map",
        subject={"stem": stem},
        payload={
            "source": {"document_id": str(uuid.uuid4()), "url": f"https://x.example/{stem}.pdf",
                       "sha256": "a" * 64, "byte_size": 10, "page_count": 120, "filename": f"{stem}.pdf"},
            "map": {"title": "Sustainability statement", "role": "primary_disclosure", "scope": "embedded",
                    "keep_ranges": [[3, 40]]},
            "stem": stem,
            "selected_page_count": 38,
            "full_original": False,
            "package": None,
        },
        provenance={"model": "test/model"},
        status="draft",
    )


# ── the world ─────────────────────────────────────────────────────────────────
@pytest.fixture
def world(tmp_path, monkeypatch):
    import asyncio

    install_sqlite_type_shims()
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'collections.db'}")

    # Enforce foreign keys like Postgres does, so a flush that updates a target
    # before its run row exists fails here too (it did on the first live batch).
    from sqlalchemy import event

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_connection, _record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    ws = Workspace(id=uuid.uuid4(), name="Kith", kind="team")
    other_ws = Workspace(id=uuid.uuid4(), name="Other", kind="team")
    user = User(id=uuid.uuid4(), email="a@example.com", display_name="Rex Reviewer", role="approver")
    project = Project(id=uuid.uuid4(), workspace_id=ws.id, name="P")
    harness = Harness(
        id=uuid.uuid4(), workspace_id=ws.id, name=COLLECTION_HARNESS_NAME,
        tool_names=list(COLLECTION_HARNESS_TOOLS),
    )

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all([ws, other_ws, user])
            await db.flush()
            db.add_all([project, harness])
            await db.commit()

    asyncio.run(setup())
    fake = FakeEngine(factory)
    uploads: list[dict] = []
    state = SimpleNamespace(
        factory=factory, engine=fake, uploads=uploads, ws=ws, other_ws=other_ws, user=user,
        project=project, harness=harness, role="approver", upload_error=None, tmp=tmp_path,
        packaging_started=[],
    )

    async def fake_targets(db, workspace_id):
        return [SimpleNamespace(slug="test-output"), SimpleNamespace(slug="disclosures")]

    async def fake_upload(db, **kw):
        if state.upload_error is not None:
            err, state.upload_error = state.upload_error, None
            raise err
        uploads.append(kw)
        return connections_service.UploadResult(
            item_id=f"item-{len(uploads)}", web_url=f"https://sp.example/{kw['filename']}",
            name=kw["filename"].rsplit("/", 1)[-1], size=len(kw["data"]),
            target_slug=kw["target_slug"], path=kw["filename"],
        )

    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(findings_api, "get_session_factory", lambda: factory)
    monkeypatch.setattr(service, "get_harness_engine", lambda: fake)
    monkeypatch.setattr(connections_service, "list_write_targets", fake_targets)
    monkeypatch.setattr(connections_service, "upload_connected_file", fake_upload)
    monkeypatch.setattr(
        findings_api, "_start_packaging",
        lambda finding_id, *, approver_user_id: state.packaging_started.append(finding_id),
    )

    async def noop_outcome(db, finding_id):
        return None

    monkeypatch.setattr(findings_api, "record_outcome_for_finding", noop_outcome)
    yield state
    asyncio.run(engine.dispose())


@pytest.fixture
def client(world):
    app = FastAPI()
    app.include_router(findings_api.router)
    app.include_router(collections_api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user():
        return world.user

    async def _ws():
        workspace = Workspace(id=world.ws.id, name=world.ws.name, kind="team")
        return WorkspaceContext(workspace, world.role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as c:
        yield c


def settle(client):
    assert client.portal.call(service.drain_collection_tasks, 20)


def run_db(client, world, fn):
    """Run `fn(db)` (async) on a fresh session inside the client's loop."""

    async def _go():
        async with world.factory() as db:
            return await fn(db)

    return client.portal.call(_go)


def batch(client, ids, action, note=None):
    """decide-batch, then wait for the background re-settle it spawns."""
    r = client.post("/api/findings/decide-batch", json={"finding_ids": list(ids), "action": action, "note": note})
    settle(client)
    return r


def create(client, **over):
    body = {
        "name": "Batch 1", "run_slug": "2026-11-05-batch1", "register_csv": REGISTER,
        "target_slug": "test-output", "write_root": "runs", "concurrency": 2,
        "first_batch_size": 0, "fiscal_year": 2025, "auto_write": True, "model_override": None,
        **over,
    }
    return client.post("/api/collections", json=body)


def detail(client, cid):
    r = client.get(f"/api/collections/{cid}")
    assert r.status_code == 200, r.text
    return r.json()


def by_slug(d):
    return {t["slug"]: t for t in d["targets"]}


# ── create ────────────────────────────────────────────────────────────────────
def test_create_returns_the_collection_with_targets_and_warnings(client, world):
    r = create(client, first_batch_size=2)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "draft"
    assert d["settings"] == {
        "target_slug": "test-output", "write_root": "runs", "concurrency": 2, "first_batch_size": 2,
        "fiscal_year": 2025, "auto_write": True, "model_override": None, "max_iterations": None, "objective": None,
        "max_cost_usd": None, "row_memory": False,
    }
    assert [t["slug"] for t in d["targets"]] == ["allianz", "shell", "apple", "siemens", "hsbc-holdings"]
    assert [t["status"] for t in d["targets"]] == ["pending", "pending", "held", "held", "held"]
    assert d["targets"][0]["known_urls"] == ["https://allianz.example/r.pdf"]
    assert d["counts"]["total"] == 5
    assert d["counts"]["by_status"]["held"] == 3
    assert d["counts"]["by_market"]["EU"] == {"total": 2, "done": 0, "gap": 0, "failed": 0}
    assert "US: 0 financial institutions (briefing asks for at least 3)" in d["warnings"]
    assert "UK: 2 embedded disclosures (briefing asks for at least 6)" in d["warnings"]
    assert d["pending_maps"] == []
    assert d["cost_usd"] == 0
    listed = client.get("/api/collections").json()["collections"]
    assert [c["id"] for c in listed] == [d["id"]] and "targets" not in listed[0]


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"run_slug": "Bad Slug"}, "run_slug"),
        ({"target_slug": "elsewhere"}, "allowed: test-output, disclosures"),
        ({"register_csv": HEADER + "Toyota,JP,,,,,,,\n"}, "Row 1 (line 2) (Toyota): market 'JP'"),
        ({"register": [{"company": "A", "market": "US"}]}, "exactly one"),
        ({"write_root": "../x"}, "write_root"),
    ],
)
def test_create_validation_is_a_422(client, world, over, fragment):
    r = create(client, **over)
    assert r.status_code == 422
    assert fragment in r.json()["detail"]


def test_negative_first_batch_is_a_422_and_concurrency_is_clamped(client, world):
    assert create(client, first_batch_size=-1).status_code == 422
    assert create(client, concurrency=40).json()["settings"]["concurrency"] == 6


def test_a_json_register_is_accepted(client, world):
    r = create(client, register_csv=None, register=[{"company": "Shell plc", "market": "UK"}])
    assert r.status_code == 200, r.text
    assert r.json()["targets"][0]["slug"] == "shell"


def test_a_duplicate_run_slug_is_a_409(client, world):
    assert create(client).status_code == 200
    r = create(client)
    assert r.status_code == 409
    assert "2026-11-05-batch1" in r.json()["detail"]


def test_analysts_cannot_create_or_start_but_can_read(client, world):
    cid = create(client).json()["id"]
    world.role = "analyst"
    assert create(client, run_slug="another-one").status_code == 403
    assert client.post(f"/api/collections/{cid}/start").status_code == 403
    assert client.get(f"/api/collections/{cid}").status_code == 200
    assert client.get(f"/api/collections/{cid}/records").status_code == 200


def test_another_workspaces_collection_is_a_404(client, world):
    cid = create(client).json()["id"]

    async def move(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.workspace_id = world.other_ws.id
        await db.commit()

    run_db(client, world, move)
    assert client.get(f"/api/collections/{cid}").status_code == 404
    assert client.post(f"/api/collections/{cid}/start").status_code == 404


# ── start, gate, release ──────────────────────────────────────────────────────
def test_start_runs_the_first_batch_and_review_outranks_the_gate(client, world):
    cid = create(client, first_batch_size=3, concurrency=2).json()["id"]
    r = client.post(f"/api/collections/{cid}/start")
    assert r.status_code == 200, r.text
    # Slot-limited: exactly `concurrency` targets launched by the start itself.
    assert [t["status"] for t in r.json()["targets"]].count("running") <= 2
    settle(client)
    d = detail(client, cid)
    targets = by_slug(d)
    # Held targets remain, but decisions are owed first: review outranks gate.
    assert d["status"] == "review"
    assert [targets[s]["status"] for s in ("allianz", "shell", "apple")] == ["review"] * 3
    assert [targets[s]["status"] for s in ("siemens", "hsbc-holdings")] == ["held", "held"]
    assert targets["siemens"]["run_id"] is None
    assert len(world.engine.executed) == 3

    async def runs(db):
        return list((await db.execute(select(Run))).scalars())

    all_runs = run_db(client, world, runs)
    assert len(all_runs) == 3
    for run in all_runs:
        ti = run.task_input
        t = next(t for t in d["targets"] if t["id"] == ti["_collection_target_id"])
        assert ti["_collection_id"] == cid
        assert ti["_label"] == f"collect:{t['slug']}"
        assert t["company"] in ti["message"]
        assert f"Market: {t['market']}" in ti["message"]
        assert "FY2025" in ti["message"]
        assert "report_collection_outcome" in ti["message"]
        assert "_model_override" not in ti
        assert run.harness_id == world.harness.id
        assert run.task_type == "freeform"
        assert run.created_by == world.user.id
    allianz = targets["allianz"]
    assert allianz["attempts"] == 1
    assert allianz["cost_usd"] == pytest.approx(0.0125)
    assert allianz["model_used"] == "test/model" and allianz["iterations"] == 7
    assert allianz["packages"][0]["status"] == "draft"
    assert allianz["packages"][0]["package_status"] is None
    assert allianz["outcome"]["status"] == "captured"
    assert d["cost_usd"] == pytest.approx(0.0375)
    assert {m["target_id"] for m in d["pending_maps"]} == {
        targets[s]["id"] for s in ("allianz", "shell", "apple")
    }
    assert all(m["schema_slug"] == "package_map" for m in d["pending_maps"])

    r = client.post(f"/api/collections/{cid}/release")
    assert r.status_code == 200, r.text
    settle(client)
    d = detail(client, cid)
    assert d["status"] == "review"
    assert all(t["status"] == "review" for t in d["targets"])
    assert len(world.engine.executed) == 5
    assert client.post(f"/api/collections/{cid}/release").status_code == 409


def test_the_model_override_is_stamped_on_every_run(client, world):
    cid = create(client, model_override="anthropic/claude-x").json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)

    async def overrides(db):
        return {r.task_input.get("_model_override") for r in (await db.execute(select(Run))).scalars()}

    assert run_db(client, world, overrides) == {"anthropic/claude-x"}


def test_start_twice_is_a_409_naming_the_status(client, world):
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    r = client.post(f"/api/collections/{cid}/start")
    assert r.status_code == 409
    assert "'review'" in r.json()["detail"]


def test_start_without_the_collector_harness_is_a_409(client, world):
    cid = create(client).json()["id"]

    async def archive(db):
        h = await db.get(Harness, world.harness.id)
        h.is_archived = True
        await db.commit()

    run_db(client, world, archive)
    r = client.post(f"/api/collections/{cid}/start")
    assert r.status_code == 409
    assert COLLECTION_HARNESS_NAME in r.json()["detail"]


def test_pause_stops_new_launches_and_resume_continues(client, world):
    cid = create(client, concurrency=1).json()["id"]
    world.engine.behaviour["allianz"] = "gap"

    # Pause from inside the first run, before it finishes: nothing else launches.
    original = world.engine.execute

    async def pausing_execute(run_id):
        async with world.factory() as db:
            c = await db.get(Collection, uuid.UUID(cid))
            c.status = "paused"
            await db.commit()
        await original(run_id)

    world.engine.execute = pausing_execute
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    d = detail(client, cid)
    assert d["status"] == "paused"
    assert by_slug(d)["allianz"]["status"] == "gap"
    assert [t["status"] for t in d["targets"][1:]] == ["pending"] * 4
    world.engine.execute = original
    assert client.post(f"/api/collections/{cid}/pause").status_code == 409
    r = client.post(f"/api/collections/{cid}/resume")
    assert r.status_code == 200, r.text
    settle(client)
    d = detail(client, cid)
    assert d["status"] == "review"
    assert len(world.engine.executed) == 5


# ── run outcomes ──────────────────────────────────────────────────────────────
def test_failed_and_gap_runs_and_retry(client, world):
    world.engine.behaviour.update({"allianz": "fail", "shell": "gap", "apple": "gap", "siemens": "gap",
                                   "hsbc-holdings": "gap"})
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    d = detail(client, cid)
    targets = by_slug(d)
    assert targets["allianz"]["status"] == "failed"
    assert targets["allianz"]["error"] == "provider exploded"
    assert targets["shell"]["status"] == "gap" and targets["shell"]["error"] is None
    assert d["status"] == "completed"
    assert d["counts"]["by_market"]["UK"]["gap"] == 2
    assert d["counts"]["by_market"]["EU"]["failed"] == 1
    first_run = targets["allianz"]["run_id"]

    world.engine.behaviour["allianz"] = "map"
    r = client.post(f"/api/collections/{cid}/targets/{targets['allianz']['id']}/retry")
    assert r.status_code == 200, r.text
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "review"
    assert t["attempts"] == 2
    assert t["run_id"] != first_run
    assert t["error"] is None
    assert t["outcome"]["status"] == "captured"  # the new run's, not carried over
    (past,) = t["history"]
    assert past["attempt"] == 1 and past["run_id"] == first_run
    assert past["status"] == "failed" and past["error"] == "provider exploded"
    assert past["cost_usd"] == pytest.approx(0.0125)
    assert past["model_used"] == "test/model"
    assert t["cost_usd"] == pytest.approx(0.025)  # both attempts
    assert detail(client, cid)["status"] == "review"
    company = next(
        c for c in client.get(f"/api/collections/{cid}/records").json()["manifest"]["companies"]
        if c["id"] == "allianz"
    )
    assert company["attempts"] == 2 and len(company["history"]) == 1
    # Only failed/gap/rejected targets retry.
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry")
    assert r.status_code == 409 and "'review'" in r.json()["detail"]


def test_a_gap_retry_clears_the_old_outcome(client, world):
    world.engine.behaviour.update({s: "gap" for s in ("allianz", "shell", "apple", "siemens", "hsbc-holdings")})
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    world.engine.behaviour["allianz"] = "fail"
    client.post(f"/api/collections/{cid}/targets/{t['id']}/retry")
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "failed" and t["outcome"] is None
    assert t["history"][0]["outcome"]["status"] == "gap"


def test_a_failed_run_that_proposed_maps_is_reviewable_with_a_warning(client, world):
    world.engine.behaviour.update({"allianz": "fail_with_map", "shell": "gap", "apple": "gap",
                                   "siemens": "gap", "hsbc-holdings": "gap"})
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "review"
    assert t["error"] == "run ended failed: provider exploded"
    batch(client, [t["packages"][0]["finding_id"]], "approve")
    assert by_slug(detail(client, cid))["allianz"]["status"] == "packaging"


def test_write_failed_targets_can_also_be_retried_with_a_new_run(client, world):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    world.upload_error = connections_service.ConnectionWriteError("Graph 503")
    _package(client, world, fid)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "write_failed"
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry")
    assert r.status_code == 200, r.text
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "review" and t["attempts"] == 2
    assert t["history"][0]["status"] == "write_failed"


def test_a_crashing_engine_fails_the_target(client, world):
    async def boom(run_id):
        raise RuntimeError("engine fell over")

    world.engine.execute = boom
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "failed"
    assert t["error"] == "RuntimeError: engine fell over"


def test_a_run_past_the_timeout_is_cancelled_and_failed(client, world, monkeypatch):
    monkeypatch.setenv("TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS", "1")
    from tret.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(service, "CANCEL_GRACE_SECONDS", 0.1)
    world.engine.behaviour.update({s: "hang" for s in ("allianz",)})
    world.engine.behaviour.update({s: "gap" for s in ("shell", "apple", "siemens", "hsbc-holdings")})
    try:
        cid = create(client).json()["id"]
        client.post(f"/api/collections/{cid}/start")
        settle(client)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "failed" and t["error"] == "timeout"
    assert uuid.UUID(t["run_id"]) in world.engine.cancelled

    async def run_status(db):
        return (await db.get(Run, uuid.UUID(t["run_id"]))).status

    assert run_db(client, world, run_status) == "cancelled"


def test_a_run_that_finishes_during_the_cancel_grace_settles_normally(client, world, monkeypatch):
    monkeypatch.setenv("TRET_COLLECTIONS_RUN_TIMEOUT_SECONDS", "1")
    from tret.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(service, "CANCEL_GRACE_SECONDS", 5)
    world.engine.behaviour["allianz"] = "hang_then_complete"
    world.engine.behaviour.update({s: "gap" for s in ("shell", "apple", "siemens", "hsbc-holdings")})
    try:
        cid = create(client).json()["id"]
        client.post(f"/api/collections/{cid}/start")
        settle(client)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    t = by_slug(detail(client, cid))["allianz"]
    assert uuid.UUID(t["run_id"]) in world.engine.cancelled
    assert t["status"] == "review" and t["error"] is None


# ── approval, packaging, write ────────────────────────────────────────────────
def _started(client, world, **over):
    world.engine.behaviour.update({"apple": "gap", "siemens": "gap", "hsbc-holdings": "gap"})
    cid = create(client, **over).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    return cid


def test_batch_decide_moves_targets_to_packaging_and_rejected(client, world):
    cid = _started(client, world)
    d = detail(client, cid)
    targets = by_slug(d)
    allianz_map = targets["allianz"]["packages"][0]["finding_id"]
    shell_map = targets["shell"]["packages"][0]["finding_id"]
    r = client.post(
        "/api/findings/decide-batch",
        json={"finding_ids": [allianz_map, str(uuid.uuid4())], "action": "approve", "note": "ok"},
    )
    settle(client)
    assert r.status_code == 200, r.text
    results = r.json()["results"]
    assert results[0] == {"id": allianz_map, "ok": True, "status": "approved"}
    assert results[1]["ok"] is False and results[1]["error"] == "Finding not found"
    assert world.packaging_started == [uuid.UUID(allianz_map)]
    r = batch(client, [shell_map, allianz_map], "reject")
    results = r.json()["results"]
    assert results[0] == {"id": shell_map, "ok": True, "status": "rejected"}
    assert results[1] == {"id": allianz_map, "ok": False, "error": "Finding is already 'approved'"}
    d = detail(client, cid)
    targets = by_slug(d)
    assert targets["allianz"]["status"] == "packaging"
    assert targets["allianz"]["packages"][0]["package_status"] == "running"
    assert targets["shell"]["status"] == "rejected"
    assert d["status"] == "review"
    assert d["pending_maps"] == []


def test_decide_batch_notifies_collections_only_of_decided_findings(client, world, monkeypatch):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    seen = []

    async def record(finding_ids, **kw):
        seen.append(list(finding_ids))

    monkeypatch.setattr(service, "notify_decided", record)
    missing = uuid.uuid4()
    client.post(
        "/api/findings/decide-batch", json={"finding_ids": [fid, str(missing)], "action": "approve"}
    )
    assert seen == [[uuid.UUID(fid)]]


def test_decide_batch_requires_the_approver_role_and_a_valid_action(client, world):
    world.role = "analyst"
    assert client.post("/api/findings/decide-batch", json={"finding_ids": [str(uuid.uuid4())], "action": "approve"}).status_code == 403
    world.role = "approver"
    r = client.post("/api/findings/decide-batch", json={"finding_ids": [str(uuid.uuid4())], "action": "maybe"})
    assert r.status_code == 422


def test_the_single_approval_route_updates_the_target_too(client, world):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    r = client.post(f"/api/findings/{fid}/approval", json={"action": "reject"})
    settle(client)
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"
    assert by_slug(detail(client, cid))["allianz"]["status"] == "rejected"


def _package(client, world, finding_id: str, *, files=("allianz.pdf", "allianz.package.json"), notify=True):
    """Write package files + Document rows and record a `packaged` result on the
    finding, as `_package_approved_map` does."""

    async def go(db):
        f = await db.get(Finding, uuid.UUID(finding_id))
        docs = []
        for name in files:
            data = f"bytes of {name} for {finding_id}".encode()
            path = Path(world.tmp) / f"{finding_id}-{name}"
            path.write_bytes(data)
            doc = Document(
                id=uuid.uuid4(), project_id=f.project_id, filename=name, content_type="application/pdf",
                byte_size=len(data), storage_path=str(path), sha256=hashlib.sha256(data).hexdigest(),
                source_kind="package", meta={},
            )
            db.add(doc)
            docs.append(
                {"role": "retained_pdf" if name.endswith(".pdf") else "package_record",
                 "document_id": str(doc.id), "filename": name, "sha256": doc.sha256,
                 "byte_size": doc.byte_size, "content_type": "application/pdf"}
            )
        f.payload = {
            **f.payload,
            "package": {"status": "packaged", "documents": docs, "retained_sha256": docs[0]["sha256"],
                        "retained_page_count": 38, "source_page_count": 120, "wall_seconds": 6.1,
                        "limitations": ["no OCR"], "error": None},
        }
        await db.commit()
        return docs

    docs = run_db(client, world, go)
    if notify:
        client.portal.call(service.notify_package_finished, uuid.UUID(finding_id))
    return docs


def test_packaging_finished_uploads_with_auto_write(client, world):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    docs = _package(client, world, fid)
    assert [u["filename"] for u in world.uploads] == [
        "runs/2026-11-05-batch1/packages/allianz/allianz.pdf",
        "runs/2026-11-05-batch1/packages/allianz/allianz.package.json",
    ]
    first = world.uploads[0]
    # The collection owns its packages folder: a re-upload replaces in place.
    assert first["conflict"] == "replace"
    assert first["max_bytes"] == 62_914_560
    assert first["target_slug"] == "test-output"
    assert first["data"] == f"bytes of allianz.pdf for {fid}".encode()
    assert first["actor_user_id"] == world.user.id  # the map's approver
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "written"
    assert t["finished_at"] is not None
    assert [w["filename"] for w in t["written_files"]] == [
        "packages/allianz/allianz.pdf", "packages/allianz/allianz.package.json"
    ]
    assert t["written_files"][0]["sha256"] == docs[0]["sha256"]
    assert t["written_files"][0]["web_url"].startswith("https://sp.example/")
    assert t["packages"][0]["package_status"] == "packaged"
    assert t["packages"][0]["retained_page_count"] == 38


def test_packaging_without_auto_write_completes_without_uploading(client, world):
    cid = _started(client, world, auto_write=False)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    _package(client, world, fid)
    assert world.uploads == []
    assert by_slug(detail(client, cid))["allianz"]["status"] == "completed"


def test_an_upload_failure_is_write_failed_and_retry_write_finishes(client, world):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    world.upload_error = connections_service.ConnectionWriteError("Microsoft Graph returned 503")
    _package(client, world, fid)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "write_failed"
    assert "Microsoft Graph returned 503" in t["error"]
    assert "packages/allianz/allianz.pdf" in t["error"]
    assert t["written_files"] == []

    world.role = "analyst"
    assert client.post(f"/api/collections/{cid}/targets/{t['id']}/retry-write").status_code == 403
    world.role = "approver"
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry-write")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "written"
    assert len(r.json()["written_files"]) == 2
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry-write")
    assert r.status_code == 409


def test_a_partial_upload_keeps_what_was_written_and_retry_skips_it(client, world):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")

    calls = {"n": 0}
    original = connections_service.upload_connected_file

    async def second_fails(db, **kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise connections_service.UploadConflictError("'x' already exists")
        return await original(db, **kw)

    connections_service.upload_connected_file = second_fails
    try:
        _package(client, world, fid)
    finally:
        connections_service.upload_connected_file = original
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "write_failed"
    assert [w["filename"] for w in t["written_files"]] == ["packages/allianz/allianz.pdf"]
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry-write")
    assert r.json()["status"] == "written"
    assert [u["filename"].rsplit("/", 1)[-1] for u in world.uploads] == ["allianz.pdf", "allianz.package.json"]


def test_uploads_are_attributed_to_the_approver_and_written_files_dedupe_by_name(client, world):
    cid = _started(client, world)
    t = by_slug(detail(client, cid))["allianz"]
    fid = t["packages"][0]["finding_id"]

    async def orphan_owner(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.created_by = None  # the creator's account was deleted
        await db.commit()

    run_db(client, world, orphan_owner)
    batch(client, [fid], "approve")
    _package(client, world, fid, files=("allianz.pdf",))
    assert world.uploads[0]["actor_user_id"] == world.user.id  # approver, not created_by
    d = detail(client, cid)
    assert d["created_by"] is None

    async def duplicate(db):
        target = await db.get(CollectionTarget, uuid.UUID(t["id"]))
        first = target.written_files[0]
        target.written_files = [first, {**first, "sha256": "b" * 64, "document_id": "newer"}]
        await db.commit()

    run_db(client, world, duplicate)
    t = by_slug(detail(client, cid))["allianz"]
    assert [w["sha256"] for w in t["written_files"]] == ["b" * 64]
    sums = client.get(f"/api/collections/{cid}/records").json()["sha256sums"]
    assert f"{'b' * 64}  packages/allianz/allianz.pdf" in sums
    assert f"{'a' * 64}  packages/allianz/allianz.pdf" not in sums


def test_concurrent_settles_upload_each_file_once(client, world):
    import asyncio

    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    original = connections_service.upload_connected_file

    async def slow(db, **kw):
        await asyncio.sleep(0.05)
        return await original(db, **kw)

    connections_service.upload_connected_file = slow
    try:
        _package(client, world, fid, notify=False)

        async def both():
            await asyncio.gather(
                service.notify_package_finished(uuid.UUID(fid)),
                service.notify_package_finished(uuid.UUID(fid)),
            )

        client.portal.call(both)
    finally:
        connections_service.upload_connected_file = original
    assert [u["filename"].rsplit("/", 1)[-1] for u in world.uploads] == ["allianz.pdf", "allianz.package.json"]
    assert by_slug(detail(client, cid))["allianz"]["status"] == "written"


def test_a_target_stuck_in_packaging_recovers_on_the_next_settle(client, world):
    cid = _started(client, world)
    t = by_slug(detail(client, cid))["allianz"]
    fid = t["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    _package(client, world, fid, notify=False)  # the process died before the callback
    assert by_slug(detail(client, cid))["allianz"]["status"] == "packaging"
    assert world.uploads == []

    async def sweep(db):
        return await service.sweep_orphaned_collections(db)

    assert run_db(client, world, sweep) >= 1
    assert by_slug(detail(client, cid))["allianz"]["status"] == "written"
    assert len(world.uploads) == 2


# ── records and finalize ──────────────────────────────────────────────────────
def test_records_preview_and_finalize(client, world):
    cid = _started(client, world)
    targets = by_slug(detail(client, cid))
    allianz_fid = targets["allianz"]["packages"][0]["finding_id"]
    shell_fid = targets["shell"]["packages"][0]["finding_id"]
    batch(client, [allianz_fid], "approve", "pp 3-40")
    batch(client, [shell_fid], "reject")
    _package(client, world, allianz_fid)
    d = detail(client, cid)
    assert d["status"] == "completed"

    records = client.get(f"/api/collections/{cid}/records").json()
    manifest = records["manifest"]
    assert manifest["schema_version"] == "1.0"
    assert manifest["collection_run"] == "2026-11-05-batch1"
    assert len(manifest["companies"]) == 5
    assert manifest["outcome"]["targets_total"] == 5
    assert manifest["outcome"]["written"] == 1
    assert manifest["outcome"]["rejected"] == 1
    assert manifest["outcome"]["gap"] == 3
    assert manifest["outcome"]["retained_pdf_documents"] == 1
    assert manifest["outcome"]["retained_pdf_pages"] == 38
    assert manifest["completeness_definition"].startswith("captured core disclosure")
    assert manifest["sample"]["EU"] == 2 and manifest["sample"]["UK"] == 2 and manifest["sample"]["US"] == 1
    allianz = next(c for c in manifest["companies"] if c["id"] == "allianz")
    doc = allianz["documents"][0]
    assert doc["review"]["status"] == "approved"
    assert doc["review"]["approver"] == "Rex Reviewer"
    assert doc["review"]["note"] == "pp 3-40"
    assert doc["retained"]["page_count"] == 38
    assert doc["retained"]["files"][0]["web_url"].startswith("https://sp.example/")
    assert doc["keep_ranges"] == [[3, 40]]
    assert doc["limitations"] == ["no OCR"]
    lines = records["sha256sums"].splitlines()
    names = [line.split("  ")[1] for line in lines]
    assert sorted(names) == sorted(
        ["packages/allianz/allianz.pdf", "packages/allianz/allianz.package.json"]
        + [n for n in service.RECORD_FILES if n != "SHA256SUMS.txt"]
    )
    assert allianz["attempts"] == 1 and allianz["history"] == []
    assert records["execution"]["totals"]["cost_usd"] == pytest.approx(0.0625)
    assert records["execution"]["packaging_version"]
    assert len(records["execution"]["runs"]) == 5
    slugs = {e["slug"] for e in records["exception_log"]["targets"]}
    assert slugs == {"shell", "apple", "siemens", "hsbc-holdings"}
    assert len(records["target_register"]["targets"]) == 5
    assert "| Allianz SE | written | 38 |" in records["results_summary"]

    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 200, r.text
    d = detail(client, cid)
    assert d["status"] == "finalized"
    assert d["finalized_at"] is not None
    record_uploads = [u for u in world.uploads if "/packages/" not in u["filename"]]
    assert [u["filename"] for u in record_uploads] == [
        f"runs/2026-11-05-batch1/{name}" for name in service.RECORD_FILES
    ]
    assert record_uploads[-1]["filename"].endswith("SHA256SUMS.txt")  # written last
    assert all(u["conflict"] == "replace" for u in record_uploads)
    assert [f["name"] for f in d["finalize"]["files"]] == list(service.RECORD_FILES)
    assert d["finalize"]["error"] is None
    # Every other uploaded record file is listed in SHA256SUMS with its real hash.
    sums_text = record_uploads[-1]["data"].decode()
    for u in record_uploads[:-1]:
        name = u["filename"].rsplit("/", 1)[-1]
        assert f"{hashlib.sha256(u['data']).hexdigest()}  {name}" in sums_text
    # Re-finalize (after a retry, say) is allowed and replaces the same six paths.
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 200
    again = [u for u in world.uploads if "/packages/" not in u["filename"]][6:]
    assert [u["filename"] for u in again] == [u["filename"] for u in record_uploads]


def test_a_finalize_upload_failure_is_recorded_and_retryable(client, world):
    cid = _started(client, world, auto_write=False)
    targets = by_slug(detail(client, cid))
    batch(client, [targets[s]["packages"][0]["finding_id"] for s in ("allianz", "shell")], "reject")
    world.upload_error = connections_service.ConnectionWriteError("Graph 403")
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 200
    d = r.json()
    assert d["status"] == "completed"
    assert d["finalize"]["error"] == "manifest.json: Graph 403"
    assert d["finalize"]["files"] == []
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.json()["status"] == "finalized"


def test_finalize_refuses_a_running_or_draft_collection(client, world):
    cid = create(client).json()["id"]
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 409 and "'draft'" in r.json()["detail"]


def test_finalize_refuses_while_maps_await_a_decision_or_packaging_runs(client, world):
    cid = _started(client, world)
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 409
    assert r.json()["detail"] == (
        "2 package maps are still awaiting a decision; approve or reject them first"
    )
    targets = by_slug(detail(client, cid))
    batch(client, [targets["allianz"]["packages"][0]["finding_id"]], "approve")
    batch(client, [targets["shell"]["packages"][0]["finding_id"]], "reject")
    r = client.post(f"/api/collections/{cid}/finalize")
    assert r.status_code == 409 and "packaging" in r.json()["detail"]
    assert world.uploads == []


def test_packages_ready_after_finalize_wait_for_retry_write(client, world):
    cid = _started(client, world)
    targets = by_slug(detail(client, cid))
    fid = targets["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    batch(client, [targets["shell"]["packages"][0]["finding_id"]], "reject")

    async def finalize_now(db):
        # Simulate: the operator finalized while allianz was still packaging.
        c = await db.get(Collection, uuid.UUID(cid))
        c.status = "finalized"
        await db.commit()

    run_db(client, world, finalize_now)
    _package(client, world, fid)
    t = by_slug(detail(client, cid))["allianz"]
    assert world.uploads == []
    assert t["status"] == "completed"
    assert t["error"] == service.FINALIZED_BEFORE_WRITE
    r = client.post(f"/api/collections/{cid}/targets/{t['id']}/retry-write")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "written" and r.json()["error"] is None
    assert len(world.uploads) == 2
    assert client.post(f"/api/collections/{cid}/finalize").status_code == 200


# ── boot sweep ────────────────────────────────────────────────────────────────
def test_the_boot_sweep_fails_orphaned_targets_and_pauses_unfinished_collections(client, world):
    cid = create(client).json()["id"]

    async def orphan(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.status = "running"
        targets = list((await db.execute(select(CollectionTarget).order_by(CollectionTarget.position))).scalars())
        run = Run(id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
                  task_type="freeform", task_input={}, document_ids=[], status="failed",
                  error=ORPHAN_ERROR)
        db.add(run)
        await db.flush()  # the run row must exist before the target points at it
        targets[0].status = "running"
        targets[0].run_id = run.id
        await db.commit()
        swept = await service.sweep_orphaned_collections(db)
        return swept

    assert run_db(client, world, orphan) == 1
    d = detail(client, cid)
    assert d["targets"][0]["status"] == "failed"
    assert d["targets"][0]["error"] == ORPHAN_ERROR
    assert d["status"] == "paused"
    assert "restarted" in d["last_error"]


def test_no_target_launches_while_draining(client, world):
    from fastapi import FastAPI as _App

    cid = create(client).json()["id"]
    app = _App()
    app.state.draining = True
    service.bind_app(app)
    try:
        r = client.post(f"/api/collections/{cid}/start")
        assert r.status_code == 200
        settle(client)
        assert world.engine.executed == []
        assert all(t["status"] == "pending" for t in detail(client, cid)["targets"])
    finally:
        service.bind_app(None)


def test_the_shutdown_drain_sees_tasks_added_after_it_started():
    from tret.main import _LiveTasks

    registry: set = set()
    live = _LiveTasks(registry, set())
    assert list(live) == []
    registry.add("task")
    assert list(live) == ["task"]


def test_release_needs_held_targets_and_is_refused_while_running(client, world):
    cid = create(client, first_batch_size=0).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    r = client.post(f"/api/collections/{cid}/release")
    assert r.status_code == 409 and r.json()["detail"] == "No held targets"

    async def force_running(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.status = "running"
        await db.commit()

    run_db(client, world, force_running)
    r = client.post(f"/api/collections/{cid}/release")
    assert r.status_code == 409 and "'running'" in r.json()["detail"]


def test_the_gate_holds_when_the_first_batch_needs_no_decisions(client, world):
    world.engine.behaviour.update({"allianz": "gap", "shell": "gap"})
    cid = create(client, first_batch_size=2).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    assert detail(client, cid)["status"] == "gate"


def test_main_wires_the_router_sweep_and_drain():
    import inspect

    from tret import main

    source = inspect.getsource(main)
    assert "sweep_orphaned_collections" in source
    assert "collections_service._collection_tasks" in source
    assert "batches_api.router" in source  # serves /api/batches and /api/collections


def test_max_iterations_is_stored_and_stamped_on_every_run(client, world):
    r = create(client, max_iterations=40)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.get(f"/api/collections/{cid}").json()["settings"]["max_iterations"] == 40
    client.post(f"/api/collections/{cid}/start")
    settle(client)

    async def caps(db):
        return {r.task_input.get("_max_iterations") for r in (await db.execute(select(Run))).scalars()}

    assert run_db(client, world, caps) == {40}


def test_max_iterations_out_of_range_is_a_422(client, world):
    assert create(client, max_iterations=0).status_code == 422
    assert create(client, max_iterations=51).status_code == 422


def test_max_cost_usd_is_stored_echoed_and_stamped_only_when_set(client, world):
    from tret.engine.delegation import COST_CAP_KEY

    async def caps(db):
        return {r.task_input.get(COST_CAP_KEY) for r in (await db.execute(select(Run))).scalars()}

    r = create(client, max_cost_usd=0.25)
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    assert client.get(f"/api/collections/{cid}").json()["settings"]["max_cost_usd"] == 0.25
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    assert run_db(client, world, caps) == {0.25}


def test_max_cost_usd_is_absent_from_the_task_input_when_unset(client, world):
    from tret.engine.delegation import COST_CAP_KEY

    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)

    async def keys(db):
        return {k for r in (await db.execute(select(Run))).scalars() for k in r.task_input}

    ks = run_db(client, world, keys)
    assert COST_CAP_KEY not in ks and "_row_memory" not in ks


def test_max_cost_usd_bounds(client, world):
    assert create(client, max_cost_usd=0).status_code == 422
    assert create(client, max_cost_usd=-1).status_code == 422
    assert create(client, max_cost_usd=1000.01).status_code == 422
    assert create(client, max_cost_usd=1000, run_slug="2026-11-05-batch2").status_code == 200


def test_row_memory_adds_a_block_to_a_later_batch_of_the_family_only_when_on(client, world):
    world.engine.behaviour.update({"allianz": "gap", "shell": "gap", "apple": "gap", "siemens": "gap",
                                   "hsbc-holdings": "gap"})
    first = create(client).json()["id"]
    client.post(f"/api/collections/{first}/start")
    settle(client)

    async def messages(db):
        return {
            str(r.task_input.get("_collection_target_id")): (r.task_input.get("message"), r.task_input.get("_row_memory"))
            for r in (await db.execute(select(Run))).scalars()
        }

    base = run_db(client, world, messages)
    off = create(client, run_slug="2026-11-05-off").json()["id"]
    client.post(f"/api/collections/{off}/start")
    settle(client)
    after_off = run_db(client, world, messages)
    new_off = {k: v for k, v in after_off.items() if k not in base}
    assert new_off and all(v[1] is None and "EARLIER ATTEMPTS" not in v[0] for v in new_off.values())
    on = create(client, run_slug="2026-11-05-on", row_memory=True).json()
    assert on["settings"]["row_memory"] is True
    client.post(f"/api/collections/{on['id']}/start")
    settle(client)
    after_on = run_db(client, world, messages)
    new_on = {k: v for k, v in after_on.items() if k not in after_off}
    assert new_on and all(v[1] is True and "EARLIER ATTEMPTS AT THIS TARGET" in v[0] for v in new_on.values())
    assert all('reported a gap: "not published"' in v[0] for v in new_on.values())
    # the block is appended after the ordinary task message, never inside it
    assert all(v[0].index("EARLIER ATTEMPTS") > 100 for v in new_on.values())


def test_a_memory_failure_does_not_block_the_launch(client, world, monkeypatch):
    from tret.batches import memory

    async def boom(*a, **k):
        raise RuntimeError("memory exploded")

    monkeypatch.setattr(memory, "earlier_attempts", boom)
    cid = create(client, row_memory=True).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    d = detail(client, cid)
    assert d["status"] != "paused" and d["counts"]["total"] == 5

    async def flags(db):
        return {r.task_input.get("_row_memory") for r in (await db.execute(select(Run))).scalars()}

    assert run_db(client, world, flags) == {None}


def test_a_provider_error_body_is_sanitised_on_the_target(client, world):
    world.engine.behaviour.update({"allianz": "fail_leaky", "shell": "gap", "apple": "gap",
                                   "siemens": "gap", "hsbc-holdings": "gap"})
    cid = create(client).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    err = by_slug(detail(client, cid))["allianz"]["error"]
    assert err == ("[openrouter] Key limit exceeded (total limit). Manage it using "
                   "$15.49 of $15.38 used; does not reset.")
    assert "openrouter.ai" not in err and "{" not in err


def test_summarise_run_error():
    f = service.summarise_run_error
    assert f(None) is None
    assert f("max_iterations (24) reached without completion") == "max_iterations (24) reached without completion"
    assert f('boom {"error":{"message":"bad key"}} end') == "boom bad key end"
    assert f('boom {"message":"top level"}') == "boom top level"
    assert f('boom {"code": 402}') == "boom"
    assert f("boom {not json}") == "boom"
    assert f("see https://x.example/a?b=1 for more\n details") == "see for more details"
    assert len(f("x" * 1000)) == 300


# ── written file viewer ───────────────────────────────────────────────────────
def _written(client, world, files=("allianz.pdf", "allianz.layout.md", "allianz.package.json")):
    cid = _started(client, world)
    fid = by_slug(detail(client, cid))["allianz"]["packages"][0]["finding_id"]
    batch(client, [fid], "approve")
    _package(client, world, fid, files=files)
    t = by_slug(detail(client, cid))["allianz"]
    assert t["status"] == "written"
    return cid, t["id"], fid


def test_a_written_file_is_served_inline_with_a_fixed_content_type(client, world):
    cid, tid, fid = _written(client, world)
    url = f"/api/collections/{cid}/targets/{tid}/files"
    r = client.get(url, params={"path": "packages/allianz/allianz.layout.md"})
    assert r.status_code == 200
    assert r.content == f"bytes of allianz.layout.md for {fid}".encode()
    assert r.headers["content-type"] == "text/markdown; charset=utf-8"
    assert r.headers["content-disposition"].startswith('inline; filename="allianz.layout.md"')
    assert "filename*=UTF-8''allianz.layout.md" in r.headers["content-disposition"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "private, no-store"
    pdf = client.get(url, params={"path": "packages/allianz/allianz.pdf"})
    assert pdf.headers["content-type"] == "application/pdf"
    js = client.get(url, params={"path": "packages/allianz/allianz.package.json"})
    assert js.headers["content-type"] == "application/json"
    # The batches route and the deprecated alias are the same handler.
    assert client.get(url.replace("/collections/", "/batches/"), params={"path": "packages/allianz/allianz.pdf"}).status_code == 200


def test_a_written_file_download_is_an_attachment(client, world):
    cid, tid, _ = _written(client, world)
    r = client.get(
        f"/api/collections/{cid}/targets/{tid}/files",
        params={"path": "packages/allianz/allianz.pdf", "download": "true"},
    )
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith('attachment; filename="allianz.pdf"')


@pytest.mark.parametrize(
    "path",
    ["packages/allianz/../allianz/allianz.pdf", "../../etc/passwd", "packages/allianz/other.pdf",
     "allianz.pdf", "/packages/allianz/allianz.pdf", ""],
)
def test_a_path_outside_written_files_is_a_404(client, world, path):
    cid, tid, _ = _written(client, world)
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": path})
    assert r.status_code == 404


def test_a_written_file_in_another_workspace_is_a_404(client, world):
    cid, tid, _ = _written(client, world)

    async def move(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.workspace_id = world.other_ws.id
        await db.commit()

    run_db(client, world, move)
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/allianz/allianz.pdf"})
    assert r.status_code == 404


def test_a_missing_stored_file_is_a_404(client, world):
    cid, tid, fid = _written(client, world)
    (Path(world.tmp) / f"{fid}-allianz.pdf").unlink()
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/allianz/allianz.pdf"})
    assert r.status_code == 404
    assert "no longer in storage" in r.json()["detail"]


def test_big_text_files_are_413_inline_but_pdfs_and_downloads_are_not(client, world, monkeypatch):
    from tret.api import batches as batches_api

    cid, tid, _ = _written(client, world)
    monkeypatch.setattr(batches_api, "INLINE_TEXT_MAX_BYTES", 10)
    url = f"/api/collections/{cid}/targets/{tid}/files"
    r = client.get(url, params={"path": "packages/allianz/allianz.layout.md"})
    assert r.status_code == 413
    assert "allianz.layout.md" in r.json()["detail"] and "download it instead" in r.json()["detail"]
    assert client.get(url, params={"path": "packages/allianz/allianz.pdf"}).status_code == 200
    assert client.get(url, params={"path": "packages/allianz/allianz.layout.md", "download": "true"}).status_code == 200


def _seed_written(client, world, cid, tid, entries, docs=()):
    """Create Documents (project_id, filename, bytes) and set the target's written_files."""

    async def go(db):
        c = await db.get(Collection, uuid.UUID(cid))
        made = []
        for project_id, name, data in docs:
            path = Path(world.tmp) / f"seed-{uuid.uuid4()}-{name}"
            path.write_bytes(data)
            d = Document(
                id=uuid.uuid4(), project_id=project_id or c.project_id, filename=name,
                content_type="text/html", byte_size=len(data), storage_path=str(path),
                sha256=hashlib.sha256(data).hexdigest(), source_kind="package", meta={},
            )
            db.add(d)
            made.append(str(d.id))
        t = await db.get(CollectionTarget, uuid.UUID(tid))
        t.written_files = [{**e, "document_id": e["document_id"].format(*made)} for e in entries]
        await db.commit()

    run_db(client, world, go)


def _entry(filename, doc="{0}"):
    return {"filename": filename, "web_url": "https://sp.example/x", "sha256": "0" * 64,
            "byte_size": 3, "uploaded_at": "2026-11-06T00:00:00Z", "document_id": doc}


def test_a_document_in_another_project_is_a_404(client, world):
    cid, tid, _ = _written(client, world)
    elsewhere = Project(id=uuid.uuid4(), workspace_id=world.ws.id, name="Elsewhere")

    async def add_project(db):
        db.add(elsewhere)
        await db.commit()

    run_db(client, world, add_project)
    _seed_written(client, world, cid, tid, [_entry("packages/x/a.md")], docs=[(elsewhere.id, "a.md", b"abc")])
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/x/a.md"})
    assert r.status_code == 404


def test_a_malformed_document_id_is_a_404(client, world):
    cid, tid, _ = _written(client, world)
    _seed_written(client, world, cid, tid, [_entry("packages/x/a.md", "not-a-uuid"), _entry("packages/x/b.md", "None")])
    for p in ("packages/x/a.md", "packages/x/b.md"):
        assert client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": p}).status_code == 404


def test_a_reuploaded_filename_serves_the_newest_entry(client, world):
    cid, tid, _ = _written(client, world)
    _seed_written(
        client, world, cid, tid,
        [_entry("packages/x/a.md", "{0}"), _entry("packages/x/a.md", "{1}")],
        docs=[(None, "a.md", b"old"), (None, "a.md", b"new")],
    )
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/x/a.md"})
    assert r.status_code == 200 and r.content == b"new"


def test_an_unknown_extension_is_octet_stream(client, world):
    cid, tid, _ = _written(client, world)
    _seed_written(client, world, cid, tid, [_entry("packages/x/a.bin")], docs=[(None, "a.bin", b"abc")])
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/x/a.bin"})
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/octet-stream"


def test_a_non_ascii_filename_gets_an_ascii_fallback_and_rfc5987_name(client, world):
    cid, tid, _ = _written(client, world)
    _seed_written(client, world, cid, tid, [_entry("packages/x/zürich.md")], docs=[(None, "zürich.md", b"abc")])
    r = client.get(f"/api/collections/{cid}/targets/{tid}/files", params={"path": "packages/x/zürich.md"})
    assert r.status_code == 200 and r.content == b"abc"
    cd = r.headers["content-disposition"]
    assert 'filename="z?rich.md"' in cd
    assert "filename*=UTF-8''z%C3%BCrich.md" in cd


def test_row_memory_is_asked_only_for_the_targets_being_launched(client, world, monkeypatch):
    from tret.batches import memory

    asked = []
    real = memory.earlier_attempts

    async def spy(db, batch, targets):
        asked.append(len(targets))
        return await real(db, batch, targets)

    monkeypatch.setattr(memory, "earlier_attempts", spy)
    cid = create(client, row_memory=True, concurrency=1).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    assert asked and asked[0] <= 3 < 5  # one slot plus at most two spares, not all five pending targets


def test_a_failing_sql_statement_in_memory_does_not_abort_the_launch(client, world, monkeypatch):
    from sqlalchemy import text

    from tret.batches import memory

    async def bad(db, batch, targets):
        await db.execute(text("select * from no_such_table_at_all"))

    monkeypatch.setattr(memory, "earlier_attempts", bad)
    cid = create(client, row_memory=True).json()["id"]
    client.post(f"/api/collections/{cid}/start")
    settle(client)
    d = detail(client, cid)
    assert d["status"] != "paused" and all(t["status"] != "pending" for t in d["targets"])


def test_the_launched_message_is_byte_identical_to_the_profile_message_when_row_memory_is_off(client, world):
    world.engine.behaviour.update({"allianz": "gap", "shell": "gap", "apple": "gap", "siemens": "gap", "hsbc-holdings": "gap"})
    first = create(client).json()["id"]
    client.post(f"/api/collections/{first}/start")
    settle(client)
    off = create(client, run_slug="2026-11-05-off2").json()["id"]
    client.post(f"/api/collections/{off}/start")
    settle(client)

    async def compare(db):
        from tret.db.models import Batch, BatchTarget

        batch = await db.get(Batch, uuid.UUID(off))
        profile = service.profile_for(batch)
        targets = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == batch.id))).scalars().all()
        runs = {str(r.task_input.get("_collection_target_id")): r for r in (await db.execute(select(Run))).scalars()}
        return [(runs[str(t.id)].task_input["message"], profile.task_input(batch, t, len(targets))["message"]) for t in targets]

    pairs = run_db(client, world, compare)
    assert pairs and all(a == b for a, b in pairs)
