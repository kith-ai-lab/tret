"""The `disclosure_extract` batch profile end to end with a fake provider (no network): the direct
path in batches/core.py, Run cost/energy/hooks, the tables, NACE proposals, the setting, the API
and the migration."""
# ruff: noqa: F811 - the imported fixture is used by name as a test argument
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.extract_fakes import MD, FakeProvider, FakeRouter, answer, model_id
from tests.test_batches_core import world  # noqa: F401 - fixture
from tret.api import report_extractions as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.batches import core
from tret.batches.profiles import disclosure_extract as de
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Batch,
    BatchTarget,
    CompanyClassification,
    DisclosureReport,
    ReportExtraction,
    ReportModule,
    Run,
    RunOutcome,
    Workspace,
)
from tret.engine import direct
from tret.engine.extensions import get_extension_registry
from tret.engine.routed_call import CallServices
from tret.providers.catalog import get_catalog
from tret.storage import get_store, normalised_key, reset_store


@pytest.fixture
def on(world, tmp_path, monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "disclosure_extract", "on")
    monkeypatch.setattr(s, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(s, "batches_min_free_mb_per_target", 0)
    reset_store()
    nace = {"division": "26", "group": "26.1", "block": "p1.b2", "confidence": 0.8}
    provider = FakeProvider([])
    provider.script = None

    async def complete_json(**kw):
        provider.calls.append(kw)
        from tests.extract_fakes import Usage, JsonCompletion, Decimal  # noqa: F401

        return JsonCompletion(
            payload=answer({"physical_climate_risk": (True, [("p2.b1", "p2.b3")], 0.9)}, nace_proposal=nace),
            usage=Usage(1000, 200, reported_cost_usd=Decimal("0.0123")), model=kw["model"], served_by="fake",
        )

    provider.complete_json = complete_json
    router = FakeRouter()
    svc = CallServices(SimpleNamespace(get=lambda n: provider), router, get_catalog())

    async def fake_services(db, workspace_id):
        return svc

    monkeypatch.setattr(direct, "get_call_services", fake_services)
    hooks: list = []

    async def hook(db, run, workspace_id):
        hooks.append((run.id, run.status, float(run.reported_cost_usd or 0)))

    ext = get_extension_registry()
    ext.add_post_run_hook(hook)
    yield SimpleNamespace(world=world, provider=provider, router=router, hooks=hooks, tmp=tmp_path)
    ext._post_run_hooks.remove(hook)
    reset_store()


def seed(world, names=("alpha", "beta")):
    async def go():
        async with world.factory() as db:
            from tret.db.models import Project

            project = (await db.execute(select(Project))).scalars().first()
            src = Batch(id=uuid.uuid4(), workspace_id=world.ws.id, project_id=project.id, name="Collected",
                        run_slug="collected-1", profile="disclosure", status="completed", settings={}, warnings=[])
            db.add(src)
            await db.flush()
            ids = []
            for i, n in enumerate(names, start=1):
                t = BatchTarget(id=uuid.uuid4(), collection_id=src.id, position=i, slug=n, company=n.title(), market="US",
                                label=n.title(), status="written", identifiers={"cik": str(320000 + i)}, known_urls=[],
                                finding_ids=[], attempts=1, written_files=[], task_input={})
                db.add(t)
                await db.flush()
                doc = uuid.uuid4()
                key = normalised_key(doc, "1")
                await get_store().put(key, MD.encode())
                db.add(DisclosureReport(project_id=project.id, document_id=doc, batch_target_id=t.id, is_primary=True,
                                        status="normalised", markdown_key=key, markdown_chars=len(MD), normaliser_version="1"))
                ids.append(t.id)
            await db.commit()
            return src.id, ids

    return asyncio.run(go())


def run_batch(world, src_id, **kw):
    async def go():
        async with world.factory() as db:
            batch, targets = await de.create_extract_batch(
                db, workspace_id=world.ws.id, user_id=world.user.id, source_batch_id=src_id,
                name="Extract", run_slug="extract-1", **kw)
            await core.start(db, batch)
            bid = batch.id
        assert await core.drain_collection_tasks(60)
        return bid

    return asyncio.run(go())


def fetch(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


def test_end_to_end_tables_run_accounting_hooks_and_nace(on):
    w = on.world
    src_id, tids = seed(w)

    async def pre(db):  # beta already has an SEC classification, which outranks the extractor
        from tret.services import peer_taxonomy as pt

        db.add(CompanyClassification(workspace_id=w.ws.id, company_key_type="cik", company_key=pt.normalise_cik("320002"),
                                     nace_group="64.1", nace_division="64", cluster="banks", nace_section="K",
                                     taxonomy_version="x", source="sec_sic"))
        await db.commit()

    fetch(w, pre)
    bid = run_batch(w, src_id, model_policy={"objective": "eco", "open_weights": "prefer"})

    async def check(db):
        batch = await db.get(Batch, bid)
        targets = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == bid))).scalars().all()
        runs = (await db.execute(select(Run).where(Run.task_input["_collection_id"].as_string() == str(bid)))).scalars().all()
        exts = (await db.execute(select(ReportExtraction))).scalars().all()
        mods = (await db.execute(select(ReportModule))).scalars().all()
        cls = (await db.execute(select(CompanyClassification).where(CompanyClassification.source == "extractor"))).scalars().all()
        outcomes = (await db.execute(select(RunOutcome))).scalars().all()
        return batch, targets, runs, exts, mods, cls, outcomes

    batch, targets, runs, exts, mods, cls, outcomes = fetch(w, check)
    assert batch.profile == "disclosure_extract" and batch.settings["model_policy"] == {"objective": "eco", "open_weights": "prefer", "mode": "auto"}
    assert sorted(t.status for t in targets) == ["completed", "completed"], [t.error for t in targets]
    assert len(exts) == 2 and all(e.status == "extracted" and e.module_set == "kith-climate@0.1" for e in exts)
    assert len(mods) == 8 and sum(m.present for m in mods) == 2
    present = next(m for m in mods if m.present)
    assert present.module == "physical_climate_risk" and present.word_count > 0 and present.text_key
    assert b"| A | flood |" in asyncio.run(get_store().get(present.text_key))
    # runs: structured_call, cost + reported cost + energy, routing recorded, hooks, no priors
    assert len(runs) == 2
    for r in runs:
        assert r.task_type == "structured_call" and r.status == "completed" and r.model_used
        assert float(r.cost_usd) > 0 and float(r.reported_cost_usd) == pytest.approx(0.0123)
        assert r.energy_wh is not None and r.energy_accounting and r.model_timeline
        assert r.routing["structured_calls"][0]["reasoning"] == "fake router: cheapest fit"
        assert r.input_tokens == 1000 and r.iterations == 1 and r.finished_at
    assert sorted(h[1] for h in on.hooks) == ["completed", "completed"] and all(h[2] > 0 for h in on.hooks)
    assert outcomes == []
    # the outcome is persisted on the run, the target, the records and the API fields
    assert all(r.task_input["_direct_outcome"]["status"] == "extracted" for r in runs)
    assert all(t.outcome["status"] == "extracted" and t.outcome["extraction_id"] and t.outcome["present"] == ["physical_climate_risk"]
               and t.outcome["models"] == [model_id()] for t in targets)
    assert sorted(e.model for e in exts) == [model_id()] * 2 and all(e.models == [model_id()] for e in exts)
    t0 = targets[0]
    assert de.PROFILE.target_extra(t0, runs[0], [])["extraction"]["present"] == ["physical_climate_risk"]
    assert de.PROFILE.target_extra(t0, runs[0], [])["extraction"]["extraction_id"] == t0.outcome["extraction_id"]

    async def records(db):
        b = await db.get(Batch, bid)
        rec = await de.PROFILE.records(db, b)
        return de.PROFILE.record_files(rec)

    files = fetch(w, records)
    import json as _json

    lines = [_json.loads(x) for x in files["results.jsonl"][0].decode().splitlines()]
    assert all(r["status"] == "completed" and r["outcome"]["status"] == "extracted" and r["outcome"]["present"] for r in lines)
    # filed under the system extractor harness, not the collector or any other
    from tret.db.models import Harness

    hs = {r.harness_id for r in runs}
    assert len(hs) == 1
    names = fetch(w, lambda db: db.get(Harness, next(iter(hs))))
    assert names.name == "Disclosure Extractor" and names.tool_names == []
    # the router was asked, never pinned
    assert all("run_override" not in a and a["model_policy"]["open_weights"] == "prefer" for a in on.router.asked)
    # NACE proposal only for the company without an sec_sic row
    assert [(c.company_key, c.nace_division, c.nace_group) for c in cls] == [("0000320001", "26", "26.1")]
    assert cls[0].evidence["block"] == "p1.b2" and cls[0].confidence == 0.8


