"""Approval purpose: only a 'review' is quality evidence about a run's model.

Covers the scoring rule (`_finding_counts`), the `tret approvals mark-packaging`
service/CLI, and the evaluated blend, over the real SQLite world of
`test_collections_api.py`.
"""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tret import cli
from tret.db.models import Approval, Batch, BatchTarget, Finding, Run, RunOutcome
from tret.router_llm.outcomes import evaluated_quality
from tret.services import outcomes as outcomes_service
from tret.services.outcomes import _finding_counts, build_outcomes, mark_packaging_approvals, record_outcome

T0 = datetime(2026, 11, 1, tzinfo=timezone.utc)


async def _run(db, world, model="test/model-a") -> Run:
    run = Run(
        id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
        task_type="freeform", task_input={}, document_ids=[], status="completed",
        model_used=model, provider_used="test", iterations=2, messages=[],
    )
    db.add(run)
    await db.flush()
    return run


async def _finding(db, run, *, slug="package_map", status="draft", action=None,
                   purpose="review", at=0) -> Finding:
    f = Finding(
        id=uuid.uuid4(), run_id=run.id, project_id=run.project_id, schema_slug=slug,
        subject={}, payload={}, provenance={}, status=status,
    )
    db.add(f)
    await db.flush()
    if action:
        db.add(Approval(
            id=uuid.uuid4(), finding_id=f.id, action=action, approver_id=run_user(db),
            purpose=purpose, created_at=T0 + timedelta(minutes=at),
        ))
        await db.flush()
    return f


_USER: dict[str, uuid.UUID] = {}


def run_user(db) -> uuid.UUID:
    return _USER["id"]


@pytest.fixture(autouse=True)
def _approver(world):
    _USER["id"] = world.user.id


async def _batch(db, world, runs, *, slug="b1", history=(), batch_id=None) -> Batch:
    b = Batch(
        id=batch_id or uuid.uuid4(), workspace_id=world.ws.id, project_id=world.project.id,
        name=slug, run_slug=slug, status="completed", settings={}, warnings=[],
    )
    db.add(b)
    await db.flush()
    for i, run in enumerate(runs):
        db.add(BatchTarget(
            id=uuid.uuid4(), collection_id=b.id, position=i, slug=f"t{i}", company=f"C{i}",
            market="US", status="completed", run_id=run.id,
            history=[{"attempt": 0, "run_id": str(h)} for h in history] if i == 0 else [],
        ))
    await db.commit()
    return b


# ── scoring: only reviews count ──────────────────────────────────────────────
def test_a_packaging_approval_is_not_counted_as_an_approval(client, world):
    async def go(db):
        run = await _run(db, world)
        await _finding(db, run, status="approved", action="approve", purpose="packaging")
        await _finding(db, run, status="approved", action="approve", purpose="review", at=1)
        await _finding(db, run, slug="x", status="rejected", action="reject")
        await _finding(db, run, slug="y", status="draft")
        legacy = await _finding(db, run, slug="z", status="approved")  # no approvals row
        assert legacy.status == "approved"
        await db.commit()
        return await _finding_counts(db, run.id)

    assert run_db(client, world, go) == {
        "approved": 2, "approved_packaging": 1, "rejected": 1, "draft": 1,
    }


def test_the_most_recent_approval_decides(client, world):
    async def go(db):
        run = await _run(db, world)
        f = await _finding(db, run, status="approved", action="approve", purpose="review", at=0)
        db.add(Approval(
            id=uuid.uuid4(), finding_id=f.id, action="approve", approver_id=run_user(db),
            purpose="packaging", created_at=T0 + timedelta(minutes=5),
        ))
        await db.commit()
        return await _finding_counts(db, run.id)

    assert run_db(client, world, go) == {"approved_packaging": 1}


def test_a_run_with_only_a_packaging_approval_scores_like_an_undecided_run(client, world):
    async def go(db):
        packaged = await _run(db, world)
        await _finding(db, packaged, status="approved", action="approve", purpose="packaging")
        undecided = await _run(db, world)
        await _finding(db, undecided, status="draft")
        reviewed = await _run(db, world)
        await _finding(db, reviewed, status="approved", action="approve", purpose="review")
        await db.commit()
        return [(await build_outcomes(db, r))[0] for r in (packaged, undecided, reviewed)]

    packaged, undecided, reviewed = run_db(client, world, go)
    assert "human" not in packaged.components
    assert packaged.findings_approved == 0
    assert packaged.quality_score == undecided.quality_score
    assert reviewed.findings_approved == 1 and "human" in reviewed.components
    assert reviewed.quality_score != packaged.quality_score


