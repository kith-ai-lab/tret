"""Claim density storage and the extraction review list (tret/evaluation/density.py, review.py):
store_result fills the density, the backfill respects versions and missing blobs, every review
item kind, stored decisions, workspace isolation, the API and its 404 while the setting is off."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_disclosure_extract import fetch, on, run_batch, seed  # noqa: F401 - fixtures and helpers
from tret.api import report_extractions as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Batch,
    BatchTarget,
    CompanyClassification,
    ExtractionReview,
    ReportExtraction,
    ReportModule,
    Workspace,
)
from tret.evaluation import density, review
from tret.services import peer_taxonomy as pt
from tret.storage import get_store

PHYS = "physical_climate_risk"


def setup_two(on):
    """Two extracted companies; returns (source batch id, extraction batch id)."""
    src_id, _ = seed(on.world, ("alpha", "beta"))
    return src_id, run_batch(on.world, src_id)


def mutate(w, fn):
    async def go():
        async with w.factory() as db:
            await fn(db)
            await db.commit()

    asyncio.run(go())


async def ext_of(db, slug: str) -> ReportExtraction:
    return (await db.execute(
        select(ReportExtraction).join(BatchTarget, BatchTarget.id == ReportExtraction.batch_target_id)
        .where(BatchTarget.slug == slug)
    )).scalar_one()


async def mod_of(db, ext: ReportExtraction, module: str = PHYS) -> ReportModule:
    return (await db.execute(
        select(ReportModule).where(ReportModule.extraction_id == ext.id, ReportModule.module == module)
    )).scalar_one()


def items(w, **kw):
    async def go(db):
        return await review.review_items(db, workspace_id=kw.pop("workspace_id", w.ws.id), **kw)

    return fetch(w, go)


def kinds_of(its, company="Alpha"):
    return sorted(i.kind for i in its if i.company == company)


# ── claim density is stored ─────────────────────────────────────────────────────────────────
def test_store_result_fills_density_for_present_modules_only(on):
    setup_two(on)

    async def read(db):
        ext = await ext_of(db, "alpha")
        mods = {m.module: m for m in (await db.execute(select(ReportModule).where(ReportModule.extraction_id == ext.id))).scalars()}
        return mods

    mods = fetch(on.world, read)
    phys = mods[PHYS]
    assert phys.present and phys.claim_count is not None and phys.density_version == density.DENSITY_VERSION
    assert set(phys.claim_counts) == set(density.CATEGORIES) and sum(phys.claim_counts.values()) == phys.claim_count
    assert phys.claim_density == round(phys.claim_count * 100 / phys.word_count, 2)
    for slug, m in mods.items():
        if not m.present:
            assert m.claim_count is None and m.claim_density is None and m.claim_counts is None and m.density_version is None


def test_api_module_output_carries_density(on):
    setup_two(on)
    ext_id = fetch(on.world, lambda db: _first_ext_id(db))
    with client(on.world) as c:
        d = c.get(f"/api/report-extractions/{ext_id}").json()
    phys = next(m for m in d["modules"] if m["module"] == PHYS)
    assert phys["density_version"] == "claims-1" and phys["claim_count"] is not None
    assert set(phys["claim_counts"]) == set(density.CATEGORIES) and "claim_density" in phys
    absent = next(m for m in d["modules"] if not m["present"])
    assert absent["claim_count"] is None and absent["density_version"] is None


async def _first_ext_id(db):
    return (await db.execute(select(ReportExtraction.id).order_by(ReportExtraction.created_at))).scalars().first()


# ── backfill ────────────────────────────────────────────────────────────────────────────────
def recompute(w, **kw):
    async def go(db):
        stats = await density.recompute_claim_density(db, workspace_id=kw.pop("workspace_id", w.ws.id), **kw)
        await db.commit()
        return stats

    return fetch(w, go)


def test_recompute_respects_version_force_and_missing_blobs(on):
    src_id, ext_batch = setup_two(on)
    w = on.world

    # everything is current: nothing to do, unless forced
    s = recompute(w, batch_id=src_id)
    assert s == {"considered": 2, "updated": 0, "skipped_current": 2, "missing_text": 0}
    assert recompute(w, batch_id=src_id, force=True)["updated"] == 2

    # an old version is recomputed, and the stored values come from the blob text
    async def age(db):
        for slug in ("alpha", "beta"):
            m = await mod_of(db, await ext_of(db, slug))
            m.density_version, m.claim_count, m.claim_density, m.claim_counts = "claims-0", None, None, None

    mutate(w, age)
    s = recompute(w, batch_id=ext_batch)  # the extraction batch id works too
    assert s["updated"] == 2 and s["missing_text"] == 0

    async def read(db):
        m = await mod_of(db, await ext_of(db, "alpha"))
        text = (await get_store().get(m.text_key)).decode()
        return m, density.claim_density(text)

    m, d = fetch(w, read)
    assert (m.claim_count, m.claim_density, m.density_version) == (d.claims, d.per_100_words, density.DENSITY_VERSION)

    # a missing blob is skipped and counted, never a crash; the other module still updates
    async def break_one(db):
        a = await mod_of(db, await ext_of(db, "alpha"))
        b = await mod_of(db, await ext_of(db, "beta"))
        a.density_version = b.density_version = "claims-0"
        return a.text_key

    key = fetch(w, lambda db: _commit(db, break_one))
    asyncio.run(get_store().delete(key))
    s = recompute(w, batch_id=src_id)
    assert s["missing_text"] == 1 and s["updated"] == 1


async def _commit(db, fn):
    out = await fn(db)
    await db.commit()
    return out


def test_recompute_is_workspace_scoped(on):
    src_id, _ = setup_two(on)
    s = recompute(on.world, workspace_id=uuid.uuid4(), batch_id=src_id, force=True)
    assert s["considered"] == 0 and s["updated"] == 0


# ── review items: every kind ────────────────────────────────────────────────────────────────
def test_a_clean_extraction_has_no_review_items(on):
    setup_two(on)
    assert items(on.world) == []


def test_module_items(on):
    setup_two(on)
    w = on.world

    async def go(db):
        m = await mod_of(db, await ext_of(db, "alpha"))
        m.confidence = 0.4
        m.truncated = True
        m.word_count = 150
        m.claim_density = 0.1
        b = await mod_of(db, await ext_of(db, "beta"))
        b.text_key, b.word_count = None, 0

    mutate(w, go)
    its = items(w)
    assert kinds_of(its) == ["module_low_confidence", "module_low_density", "module_truncated"]
    low = next(i for i in its if i.kind == "module_low_confidence")
    assert low.module == PHYS and low.value == 0.4 and low.item_key == f"module:{PHYS}:low_confidence"
    assert next(i for i in its if i.kind == "module_low_density").item_key == f"module:{PHYS}:low_density"
    assert kinds_of(its, "Beta") == ["module_present_no_text"]
    # the density item needs 100 words; the thresholds come from the settings
    async def short(db):
        (await mod_of(db, await ext_of(db, "alpha"))).word_count = 99

    mutate(w, short)
    assert "module_low_density" not in kinds_of(items(w))
    get_settings().disclosure_review_min_confidence = 0.3
    try:
        assert "module_low_confidence" not in kinds_of(items(w))
    finally:
        get_settings().disclosure_review_min_confidence = 0.6


def test_status_items_and_failed_lists_only_the_status(on):
    setup_two(on)
    w = on.world

    async def go(db):
        a = await ext_of(db, "alpha")
        a.status = "partial"
        a.issues = [{"kind": "window_failed", "window": 2, "detail": "boom"}]
        b = await ext_of(db, "beta")
        b.status = "failed"
        b.issues = [{"kind": "window_failed", "window": 1, "detail": "model unavailable"}]
        (await mod_of(db, b)).confidence = 0.1  # ignored: a failed extraction has no modules to judge

    mutate(w, go)
    its = items(w)
    assert kinds_of(its, "Alpha") == ["extraction_partial"]
    assert kinds_of(its, "Beta") == ["extraction_failed"]
    assert [i.kind for i in its][0] == "extraction_failed"  # failed sorts before partial
    assert "model unavailable" in next(i for i in its if i.kind == "extraction_failed").detail
    assert next(i for i in its if i.kind == "extraction_partial").item_key == "status:partial"
    assert next(i for i in its if i.kind == "extraction_failed").item_key == "status:failed"


def test_span_issues_group_by_kind_and_module(on):
    setup_two(on)

    async def go(db):
        a = await ext_of(db, "alpha")
        a.issues = [
            {"kind": "invalid_span", "module": PHYS, "window": 1},
            {"kind": "invalid_span", "module": PHYS, "window": 2},
            {"kind": "reversed_span", "module": "governance_and_capex", "window": 1},
            {"kind": "present_without_valid_span", "module": PHYS},
            {"kind": "invalid_block", "field": "assurance_block", "window": 1},
            {"kind": "truncated", "module": PHYS},  # not a span issue: has its own item
        ]

    mutate(on.world, go)
    its = [i for i in items(on.world) if i.kind == "span_issues"]
    assert sorted(i.item_key for i in its) == [
        "issue:invalid_block:assurance_block",
        f"issue:invalid_span:{PHYS}",
        "issue:present_without_valid_span:" + PHYS,
        "issue:reversed_span:governance_and_capex",
    ]
    assert next(i for i in its if i.item_key == f"issue:invalid_span:{PHYS}").value == 2.0


def classification_rows(w):
    async def go(db):
        return list((await db.execute(select(CompanyClassification))).scalars())

    return fetch(w, go)


def test_nace_low_confidence_needs_the_extractor_to_be_the_effective_source(on):
    setup_two(on)
    w = on.world
    rows = classification_rows(w)
    assert {r.source for r in rows} == {"extractor"}  # the fake extractor proposed 26.1 at 0.8

    async def lower(db):
        for slug in ("alpha", "beta"):
            e = await ext_of(db, slug)
            e.metadata_json = {**e.metadata_json, "nace_proposal": {**e.metadata_json["nace_proposal"], "confidence": 0.3}}
        for r in (await db.execute(select(CompanyClassification))).scalars():
            r.confidence = 0.3

    mutate(w, lower)
    its = items(w)
    assert kinds_of(its, "Alpha") == ["nace_low_confidence"] and kinds_of(its, "Beta") == ["nace_low_confidence"]
    low = next(i for i in its if i.kind == "nace_low_confidence")
    assert low.item_key == "nace:low_confidence" and low.value == 0.3 and "company-classifications/manual" in low.detail

    # a manual (or sec_sic) classification outranks the extractor: nothing left to look at for alpha
    async def manual(db):
        e = await ext_of(db, "alpha")
        t = await db.get(BatchTarget, e.batch_target_id)
        key_type, key = pt.target_keys(t)[0]
        db.add(CompanyClassification(
            workspace_id=w.ws.id, company_key_type=key_type, company_key=key, source="manual",
            nace_division="26", nace_group="26.1", cluster="x", nace_section="C", taxonomy_version="t",
            source_code="26.1", confidence=1.0, reviewed_by="a@example.com",
        ))

    mutate(w, manual)
    its = items(w)
    assert kinds_of(its, "Alpha") == [] and kinds_of(its, "Beta") == ["nace_low_confidence"]


def test_nace_missing_and_gics_disagrees(on):
    setup_two(on)
    w = on.world

    async def go(db):
        a = await ext_of(db, "alpha")
        a.metadata_json = {**a.metadata_json, "nace_proposal": None}
        for r in (await db.execute(select(CompanyClassification))).scalars():
            t = await db.get(BatchTarget, a.batch_target_id)
            if (r.company_key_type, r.company_key) == pt.target_keys(t)[0]:
                await db.delete(r)  # alpha: nothing at all
            else:
                r.gics_disagrees = True  # beta: the extractor row disagrees with the register's GICS sector

    mutate(w, go)
    its = items(w)
    assert kinds_of(its, "Alpha") == ["nace_missing"]
    assert kinds_of(its, "Beta") == ["gics_disagrees"]
    assert next(i for i in its if i.kind == "gics_disagrees").item_key == "nace:gics_disagrees"

    async def manual(db):  # a manual row is a human's call: the disagreement is not listed
        for r in (await db.execute(select(CompanyClassification))).scalars():
            r.source = "manual"

    mutate(w, manual)
    assert kinds_of(items(w), "Beta") == []


def test_sorting_kind_filter_and_batch_filter(on):
    src_id, ext_batch = setup_two(on)
    w = on.world

    async def go(db):
        a = await ext_of(db, "alpha")
        a.status = "partial"
        a.issues = [{"kind": "window_failed", "window": 1, "detail": "x"}]
        (await mod_of(db, a)).truncated = True
        b = await ext_of(db, "beta")
        b.status = "failed"
        b.issues = []

    mutate(w, go)
    its = items(w)
    assert [i.kind for i in its] == ["extraction_failed", "extraction_partial", "module_truncated"]
    assert [i.kind for i in items(w, kinds=["module_truncated"])] == ["module_truncated"]
    with pytest.raises(ValueError):
        items(w, kinds=["nope"])
    assert len(items(w, batch_id=src_id)) == 3 and len(items(w, batch_id=ext_batch)) == 3
    assert items(w, batch_id=uuid.uuid4()) == []


# ── decisions ───────────────────────────────────────────────────────────────────────────────
def decide(w, ext_id, key, decision="dismissed", note=None, ws=None):
    async def go(db):
        out = await review.set_decision(
            db, workspace_id=ws or w.ws.id, extraction_id=ext_id, item_key=key, decision=decision,
            note=note, reviewed_by="a@example.com",
        )
        await db.commit()
        return out

    return fetch(w, go)


def test_a_decision_hides_the_item_until_include_resolved(on):
    setup_two(on)
    w = on.world

    async def go(db):
        a = await ext_of(db, "alpha")
        (await mod_of(db, a)).truncated = True
        (await mod_of(db, a)).confidence = 0.2
        return a.id

    ext_id = fetch(w, lambda db: _commit(db, go))
    assert kinds_of(items(w)) == ["module_low_confidence", "module_truncated"]
    out = decide(w, ext_id, f"module:{PHYS}:truncated", "accepted", " fine as cut ")
    assert out["decision"] == "accepted" and out["note"] == "fine as cut" and out["reviewed_by"] == "a@example.com"
    assert kinds_of(items(w)) == ["module_low_confidence"]
    both = items(w, include_resolved=True)
    done = next(i for i in both if i.kind == "module_truncated")
    assert done.decision["decision"] == "accepted" and done.decision["note"] == "fine as cut"
    assert next(i for i in both if i.kind == "module_low_confidence").decision is None
    # deciding again replaces the decision, never duplicates the row
    decide(w, ext_id, f"module:{PHYS}:truncated", "fixed")
    assert fetch(w, lambda db: _count(db)) == 1
    assert next(i for i in items(w, include_resolved=True) if i.kind == "module_truncated").decision["decision"] == "fixed"


async def _count(db):
    return len((await db.execute(select(ExtractionReview))).scalars().all())


def test_unknown_item_bad_decision_and_other_workspace_are_rejected(on):
    setup_two(on)
    w = on.world
    ext_id = fetch(w, lambda db: _first_ext_id(db))
    with pytest.raises(review.ReviewNotFound):
        decide(w, ext_id, "module:nope:truncated")  # not an item this extraction produces
    with pytest.raises(review.ReviewNotFound):
        decide(w, ext_id, "status:failed")  # extracted fine: no status item
    with pytest.raises(ValueError):
        decide(w, ext_id, "status", "maybe")
    with pytest.raises(review.ReviewNotFound):
        decide(w, uuid.uuid4(), "status")
    assert fetch(w, lambda db: _count(db)) == 0

    async def go(db):
        (await mod_of(db, await db.get(ReportExtraction, ext_id))).truncated = True

    mutate(w, go)
    with pytest.raises(review.ReviewNotFound):  # the extraction belongs to another workspace
        decide(w, ext_id, f"module:{PHYS}:truncated", ws=uuid.uuid4())
    assert items(w, workspace_id=uuid.uuid4()) == []


# ── API ─────────────────────────────────────────────────────────────────────────────────────
def client(w, state=None, role="approver"):
    state = state if state is not None else {"ws": w.ws.id}
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with w.factory() as s:
            yield s

    async def _user():
        return w.user

    async def _ws():
        return WorkspaceContext(Workspace(id=state["ws"], name="W", kind="team"), role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    c = TestClient(app)
    c.state = state
    return c


def test_review_api(on, monkeypatch):
    src_id, ext_batch = setup_two(on)
    w = on.world

    async def go(db):
        a = await ext_of(db, "alpha")
        (await mod_of(db, a)).truncated = True
        b = await ext_of(db, "beta")
        b.status = "failed"
        b.issues = []
        return a.id

    ext_id = fetch(w, lambda db: _commit(db, go))
    with client(w) as c:
        r = c.get("/api/report-extractions/review", params={"batch_id": str(src_id)})
        assert r.status_code == 200 and r.json()["count"] == 2 and r.json()["total"] == 2
        got = r.json()["items"]
        assert [i["kind"] for i in got] == ["extraction_failed", "module_truncated"]
        assert got[0]["company"] == "Beta" and got[0]["decision"] is None and got[0]["severity"] == 0
        assert set(got[1]) >= {"extraction_id", "disclosure_report_id", "batch_target_id", "item_key", "module", "detail", "value"}
        assert c.get("/api/report-extractions/review", params={"kind": "module_truncated"}).json()["count"] == 1
        assert c.get("/api/report-extractions/review", params={"kind": ["module_truncated", "extraction_failed"]}).json()["count"] == 2
        assert c.get("/api/report-extractions/review", params={"kind": "nope"}).status_code == 422
        page = c.get("/api/report-extractions/review", params={"limit": 1, "offset": 1}).json()
        assert page["total"] == 2 and page["count"] == 1 and page["items"][0]["kind"] == "module_truncated"
        assert c.get("/api/report-extractions/review", params={"offset": 5}).json()["count"] == 0
        for bad in ({"limit": 0}, {"limit": 5001}, {"offset": -1}):
            assert c.get("/api/report-extractions/review", params=bad).status_code == 422
        assert c.get("/api/report-extractions/review", params={"batch_id": str(uuid.uuid4())}).status_code == 404
        assert c.get("/api/report-extractions/review", params={"batch_id": str(ext_batch)}).json()["count"] == 2

        body = {"item_key": f"module:{PHYS}:truncated", "decision": "dismissed", "note": "ok"}
        r = c.put(f"/api/report-extractions/{ext_id}/review", json=body)
        assert r.status_code == 200 and r.json()["reviewed_by"] == "a@example.com" and r.json()["decision"] == "dismissed"
        assert c.get("/api/report-extractions/review").json()["count"] == 1
        resolved = c.get("/api/report-extractions/review", params={"include_resolved": "true"}).json()["items"]
        assert next(i for i in resolved if i["kind"] == "module_truncated")["decision"]["note"] == "ok"

        assert c.put(f"/api/report-extractions/{ext_id}/review", json={**body, "item_key": "nope"}).status_code == 404
        assert c.put(f"/api/report-extractions/{ext_id}/review", json={**body, "decision": "maybe"}).status_code == 422
        assert c.put(f"/api/report-extractions/{uuid.uuid4()}/review", json=body).status_code == 404

        # another workspace sees nothing and cannot decide
        c.state["ws"] = uuid.uuid4()
        assert c.get("/api/report-extractions/review").json() == {"items": [], "count": 0, "total": 0, "limit": 500, "offset": 0}
        assert c.put(f"/api/report-extractions/{ext_id}/review", json=body).status_code == 404
        assert c.get("/api/report-extractions/review", params={"batch_id": str(src_id)}).status_code == 404
        c.state["ws"] = w.ws.id

        # deciding needs an approver
        analyst = client(w, role="analyst")
        assert analyst.put(f"/api/report-extractions/{ext_id}/review", json=body).status_code == 403
        assert analyst.get("/api/report-extractions/review").status_code == 200

        monkeypatch.setattr(get_settings(), "disclosure_extract", "off")
        assert c.get("/api/report-extractions/review").status_code == 404
        assert c.put(f"/api/report-extractions/{ext_id}/review", json=body).status_code == 404


def test_batch_of_another_workspace_is_hidden(on):
    setup_two(on)
    w = on.world

    async def go(db):
        other = Workspace(id=uuid.uuid4(), name="Other", kind="team")
        db.add(other)
        await db.flush()
        from tret.db.models import Project

        p = Project(id=uuid.uuid4(), workspace_id=other.id, name="P")
        db.add(p)
        await db.flush()
        b = Batch(id=uuid.uuid4(), workspace_id=other.id, project_id=p.id, name="Theirs", run_slug="theirs-1",
                  profile="disclosure", status="completed", settings={}, warnings=[])
        db.add(b)
        return b.id

    other_batch = fetch(w, lambda db: _commit(db, go))
    with client(w) as c:
        assert c.get("/api/report-extractions/review", params={"batch_id": str(other_batch)}).status_code == 404


# ── CLI ─────────────────────────────────────────────────────────────────────────────────────
def run_cli(monkeypatch, *argv):
    import sys

    from tret import cli

    monkeypatch.setattr(sys, "argv", ["tret", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def test_cli_claim_density_and_review_list(on, monkeypatch, capsys):
    import json

    src_id, _ = setup_two(on)
    w = on.world
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: w.factory)

    assert run_cli(monkeypatch, "disclosure", "claim-density", "--from-batch", str(src_id)) == 0
    assert "2 module(s), 0 updated, 2 already current, 0 skipped" in capsys.readouterr().out
    assert run_cli(monkeypatch, "disclosure", "claim-density", "--from-batch", str(src_id), "--force", "--json") == 0
    assert json.loads(capsys.readouterr().out)["updated"] == 2
    assert run_cli(monkeypatch, "disclosure", "claim-density", "--from-batch", "nope") == 2
    assert run_cli(monkeypatch, "disclosure", "claim-density", "--from-batch", str(uuid.uuid4())) == 1

    async def go(db):
        (await mod_of(db, await ext_of(db, "alpha"))).truncated = True

    mutate(w, go)
    capsys.readouterr()
    assert run_cli(monkeypatch, "disclosure", "review-list", "--from-batch", str(src_id)) == 0
    out = capsys.readouterr().out
    assert "module_truncated" in out and "Alpha" in out and "1 item(s) to review" in out
    assert run_cli(monkeypatch, "disclosure", "review-list", "--from-batch", str(src_id), "--kind", "extraction_failed") == 0
    assert "0 item(s)" in capsys.readouterr().out
    assert run_cli(monkeypatch, "disclosure", "review-list", "--from-batch", str(src_id), "--kind", "nope") == 2
    assert run_cli(monkeypatch, "disclosure", "review-list", "--from-batch", str(src_id), "--json") == 0
    assert json.loads(capsys.readouterr().out)[0]["kind"] == "module_truncated"
    assert run_cli(monkeypatch, "disclosure", "review-list", "--from-batch", str(uuid.uuid4())) == 1


# ── review fixes: stale decisions, company-level NACE items, concurrency, paging ───────────
def test_failed_and_partial_decisions_do_not_hide_each_other(on):
    setup_two(on)
    w = on.world

    async def partial(db):
        a = await ext_of(db, "alpha")
        a.status, a.issues = "partial", [{"kind": "window_failed", "window": 1, "detail": "x"}]
        return a.id

    ext_id = fetch(w, lambda db: _commit(db, partial))
    decide(w, ext_id, "status:partial", "dismissed")
    assert "extraction_partial" not in kinds_of(items(w))

    async def failed(db):
        (await db.get(ReportExtraction, ext_id)).status = "failed"

    mutate(w, failed)
    assert kinds_of(items(w)) == ["extraction_failed"]  # the old decision was about the partial state


def test_a_replaced_extraction_loses_its_decisions(on):
    from tret.evaluation import extract as ex
    from tret.evaluation.modules import load_module_set

    setup_two(on)
    w = on.world

    async def prep(db):
        a = await ext_of(db, "alpha")
        (await mod_of(db, a)).truncated = True
        return a.id

    ext_id = fetch(w, lambda db: _commit(db, prep))
    decide(w, ext_id, f"module:{PHYS}:truncated", "dismissed")
    assert fetch(w, lambda db: _count(db)) == 1

    async def replace(db):
        from tret.db.models import DisclosureReport

        e = await db.get(ReportExtraction, ext_id)
        report = await db.get(DisclosureReport, e.disclosure_report_id)
        ms = load_module_set()
        mr = ex.ModuleResult(PHYS, True, [], "Physical risks: floods.", 3, 0.9, True, "n")
        res = ex.ExtractionResult(ex.STATUS_EXTRACTED, e.metadata_json, {PHYS: mr}, 1, [])
        row, stored = await ex.store_result(
            db, report, res, ms, workspace_id=w.ws.id, batch_target_id=e.batch_target_id, run_id=None, models=["m"])
        assert stored and row.id == ext_id
        await db.commit()

    fetch(w, replace)
    assert fetch(w, lambda db: _count(db)) == 0
    assert "module_truncated" in kinds_of(items(w))  # the new result gets a fresh review


def test_density_failure_never_blocks_saving_an_extraction(on, monkeypatch, caplog):
    def boom(text):
        raise RuntimeError("regex exploded")

    monkeypatch.setattr(density, "claim_density", boom)
    setup_two(on)

    async def read(db):
        m = await mod_of(db, await ext_of(db, "alpha"))
        return m.present, m.claim_count, m.density_version

    assert fetch(on.world, read) == (True, None, None)
    assert "claim density failed" in caplog.text


def test_nace_items_are_per_company_on_the_latest_extraction(on):
    from tret.evaluation import extract as ex
    from tret.evaluation.modules import load_module_set

    setup_two(on)
    w = on.world

    async def lower(db):
        for r in (await db.execute(select(CompanyClassification))).scalars():
            r.confidence = 0.3  # the classification's confidence, not any one proposal's

    mutate(w, lower)
    first = items(w)
    assert kinds_of(first, "Alpha") == ["nace_low_confidence"]
    old_id = next(i.extraction_id for i in first if i.company == "Alpha")

    # a second, newer extraction of the same company takes the NACE items over; the old one has none
    async def again(db):
        from tret.db.models import DisclosureReport

        e = await db.get(ReportExtraction, old_id)
        report = await db.get(DisclosureReport, e.disclosure_report_id)
        ms = load_module_set()
        res = ex.ExtractionResult(ex.STATUS_EXTRACTED, {"nace_proposal": None}, {}, 1, [])
        new = ReportExtraction(
            workspace_id=w.ws.id, project_id=report.project_id, disclosure_report_id=report.id,
            batch_target_id=e.batch_target_id, extractor_version="2", module_set=ms.ident, status="extracted",
            metadata_json=res.metadata, windows=1, issues=[], models=[])
        db.add(new)
        await db.flush()
        return new.id

    new_id = fetch(w, lambda db: _commit(db, again))
    its = [i for i in items(w) if i.company == "Alpha"]
    assert [(i.kind, i.extraction_id) for i in its] == [("nace_low_confidence", new_id)]
    # decisions validate against the carrier only
    with pytest.raises(review.ReviewNotFound):
        decide(w, old_id, "nace:low_confidence")
    assert decide(w, new_id, "nace:low_confidence", "fixed")["decision"] == "fixed"
    assert kinds_of(items(w), "Alpha") == []

    # a failed newer extraction does not take them over
    async def fail(db):
        (await db.get(ReportExtraction, new_id)).status = "failed"

    mutate(w, fail)
    its = [i for i in items(w, include_resolved=True) if i.company == "Alpha"]
    assert {i.extraction_id for i in its if i.kind == "nace_low_confidence"} == {old_id}


def test_nace_low_confidence_uses_the_effective_rows_confidence(on):
    setup_two(on)
    w = on.world

    async def go(db):  # the proposal in this extraction says 0.3, but the stored row (what counts) is 0.9
        for slug in ("alpha", "beta"):
            e = await ext_of(db, slug)
            e.metadata_json = {**e.metadata_json, "nace_proposal": {**e.metadata_json["nace_proposal"], "confidence": 0.3}}
        for r in (await db.execute(select(CompanyClassification))).scalars():
            r.confidence = 0.9

    mutate(w, go)
    assert items(w) == []


def test_concurrent_first_decisions_both_succeed(on):
    setup_two(on)
    w = on.world

    async def prep(db):
        a = await ext_of(db, "alpha")
        (await mod_of(db, a)).truncated = True
        return a.id

    ext_id = fetch(w, lambda db: _commit(db, prep))
    key = f"module:{PHYS}:truncated"

    async def two():
        async def one(who):
            async with w.factory() as db:
                out = await review.set_decision(db, workspace_id=w.ws.id, extraction_id=ext_id, item_key=key,
                                                decision="dismissed", note=who, reviewed_by=who)
                await db.commit()
                return out

        return await asyncio.gather(one("a"), one("b"))

    asyncio.run(two())
    assert fetch(w, lambda db: _count(db)) == 1


def test_paging_and_chunked_id_lists(on, monkeypatch):
    setup_two(on)
    w = on.world

    async def go(db):
        for slug in ("alpha", "beta"):
            (await mod_of(db, await ext_of(db, slug))).truncated = True

    mutate(w, go)
    monkeypatch.setattr(review, "CHUNK", 1)  # every IN (...) list is cut into single ids
    async def page(db):
        return await review.review_page(db, workspace_id=w.ws.id, limit=1, offset=0), await review.review_page(
            db, workspace_id=w.ws.id, limit=1, offset=1)

    (p1, t1), (p2, t2) = fetch(w, page)
    assert t1 == t2 == 2 and len(p1) == len(p2) == 1 and p1[0].company == "Alpha" and p2[0].company == "Beta"
    assert len(items(w, limit=1)) == 1 and len(items(w, offset=1)) == 1 and len(items(w)) == 2