def test_off_refuses_create_and_launch(on, monkeypatch):
    w = on.world
    src_id, _ = seed(w, ("alpha",))

    async def draft(db):
        return await de.create_extract_batch(db, workspace_id=w.ws.id, user_id=None, source_batch_id=src_id,
                                             name="E", run_slug="extract-off")

    batch, _ = fetch(w, draft)
    monkeypatch.setattr(get_settings(), "disclosure_extract", "off")

    async def create_off(db):
        with pytest.raises(HTTPException) as e:
            await de.create_extract_batch(db, workspace_id=w.ws.id, user_id=None, source_batch_id=src_id,
                                          name="E", run_slug="extract-off2")
        assert e.value.status_code == 409 and "TRET_DISCLOSURE_EXTRACT" in e.value.detail
        b = await db.get(Batch, batch.id)
        with pytest.raises(HTTPException) as e:
            await core.start(db, b)
        assert e.value.status_code == 409

    fetch(w, create_off)


def test_pinned_policy_and_only_missing(on):
    w = on.world
    src_id, _ = seed(w, ("alpha",))

    async def pinned(db):
        with pytest.raises(HTTPException) as e:
            await de.create_extract_batch(db, workspace_id=w.ws.id, user_id=None, source_batch_id=src_id, name="E",
                                          run_slug="pin-1", model_policy={"mode": "pinned", "model": "a/b"})
        assert e.value.status_code == 422

    fetch(w, pinned)
    run_batch(w, src_id)

    async def missing(db):
        with pytest.raises(HTTPException) as e:
            await de.create_extract_batch(db, workspace_id=w.ws.id, user_id=None, source_batch_id=src_id, name="E",
                                          run_slug="again-1", only_missing=True)
        assert e.value.status_code == 422 and "already" in e.value.detail

    fetch(w, missing)