def test_the_human_component_reflects_exactly_the_review_approvals(client, world):
    async def go(db):
        mixed = await _run(db, world)
        await _finding(db, mixed, status="approved", action="approve", purpose="review")
        await _finding(db, mixed, status="approved", action="approve", purpose="packaging")
        one = await _run(db, world)
        await _finding(db, one, status="approved", action="approve", purpose="review")
        await db.commit()
        return (await build_outcomes(db, mixed))[0], (await build_outcomes(db, one))[0]

    mixed, one = run_db(client, world, go)
    assert mixed.findings_approved == 1
    assert mixed.components["human"] == one.components["human"]
    assert mixed.quality_score == one.quality_score


def test_a_rejection_always_counts(client, world):
    async def go(db):
        run = await _run(db, world)
        await _finding(db, run, status="rejected", action="reject")
        await db.commit()
        return (await build_outcomes(db, run))[0]

    row = run_db(client, world, go)
    assert row.findings_rejected == 1 and "human" in row.components


def test_the_evaluated_blend_drops_the_human_term_for_packaging_approvals(client, world, monkeypatch):
    async def fake_term(db, run):
        return {"quality": 1.0, "source": "judge"}

    async def fake_family(db, run):
        return "fam@ws"

    monkeypatch.setattr(outcomes_service, "_evaluator_term", fake_term)
    monkeypatch.setattr(outcomes_service, "task_family_for_run", fake_family)

    async def go(db):
        packaged = await _run(db, world)
        await _finding(db, packaged, status="approved", action="approve", purpose="packaging")
        reviewed = await _run(db, world)
        await _finding(db, reviewed, status="approved", action="approve", purpose="review")
        await db.commit()
        return [(await build_outcomes(db, r))[-1] for r in (packaged, reviewed)]

    packaged, reviewed = run_db(client, world, go)
    auto = float(packaged.components["automatic_score"])
    assert float(packaged.evaluated_quality_score) == pytest.approx(
        evaluated_quality(auto=auto, human_rate=None, evaluator=1.0), abs=1e-4
    )
    assert float(reviewed.evaluated_quality_score) == pytest.approx(
        evaluated_quality(
            auto=auto, human_rate=reviewed.components["human"]["smoothed_rate"], evaluator=1.0
        ),
        abs=1e-4,
    )
    assert float(packaged.evaluated_quality_score) != float(reviewed.evaluated_quality_score)


# ── mark-packaging ───────────────────────────────────────────────────────────
async def _seed_world(db, world):
    """Two batches. b1: run A (model-a, an approved map + an approved non-map +
    a rejected map) and run B (model-b, an approved map; B is an earlier attempt
    held in `history`). b2: run C (model-a, an approved map)."""
    a, b_run, c = await _run(db, world, "test/model-a"), await _run(db, world, "test/model-b"), await _run(db, world, "test/model-a")
    fa = await _finding(db, a, status="approved", action="approve")
    f_other = await _finding(db, a, slug="draft_section", status="approved", action="approve")
    f_rej = await _finding(db, a, status="rejected", action="reject")
    fb = await _finding(db, b_run, status="approved", action="approve")
    fc = await _finding(db, c, status="approved", action="approve")
    await db.commit()
    b1 = await _batch(db, world, [a], slug="b1", history=[b_run.id])
    b2 = await _batch(db, world, [c], slug="b2")
    for r in (a, b_run, c):
        await record_outcome(db, r, commit=True)
    # Plain ids only: a rollback inside a re-score expires every ORM object the
    # session holds, and reading one afterwards would lazy-load outside a greenlet.
    return {"a": a.id, "b": b_run.id, "c": c.id, "fa": fa.id, "f_other": f_other.id,
            "f_rej": f_rej.id, "fb": fb.id, "fc": fc.id, "b1": b1.id, "b2": b2.id}


async def _purposes(db):
    rows = (await db.execute(select(Approval.finding_id, Approval.purpose))).all()
    return dict(rows)


async def _snapshot(db):
    ap = await _purposes(db)
    oc = {r.run_id: (r.quality_score, r.findings_approved)
          for r in (await db.execute(select(RunOutcome))).scalars()}
    return ap, oc