def test_a_failing_model_fails_the_target_and_still_books_cost(on):
    w = on.world
    src_id, _ = seed(w, ("alpha",))
    from tests.extract_fakes import http_error

    async def boom(**kw):
        raise http_error(400, "request blocked by content policy")

    on.provider.complete_json = boom
    bid = run_batch(w, src_id)

    async def check(db):
        t = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == bid))).scalars().one()
        r = await db.get(Run, t.run_id)
        return t, r

    t, r = fetch(w, check)
    assert t.status == "failed" and r.status == "failed" and "refused" in (r.error or "")
    assert len(on.hooks) == 1


# ── API ─────────────────────────────────────────────────────────────────────────
def test_api_404_when_off_isolation_and_module_text(on, monkeypatch):
    w = on.world
    src_id, _ = seed(w, ("alpha",))
    run_batch(w, src_id)

    async def ids(db):
        e = (await db.execute(select(ReportExtraction))).scalars().one()
        return e.id, e.disclosure_report_id

    ext_id, report_id = fetch(w, ids)
    state = {"ws": w.ws.id}
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with w.factory() as s:
            yield s

    async def _user():
        return w.user

    async def _ws():
        return WorkspaceContext(Workspace(id=state["ws"], name="W", kind="team"), "approver")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as c:
        lst = c.get(f"/api/disclosure-reports/{report_id}/extractions").json()["extractions"]
        assert [e["id"] for e in lst] == [str(ext_id)]
        d = c.get(f"/api/report-extractions/{ext_id}").json()
        assert d["status"] == "extracted" and len(d["modules"]) == 4 and d["metadata"]["report_year"] == 2024
        pm = next(m for m in d["modules"] if m["module"] == "physical_climate_risk")
        assert pm["present"] and pm["spans"][0]["start"] == "p2.b1" and pm["spans"][0]["start_page"] == 2
        r = c.get(f"/api/report-extractions/{ext_id}/modules/physical_climate_risk/text")
        assert r.status_code == 200 and "Physical risks: floods" in r.text and "## Page" not in r.text
        assert c.get(f"/api/report-extractions/{ext_id}/modules/targets_and_transition/text").status_code == 404
        assert c.get(f"/api/report-extractions/{ext_id}/modules/nope/text").status_code == 404
        state["ws"] = uuid.uuid4()  # another workspace
        assert c.get(f"/api/report-extractions/{ext_id}").status_code == 404
        assert c.get(f"/api/report-extractions/{ext_id}/modules/physical_climate_risk/text").status_code == 404
        assert c.get(f"/api/disclosure-reports/{report_id}/extractions").json() == {"extractions": []}
        state["ws"] = w.ws.id
        monkeypatch.setattr(get_settings(), "disclosure_extract", "off")
        assert c.get(f"/api/report-extractions/{ext_id}").status_code == 404
        assert c.get(f"/api/disclosure-reports/{report_id}/extractions").status_code == 404