def test_dry_run_writes_nothing(client, world, monkeypatch):
    calls = []
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: calls.append(1))

    async def go(db):
        s = await _seed_world(db, world)
        before = await _snapshot(db)
        res = await mark_packaging_approvals(db, [str(s["b1"])], dry_run=True)
        return before, await _snapshot(db), res

    before, after, res = run_db(client, world, go)
    assert before == after and not calls
    assert res["dry_run"] is True
    assert res["batches"][0]["approvals_changed"] == 2 and res["batches"][0]["runs_rescored"] == 2
    assert all(m["mean_after"] is None for m in res["models"])


def test_a_real_run_flips_only_the_named_batches_approved_page_maps(client, world, monkeypatch):
    calls = []
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: calls.append(1))

    async def go(db):
        s = await _seed_world(db, world)
        before = (await _snapshot(db))[1]
        res = await mark_packaging_approvals(db, [str(s["b1"])], dry_run=False)
        res2 = await mark_packaging_approvals(db, [str(s["b1"])], dry_run=False)
        after = await _snapshot(db)
        return s, before, after, res, res2

    s, before, (ap, oc), res, res2 = run_db(client, world, go)
    assert ap[s["fa"]] == "packaging" and ap[s["fb"]] == "packaging"  # incl. the earlier attempt's run
    assert ap[s["fc"]] == "review"  # another batch
    assert ap[s["f_rej"]] == "review" and ap[s["f_other"]] == "review"
    assert res["batches"][0]["approvals_changed"] == 2 and res["batches"][0]["runs_rescored"] == 2
    # the runs lost their human component; the untouched run did not
    assert oc[s["b"]][1] == 0 and oc[s["c"]] == before[s["c"]]
    assert before[s["a"]][1] == 2 and oc[s["a"]][1] == 1  # the non-map approval still counts
    assert res2["batches"][0]["approvals_changed"] == 0  # nothing left to flip...
    assert res2["batches"][0]["runs_rescored"] == 2  # ...but it re-scores again
    assert calls == [1, 1]  # priors invalidated once per invocation
    models = {m["model_id"]: m for m in res["models"]}
    assert set(models) == {"test/model-a", "test/model-b"}
    assert models["test/model-b"]["rows"] == 1
    # one lone approval smoothed to 2/3 sits below a clean run's base, so dropping it moves the mean
    assert models["test/model-b"]["mean_after"] != models["test/model-b"]["mean_before"]