# ── migration ───────────────────────────────────────────────────────────────────
def test_migration_chain_and_markers():
    from alembic.script import ScriptDirectory

    from tret.db.migrate import REVISION_MARKERS, alembic_config

    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["b3f8c2a6d914"]  # tournament tables (PR 6)
    assert script.get_revision("b3f8c2a6d914").down_revision == "a9d4e7c1f352"
    assert script.get_revision("a9d4e7c1f352").down_revision == "e7a4c9d2b861"
    assert script.get_revision("e7a4c9d2b861").down_revision == "d8f3a1c5b7e2"
    assert script.get_revision("d8f3a1c5b7e2").down_revision == "c4a7e2b9d158"
    assert dict(REVISION_MARKERS)["d8f3a1c5b7e2"] == (("report_extractions", None), ("report_modules", None))
    mod = script.get_revision("d8f3a1c5b7e2").module
    assert callable(mod.upgrade) and callable(mod.downgrade)


def test_run_outcome_marker_for_structured_calls():
    from tret.batches.rubrics import STRUCTURED_CALL_KEY, is_structured_call_run

    assert is_structured_call_run({STRUCTURED_CALL_KEY: True}) and not is_structured_call_run({})


# ── review fixes ────────────────────────────────────────────────────────────────
def test_a_worse_result_never_overwrites_a_better_stored_one(on):
    from tret.evaluation import extract as ex
    from tret.evaluation.modules import default_module_set
    from tret.storage import module_text_key

    w = on.world
    src_id, _ = seed(w, ("alpha",))
    ms = default_module_set()

    async def go(db):
        report = (await db.execute(select(DisclosureReport))).scalars().one()
        good = ex.ExtractionResult(ex.STATUS_EXTRACTED, {"doc_type": "annual_report"}, {
            "physical_climate_risk": ex.ModuleResult("physical_climate_risk", True, [], "kept text", 2, 0.9)}, 1, [])
        row, stored = await ex.store_result(db, report, good, ms, workspace_id=w.ws.id, batch_target_id=None, run_id=None, models=["m1"])
        assert stored
        await db.commit()
        for status in (ex.STATUS_FAILED, ex.STATUS_PARTIAL):
            bad = ex.ExtractionResult(status, {}, {} if status == ex.STATUS_FAILED else {
                "physical_climate_risk": ex.ModuleResult("physical_climate_risk", False)}, 1, [{"kind": "x"}], error="boom")
            row2, stored = await ex.store_result(db, report, bad, ms, workspace_id=w.ws.id, batch_target_id=None, run_id=uuid.uuid4(), models=["m2"])
            await db.commit()
            assert not stored and row2.id == row.id and row2.status == ex.STATUS_EXTRACTED and row2.models == ["m1"]
        mods = (await db.execute(select(ReportModule))).scalars().all()
        assert len(mods) == 1 and mods[0].present and mods[0].text_key == module_text_key(report.id, "1", ms.ident, "physical_climate_risk", mods[0].text_sha256)
        assert await get_store().get(mods[0].text_key) == b"kept text"
        attempts = [i for i in row2.issues if i["kind"] == "later_attempt_not_stored"]
        assert [a["status"] for a in attempts] == ["failed", "partial"] and attempts[0]["error"] == "boom"
        # a partial is replaced by an equal-or-better one; extracted replaces partial
        row3 = await db.get(type(row), row.id)
        row3.status = ex.STATUS_PARTIAL
        await db.commit()
        _, stored = await ex.store_result(db, report, good, ms, workspace_id=w.ws.id, batch_target_id=None, run_id=None, models=["m3"])
        assert stored and row3.status == ex.STATUS_EXTRACTED and row3.models == ["m3"]
        await db.commit()

    fetch(w, go)


def test_concurrent_insert_of_the_same_extraction_is_reconciled(on, monkeypatch):
    from tret.evaluation import extract as ex
    from tret.evaluation.modules import default_module_set

    w = on.world
    seed(w, ("alpha",))
    ms = default_module_set()
    res = ex.ExtractionResult(ex.STATUS_EXTRACTED, {}, {"physical_climate_risk": ex.ModuleResult("physical_climate_risk")}, 1, [])

    async def go(db):
        report = (await db.execute(select(DisclosureReport))).scalars().one()
        await ex.store_result(db, report, res, ms, workspace_id=w.ws.id, batch_target_id=None, run_id=None, models=["a"])
        await db.commit()
        real = ex._find_extraction
        calls = {"n": 0}

        async def blind_first(db_, report_id, module_set):  # the other writer got there between select and insert
            calls["n"] += 1
            return None if calls["n"] == 1 else await real(db_, report_id, module_set)

        monkeypatch.setattr(ex, "_find_extraction", blind_first)
        failed = ex.ExtractionResult(ex.STATUS_FAILED, {}, {}, 1, [], error="x")
        row, stored = await ex.store_result(db, report, failed, ms, workspace_id=w.ws.id, batch_target_id=None, run_id=None, models=["b"])
        assert calls["n"] == 2 and not stored and row.status == ex.STATUS_EXTRACTED
        await db.commit()
        assert len((await db.execute(select(ReportExtraction))).scalars().all()) == 1

    fetch(w, go)


def test_extractor_harness_is_created_once_per_workspace_and_is_the_only_one_used(on):
    from tret.db.models import Harness
    from tret.services.workspace import ensure_extractor_harness

    w = on.world

    async def go(db):
        a = await ensure_extractor_harness(db, w.ws.id)
        b = await ensure_extractor_harness(db, w.ws.id)
        await db.commit()
        assert a.id == b.id
        n = (await db.execute(select(Harness).where(Harness.name == "Disclosure Extractor"))).scalars().all()
        assert len(n) == 1 and n[0].workspace_id == w.ws.id
        assert (await de.PROFILE.harness_for(db, SimpleNamespace(workspace_id=w.ws.id, harness_id=None))).id == a.id
        other = Harness(id=uuid.uuid4(), workspace_id=w.ws.id, name="Disclosure Collector", tool_names=[])
        db.add(other)
        await db.commit()
        # a batch created against another harness is refused
        with pytest.raises(HTTPException):
            await de.PROFILE.validate_settings(db, w.ws.id, SimpleNamespace(
                harness_id=other.id, module_set_path=None, model_policy=None, window_tokens=None, concurrency=1,
                first_batch_size=0, max_cost_usd=None, only_missing=False, source_batch_id=None))

    fetch(w, go)