def test_an_unknown_batch_is_reported_without_failing_the_others(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        s = await _seed_world(db, world)
        return await mark_packaging_approvals(
            db, [str(uuid.uuid4()), "not-a-uuid", str(s["b2"])], dry_run=False
        )

    res = run_db(client, world, go)
    assert [b["found"] for b in res["batches"]] == [False, False, True]
    assert res["batches"][2]["approvals_changed"] == 1


def test_the_cli_command_prints_the_summary(client, world, monkeypatch, capsys):
    import sys

    from tret.db import engine as engine_mod

    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def seed(db):
        return str((await _seed_world(db, world))["b1"])

    bid = run_db(client, world, seed)
    monkeypatch.setattr(engine_mod, "get_session_factory", lambda: world.factory)
    monkeypatch.setattr(
        sys, "argv",
        ["tret", "approvals", "mark-packaging", "--batch", bid, "--batch", str(uuid.uuid4()), "--dry-run"],
    )
    cli.main()
    out = capsys.readouterr().out
    assert "would change 2 approvals, would re-score 2 runs" in out and "not found" in out
    assert "dry run" in out and "quality after" not in out and "quality before" in out
    assert "eval before" in out and "eval after" not in out
    print(out)


# ── the real handler on a real session ───────────────────────────────────────
@pytest.fixture
def session_client(world, monkeypatch):
    """The findings router with `User` and `Workspace` loaded in the SAME session
    the handler uses, as production does, so a rollback expires them too."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from tret.api import findings as findings_api
    from tret.api.auth import current_user
    from tret.api.workspace import WorkspaceContext, require_workspace_approver
    from tret.db.engine import get_db
    from tret.db.models import User, Workspace

    monkeypatch.setattr(findings_api, "_start_packaging", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(findings_api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user(db=Depends(get_db)):
        return await db.get(User, world.user.id)

    async def _ctx(db=Depends(get_db)):
        return WorkspaceContext(await db.get(Workspace, world.ws.id), "approver")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[require_workspace_approver] = _ctx
    with TestClient(app) as c:
        yield c


def _statuses(client, world, ids):
    async def go(db):
        out = {}
        for fid in ids:
            f = await db.get(Finding, fid)
            ap = (await db.execute(select(Approval.purpose).where(Approval.finding_id == fid))).scalars().all()
            out[fid] = (f.status, list(ap))
        return out

    return run_db(client, world, go)


def test_a_refused_item_in_the_middle_does_not_sink_the_rest_of_the_batch(session_client, world):
    async def seed(db):
        run = await _run(db, world)
        m1 = await _finding(db, run)
        other = await _finding(db, run, slug="draft_section")
        m2 = await _finding(db, run)
        await db.commit()
        return m1.id, other.id, m2.id

    m1, other, m2 = run_db(session_client, world, seed)
    r = session_client.post(
        "/api/findings/decide-batch",
        json={"finding_ids": [str(m1), str(other), str(m2)], "action": "approve", "purpose": "packaging"},
    )
    assert r.status_code == 200, r.text
    res = {x["id"]: x for x in r.json()["results"]}
    assert res[str(m1)]["ok"] is True and res[str(m2)]["ok"] is True, res
    assert res[str(other)]["ok"] is False and "package_map" in res[str(other)]["error"]
    st = _statuses(session_client, world, [m1, other, m2])
    assert st[m1] == ("approved", ["packaging"]) and st[m2] == ("approved", ["packaging"])
    assert st[other] == ("draft", [])


def test_an_already_decided_item_does_not_sink_the_next_one(session_client, world):
    async def seed(db):
        run = await _run(db, world)
        done = await _finding(db, run, slug="draft_section", status="approved", action="approve")
        draft = await _finding(db, run, slug="draft_section")
        await db.commit()
        return done.id, draft.id

    done, draft = run_db(session_client, world, seed)
    r = session_client.post(
        "/api/findings/decide-batch", json={"finding_ids": [str(done), str(draft)], "action": "approve"}
    )
    assert r.status_code == 200, r.text
    res = {x["id"]: x for x in r.json()["results"]}
    assert res[str(done)]["ok"] is False and "already 'approved'" in res[str(done)]["error"]
    assert res[str(draft)]["ok"] is True, res
    assert _statuses(session_client, world, [draft])[draft] == ("approved", ["review"])


# ── optional purpose, and the batch-level contradiction ─────────────────────
def test_a_null_purpose_means_review(session_client, world):
    async def seed(db):
        run = await _run(db, world)
        f = await _finding(db, run, slug="draft_section")
        g = await _finding(db, run, slug="draft_section")
        await db.commit()
        return f.id, g.id

    f, g = run_db(session_client, world, seed)
    r = session_client.post(f"/api/findings/{f}/approval", json={"action": "approve", "purpose": None})
    assert r.status_code == 200 and r.json()["purpose"] == "review"
    r = session_client.post(
        "/api/findings/decide-batch", json={"finding_ids": [str(g)], "action": "approve", "purpose": None}
    )
    assert r.status_code == 200 and r.json()["results"][0]["ok"] is True
    st = _statuses(session_client, world, [f, g])
    assert st[f] == ("approved", ["review"]) and st[g] == ("approved", ["review"])


def test_reject_with_packaging_is_refused_up_front(session_client, world):
    async def seed(db):
        run = await _run(db, world)
        f = await _finding(db, run)
        await db.commit()
        return f.id

    f = run_db(session_client, world, seed)
    r = session_client.post(
        "/api/findings/decide-batch", json={"finding_ids": [str(f)], "action": "reject", "purpose": "packaging"}
    )
    assert r.status_code == 422 and "rejection" in r.text
    r = session_client.post(f"/api/findings/{f}/approval", json={"action": "reject", "purpose": "packaging"})
    assert r.status_code == 422
    assert _statuses(session_client, world, [f])[f] == ("draft", [])


# ── end to end through the API ───────────────────────────────────────────────
def _decide_and_read(client, world, purpose, monkeypatch):
    from tret.api import findings as findings_api

    # the shared world stubs the re-score hook out; this test is about it
    monkeypatch.setattr(findings_api, "record_outcome_for_finding", outcomes_service.record_outcome_for_finding)
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def seed(db):
        run = await _run(db, world)
        f = await _finding(db, run)
        await db.commit()
        row = (await record_outcome(db, run, commit=True))[0]
        return run.id, f.id, float(row.quality_score)

    run_id, fid, before = run_db(client, world, seed)
    r = client.post(f"/api/findings/{fid}/approval", json={"action": "approve", "purpose": purpose})
    assert r.status_code == 200, r.text

    async def read(db):
        return await db.get(RunOutcome, (run_id, 0))

    return before, run_db(client, world, read)


def test_end_to_end_a_packaging_approval_leaves_the_stored_outcome_unchanged(client, world, monkeypatch):
    before, row = _decide_and_read(client, world, "packaging", monkeypatch)
    assert row.findings_approved == 0 and "human" not in row.components
    assert float(row.quality_score) == before


def test_end_to_end_a_review_approval_scores_the_stored_outcome(client, world, monkeypatch):
    before, row = _decide_and_read(client, world, "review", monkeypatch)
    assert row.findings_approved == 1 and "human" in row.components
    assert float(row.quality_score) != before


# ── mark-packaging: repair and failures ──────────────────────────────────────
@pytest.mark.parametrize("bad_is", ["first", "last"])
def test_a_failed_rescore_is_reported_and_a_rerun_repairs_it(client, world, monkeypatch, bad_is):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)
    real = outcomes_service.record_outcome

    async def go(db):
        s = await _seed_world(db, world)
        a, b = s["a"], s["b"]
        ordered = sorted([a, b], key=str)  # the order the command processes runs in
        bad = ordered[0] if bad_is == "first" else ordered[-1]
        good = b if bad == a else a
        bid = str(s["b1"])
        state = {"fail": True}

        async def flaky(db_, run, **kw):
            if run.id == bad and state["fail"]:
                state["fail"] = False
                return []  # what a swallowed error looks like
            return await real(db_, run, **kw)

        monkeypatch.setattr(outcomes_service, "record_outcome", flaky)
        first = await mark_packaging_approvals(db, [bid], dry_run=False)
        mid = {r.run_id: r.findings_approved for r in (await db.execute(select(RunOutcome))).scalars()}
        second = await mark_packaging_approvals(db, [bid], dry_run=False)
        end = {r.run_id: r.findings_approved for r in (await db.execute(select(RunOutcome))).scalars()}
        return {"a": a, "b": b}, bad, good, first, mid, second, end

    ids, bad, good, first, mid, second, end = run_db(client, world, go)
    b1 = first["batches"][0]
    assert b1["approvals_changed"] == 2 and b1["runs_failed"] == [str(bad)]
    assert b1["runs_rescored"] == 1  # the other run was still re-scored
    # run a held 2 approved findings (a map and a non-map), run b held 1 (a map)
    inflated, repaired = ({"a": 2, "b": 1}, {"a": 1, "b": 0})
    key = {s_id: k for k, s_id in ids.items()}
    assert mid[bad] == inflated[key[bad]]  # the failed run keeps its inflated count
    assert mid[good] == repaired[key[good]]
    assert second["batches"][0]["approvals_changed"] == 0  # nothing left to flip...
    assert second["batches"][0]["runs_failed"] == [] and second["batches"][0]["runs_rescored"] == 2
    assert end[bad] == repaired[key[bad]] and end[good] == repaired[key[good]]


def test_a_stored_count_that_disagrees_with_the_review_count_is_a_failure(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        s = await _seed_world(db, world)
        c_id = s["c"]

        async def liar(db_, run, **kw):  # writes rows, but leaves the old counts
            return [object()]

        monkeypatch.setattr(outcomes_service, "record_outcome", liar)
        return c_id, await mark_packaging_approvals(db, [str(s["b2"])], dry_run=False)

    c_id, res = run_db(client, world, go)
    assert res["batches"][0]["runs_failed"] == [str(c_id)]


def test_a_run_without_outcome_rows_gets_none(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        s = await _seed_world(db, world)
        await db.delete(await db.get(RunOutcome, (s["c"], 0)))
        await db.commit()
        res = await mark_packaging_approvals(db, [str(s["b2"])], dry_run=False)
        rows = (await db.execute(select(RunOutcome).where(RunOutcome.run_id == s["c"]))).scalars().all()
        return res, rows, (await _purposes(db))[s["fc"]]

    res, rows, purpose = run_db(client, world, go)
    assert rows == [] and res["batches"][0]["runs_rescored"] == 0 and res["batches"][0]["runs_failed"] == []
    assert purpose == "packaging"  # the approval is still re-marked


def test_the_cli_exits_non_zero_when_a_rescore_failed(client, world, monkeypatch, capsys):
    import sys

    from tret.db import engine as engine_mod

    async def seed(db):
        return str((await _seed_world(db, world))["b2"])

    bid = run_db(client, world, seed)

    async def fake(db, ids, *, dry_run):
        return {"dry_run": dry_run, "models": [], "batches": [
            {"batch_id": bid, "found": True, "approvals_changed": 1, "runs_rescored": 0, "runs_failed": ["r1"]}
        ]}

    monkeypatch.setattr(outcomes_service, "mark_packaging_approvals", fake)
    monkeypatch.setattr(engine_mod, "get_session_factory", lambda: world.factory)
    monkeypatch.setattr(sys, "argv", ["tret", "approvals", "mark-packaging", "--batch", bid])
    with pytest.raises(SystemExit) as e:
        cli.main()
    assert e.value.code == 1 and "r1" in capsys.readouterr().err


# ── a run with a task family, a live evaluation and two segments ─────────────
JUDGE_QUALITY = 80  # the evaluation's `quality`, on the 0-100 scale


async def _seed_evaluated_run(db, world):
    """A batch target run in a task family, with a scored, current, non-stale
    evaluation, a two-segment model timeline and one approved page map (a review).
    Outcomes are recorded before returning. Returns plain ids."""
    from tret.db.models import BatchEvaluation, TargetEvaluation

    batch_id = uuid.uuid4()
    run = await _run(db, world, "test/model-b")
    run.model_timeline = [
        {"model": "test/model-a", "provider": "test", "from_iteration": 1, "to_iteration": 1,
         "cost_usd": 0.01},
        {"model": "test/model-b", "provider": "test", "from_iteration": 2, "to_iteration": 2,
         "reason": "capability_stall", "cost_usd": 0.02},
    ]
    run.task_input = {"_collection_id": str(batch_id)}
    f = await _finding(db, run, status="approved", action="approve")
    await db.commit()
    batch = await _batch(db, world, [run], slug="fam", batch_id=batch_id)
    target = (await db.execute(select(BatchTarget).where(BatchTarget.collection_id == batch.id))).scalars().one()
    ev = BatchEvaluation(
        id=uuid.uuid4(), collection_id=batch.id, workspace_id=world.ws.id, family_key="fam",
        rubric_key="r", rubric_version="1", rubric={}, judge_model="test/judge", status="completed",
    )
    db.add(ev)
    await db.flush()
    db.add(TargetEvaluation(
        id=uuid.uuid4(), evaluation_id=ev.id, collection_id=batch.id, workspace_id=world.ws.id,
        target_id=target.id, row_key="t0", rubric_key="r", family_key="fam", attempt=0,
        evaluated_run_id=run.id, status="scored", quality=JUDGE_QUALITY,
    ))
    await db.commit()
    ids = {"run": run.id, "finding": f.id, "batch": batch.id}
    await record_outcome(db, await db.get(Run, ids["run"]), commit=True)
    return ids


async def _rows(db, run_id):
    return {
        r.segment_index: r
        for r in (await db.execute(select(RunOutcome).where(RunOutcome.run_id == run_id))).scalars()
    }


def _plain(row):
    return {c.name: getattr(row, c.name) for c in row.__table__.columns}


async def _eval_snapshot(db):
    from tret.db.models import BatchEvaluation, TargetEvaluation

    out = []
    for model in (BatchEvaluation, TargetEvaluation):
        for r in (await db.execute(select(model).order_by(model.id))).scalars():
            out.append(_plain(r))
    return out


def test_marking_an_evaluated_two_segment_run_drops_only_the_human_term(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        ids = await _seed_evaluated_run(db, world)
        before = {i: _plain(r) for i, r in (await _rows(db, ids["run"])).items()}
        evals_before = await _eval_snapshot(db)
        res = await mark_packaging_approvals(db, [str(ids["batch"])], dry_run=False)
        after = {i: _plain(r) for i, r in (await _rows(db, ids["run"])).items()}
        return before, after, evals_before, await _eval_snapshot(db), res

    before, after, evals_before, evals_after, res = run_db(client, world, go)
    assert res["batches"][0]["approvals_changed"] == 1 and res["batches"][0]["runs_failed"] == []
    assert sorted(before) == sorted(after) == [0, 1]  # no outcome row added or removed
    # the setup really was evaluated, in a family, with the human term present
    assert before[1]["evaluated_quality_score"] is not None and before[0]["evaluated_quality_score"] is None
    assert before[1]["task_family"] and before[1]["components"]["human"]
    assert before[1]["findings_approved"] == 1 and before[0]["findings_approved"] == 0
    # after: the last segment lost exactly the human term, nothing else about the family changed
    assert after[0]["findings_approved"] == 0 and after[1]["findings_approved"] == 0
    assert "human" not in after[1]["components"]
    assert after[0]["task_family"] == before[0]["task_family"]
    assert after[1]["task_family"] == before[1]["task_family"]
    expected = evaluated_quality(
        auto=float(after[1]["components"]["automatic_score"]),
        human_rate=None,
        evaluator=JUDGE_QUALITY / 100,
    )
    assert float(after[1]["evaluated_quality_score"]) == pytest.approx(expected, abs=1e-4)
    assert float(after[1]["evaluated_quality_score"]) != float(before[1]["evaluated_quality_score"])
    assert after[0]["evaluated_quality_score"] is None
    assert evals_after == evals_before  # the evaluation rows are untouched
    m = {x["model_id"]: x for x in res["models"]}["test/model-b"]
    assert m["evaluated_rows"] == 1
    assert m["evaluated_mean_before"] == pytest.approx(float(before[1]["evaluated_quality_score"]))
    assert m["evaluated_mean_after"] == pytest.approx(float(after[1]["evaluated_quality_score"]))


def test_a_run_whose_approvals_are_all_reviews_is_left_exactly_as_it_was(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        ids = await _seed_evaluated_run(db, world)
        # the approved finding is not a page map, so there is nothing to flip
        f = await db.get(Finding, ids["finding"])
        f.schema_slug = "draft_section"
        await db.commit()
        run = await db.get(Run, ids["run"])
        await record_outcome(db, run, commit=True)
        before = {i: _plain(r) for i, r in (await _rows(db, ids["run"])).items()}
        res = await mark_packaging_approvals(db, [str(ids["batch"])], dry_run=False)
        return ids, before, {i: _plain(r) for i, r in (await _rows(db, ids["run"])).items()}, res

    _, before, after, res = run_db(client, world, go)
    assert before[1]["evaluated_quality_score"] is not None and before[1]["findings_approved"] == 1
    assert res["batches"][0]["approvals_changed"] == 0 and res["batches"][0]["runs_rescored"] == 0
    assert after == before  # quality_score and evaluated_quality_score included


def test_the_same_batch_in_two_spellings_is_processed_once(client, world, monkeypatch):
    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def go(db):
        s = await _seed_world(db, world)
        canonical = str(s["b1"])
        return await mark_packaging_approvals(
            db, [canonical.upper(), canonical.replace("-", ""), canonical], dry_run=False
        ), canonical

    res, canonical = run_db(client, world, go)
    assert [b["batch_id"] for b in res["batches"]] == [canonical]
    assert res["batches"][0]["approvals_changed"] == 2 and res["batches"][0]["runs_rescored"] == 2


def test_the_cli_real_run_output(client, world, monkeypatch, capsys):
    import sys

    from tret.db import engine as engine_mod

    monkeypatch.setattr(outcomes_service, "_invalidate_priors", lambda: None)

    async def seed(db):
        return str((await _seed_world(db, world))["b1"])

    bid = run_db(client, world, seed)
    monkeypatch.setattr(engine_mod, "get_session_factory", lambda: world.factory)
    out = []
    for _ in range(2):
        monkeypatch.setattr(sys, "argv", ["tret", "approvals", "mark-packaging", "--batch", bid])
        cli.main()
        out.append(capsys.readouterr().out)
    print(out[0], out[1], sep="\n--- second run ---\n")
    assert f"{bid}: 2 approvals changed, 2 runs re-scored" in out[0]
    assert "quality after" in out[0] and "eval after" in out[0]
    assert out[0].rstrip().endswith("A running server picks up the re-scored record within about 60 seconds (its priors cache lifetime).")
    assert f"{bid}: 0 approvals changed, 2 runs re-scored" in out[1]