def test_setup_failure_still_closes_the_run_and_fires_the_hook(on, monkeypatch):
    w = on.world
    src_id, _ = seed(w, ("alpha",))

    async def boom(*a, **k):
        raise RuntimeError("emissions layers exploded")

    monkeypatch.setattr(direct, "build_emissions_context", boom)
    bid = run_batch(w, src_id)

    async def check(db):
        t = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == bid))).scalars().one()
        return t, await db.get(Run, t.run_id)

    t, r = fetch(w, check)
    assert t.status == "failed" and r.status == "failed" and "emissions layers exploded" in r.error and r.finished_at
    assert len(on.hooks) == 1 and on.hooks[0][1] == "failed"


def test_missing_harness_fails_the_run_cleanly(on):
    w = on.world

    async def go(db):
        from tret.db.models import Project

        project = (await db.execute(select(Project))).scalars().first()
        run = Run(id=uuid.uuid4(), project_id=project.id, harness_id=uuid.uuid4(), task_type="structured_call", task_input={}, document_ids=[])
        db.add(run)
        await db.commit()
        await direct.execute_direct(run.id, de.PROFILE, w.factory)
        await db.refresh(run)
        return run

    r = fetch(w, go)
    assert r.status == "failed" and "harness" in r.error and on.hooks == []


def test_time_limit_cancels_the_run_books_the_sent_call_and_fires_the_hook(on, monkeypatch):
    from tret.services import collections as service

    w = on.world
    src_id, _ = seed(w, ("alpha",))
    monkeypatch.setattr(get_settings(), "collections_run_timeout_seconds", 1)
    monkeypatch.setattr(service, "CANCEL_GRACE_SECONDS", 1.0, raising=False)

    async def hang(**kw):
        await asyncio.sleep(60)

    on.provider.complete_json = hang
    bid = run_batch(w, src_id)

    async def check(db):
        t = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == bid))).scalars().one()
        return t, await db.get(Run, t.run_id, populate_existing=True)

    t, r = fetch(w, check)
    assert t.error == "timeout" and r.status == "cancelled" and r.error == core.TIMEOUT_RUN_ERROR
    assert float(r.cost_usd) > 0 and r.input_tokens > 0  # the call that was sent is paid for (estimated)
    assert r.model_timeline and r.model_timeline[0]["estimated"] is True
    assert len(on.hooks) == 1 and on.hooks[0][1] == "cancelled"


def test_a_report_of_another_workspace_is_refused(on):
    from tret.db.models import Project

    w = on.world
    src_id, _ = seed(w, ("alpha",))

    async def foreign(db):
        other = Workspace(id=uuid.uuid4(), name="Other", kind="team")
        db.add(other)
        await db.flush()
        proj = Project(id=uuid.uuid4(), workspace_id=other.id, name="P2")
        db.add(proj)
        await db.flush()
        doc = uuid.uuid4()
        key = normalised_key(doc, "1")
        await get_store().put(key, MD.encode())
        rep = DisclosureReport(project_id=proj.id, document_id=doc, is_primary=False, status="normalised",
                               markdown_key=key, markdown_chars=len(MD), normaliser_version="1")
        db.add(rep)
        await db.commit()
        return rep.id

    foreign_id = fetch(w, foreign)

    async def go():
        async with w.factory() as db:
            batch, targets = await de.create_extract_batch(db, workspace_id=w.ws.id, user_id=None, source_batch_id=src_id,
                                                           name="E", run_slug="xws-1")
            targets[0].task_input = {**targets[0].task_input, "report_id": str(foreign_id)}
            await db.commit()
            await core.start(db, batch)
            bid = batch.id
        assert await core.drain_collection_tasks(60)
        return bid

    bid = asyncio.run(go())

    async def check(db):
        t = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == bid))).scalars().one()
        return t, (await db.execute(select(ReportExtraction))).scalars().all()

    t, exts = fetch(w, check)
    assert t.status == "failed" and "does not belong" in t.error and exts == [] and on.provider.calls == []
