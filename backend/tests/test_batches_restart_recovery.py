"""Boot recovery of batches (`sweep_orphaned_collections`, `launch_auto_resumed`).

A batch that was running when the server restarted resumes on its own; a target
whose run the restart cut off (the orphan sweep's `process_restart` marker) goes
back to `pending` at most twice, archived like a manual retry. A batch a person
paused stays paused, and `batches_auto_resume=false` restores the old pause.
No network: the launch loop is replaced by a recorder.
"""

# ruff: noqa: F811
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from tests.test_collections_api import (  # noqa: F401  (fixtures + helpers shared with the batch API tests)
    by_slug,
    client,
    create,
    detail,
    run_db,
    settle,
    world,
)
from tret.batches import core
from tret.config import get_settings
from tret.db.models import Collection, CollectionTarget, Run
from tret.services import collections as service
from tret.services.reconcile import ORPHAN_ERROR

OLD_NOTE = "The server restarted during this collection; resume to continue."


@pytest.fixture
def launches(monkeypatch):
    """Replace the launch loop: record the batches it is started for."""
    seen: list[uuid.UUID] = []

    async def fake_launch(collection_id):
        seen.append(collection_id)
        return 0

    monkeypatch.setattr(core, "_launch", fake_launch)
    return seen


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _interrupt(db, world, cid, *, error=ORPHAN_ERROR, status="running", batch_status="running",
                     history=None):
    """Put the first target mid-run with a run the orphan sweep (or its own error) failed."""
    c = await db.get(Collection, uuid.UUID(cid))
    c.status = batch_status
    targets = list((await db.execute(select(CollectionTarget).order_by(CollectionTarget.position))).scalars())
    run = Run(id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
              task_type="freeform", task_input={}, document_ids=[], status="failed", error=error)
    db.add(run)
    await db.flush()
    t = targets[0]
    t.status = status
    t.run_id = run.id
    t.attempts = 1 + len(history or [])
    t.history = history or []
    await db.commit()
    return t.slug


def _restart_attempt(n):
    return {"attempt": n, "run_id": None, "status": "failed", "error": ORPHAN_ERROR, "outcome": None,
            "cost_usd": 0.0, "model_used": None, "started_at": None, "finished_at": None}


async def _sweep(db):
    swept = await service.sweep_orphaned_collections(db)
    return swept, await service.launch_auto_resumed()


async def _cut_off_again(db, world, slug, *, error=ORPHAN_ERROR):
    """What a launch plus a restart does to a pending row: a new run, failed by the orphan sweep."""
    t = (await db.execute(select(CollectionTarget).where(CollectionTarget.slug == slug))).scalars().one()
    run = Run(id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
              task_type="freeform", task_input={}, document_ids=[], status="failed", error=error)
    db.add(run)
    await db.flush()
    t.status, t.run_id, t.attempts = "running", run.id, (t.attempts or 0) + 1
    c = await db.get(Collection, t.collection_id)
    c.status = "running"
    await db.commit()


def _reboot(client, world, slug, **kw):
    async def go(db):
        await _cut_off_again(db, world, slug, **kw)
        return await _sweep(db)

    return run_db(client, world, go)


def _set_count(client, world, cid, n):
    async def go(db):
        c = await db.get(Collection, uuid.UUID(cid))
        c.settings = {**(c.settings or {}), "auto_resume_count": n}
        await db.commit()

    run_db(client, world, go)


def _boot(client, world, cid, **kw):
    async def go(db):
        slug = await _interrupt(db, world, cid, **kw)
        return slug, *(await _sweep(db))

    return run_db(client, world, go)


def test_a_running_batch_with_pending_targets_is_auto_resumed(client, world, launches):
    cid = create(client).json()["id"]
    slug, swept, scheduled = _boot(client, world, cid)
    settle(client)
    assert scheduled == 1
    assert launches == [uuid.UUID(cid)]
    d = detail(client, cid)
    assert d["status"] == "running"
    assert d["last_error"] is None
    assert d["last_resumed_at"]
    assert "resumed on its own" in d["last_resume_note"]


def test_a_restart_interrupted_target_goes_back_to_pending_with_its_attempt_archived(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid)
    d = detail(client, cid)
    t = by_slug(d)[slug]
    assert t["status"] == "pending"
    assert t["error"] is None
    assert len(t["history"]) == 1
    assert t["history"][0]["status"] == "failed"
    assert t["history"][0]["error"] == ORPHAN_ERROR


def test_the_cap_leaves_a_target_failed_after_two_restart_retries(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid, history=[_restart_attempt(1), _restart_attempt(2)])
    t = by_slug(detail(client, cid))[slug]
    assert t["status"] == "failed"
    assert "restarts 3 times" in t["error"]
    assert len(t["history"]) == 2  # nothing archived: it was not retried


def test_the_second_restart_retry_is_still_allowed(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid, history=[_restart_attempt(1)])
    assert by_slug(detail(client, cid))[slug]["status"] == "pending"


def test_a_target_that_failed_on_its_own_error_is_not_retried(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid, error="RuntimeError: engine fell over")
    t = by_slug(detail(client, cid))[slug]
    assert t["status"] == "failed"
    assert t["error"] == "RuntimeError: engine fell over"
    assert t["history"] == []


def test_a_batch_a_person_paused_stays_paused(client, world, launches):
    cid = create(client).json()["id"]
    _, swept, scheduled = _boot(client, world, cid, batch_status="paused")
    assert scheduled == 0 and launches == []
    d = detail(client, cid)
    assert d["status"] == "paused"
    assert d["last_resumed_at"] is None
    # and its interrupted target is left for the operator's retry
    assert d["targets"][0]["status"] == "failed"


def test_other_statuses_are_untouched(client, world, launches):
    cid = create(client).json()["id"]
    _, swept, scheduled = _boot(client, world, cid, batch_status="finalized")
    assert scheduled == 0 and launches == []
    assert detail(client, cid)["status"] == "finalized"


def test_with_auto_resume_off_the_old_behaviour_holds(client, world, launches, monkeypatch):
    monkeypatch.setenv("TRET_BATCHES_AUTO_RESUME", "false")
    get_settings.cache_clear()
    cid = create(client).json()["id"]
    slug, swept, scheduled = _boot(client, world, cid)
    assert scheduled == 0 and launches == []
    d = detail(client, cid)
    assert d["status"] == "paused"
    assert d["last_error"] == OLD_NOTE
    assert d["last_resumed_at"] is None
    t = by_slug(d)[slug]
    assert t["status"] == "failed" and t["error"] == ORPHAN_ERROR and t["history"] == []


def test_the_per_row_cap_holds_across_three_real_sweeps(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid)
    for expected in ("pending", "failed"):
        _set_count(client, world, cid, 0)  # isolate the row cap from the batch breaker
        _reboot(client, world, slug)
        assert by_slug(detail(client, cid))[slug]["status"] == expected
    t = by_slug(detail(client, cid))[slug]
    assert "restarts 3 times" in t["error"]
    assert len(t["history"]) == 2


def test_the_breaker_pauses_a_batch_on_its_third_restart_in_a_row(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid)  # 1st: resumed
    _reboot(client, world, slug)  # 2nd: resumed
    d = detail(client, cid)
    assert d["status"] == "running" and d["targets"][0]["status"] == "pending"
    _, scheduled = _reboot(client, world, slug)  # 3rd: breaker
    d = detail(client, cid)
    assert scheduled == 0
    assert d["status"] == "paused"
    assert "repeatedly" in d["last_error"]
    t = by_slug(d)[slug]
    assert t["status"] == "failed" and t["error"] == ORPHAN_ERROR


def test_a_non_restart_settle_resets_the_breaker(client, world, launches):
    cid = create(client).json()["id"]
    slug, *_ = _boot(client, world, cid)
    _reboot(client, world, slug)

    async def own_failure(db):
        other = (await db.execute(select(CollectionTarget).where(CollectionTarget.slug != slug))).scalars().first()
        c = await db.get(Collection, uuid.UUID(cid))
        run = Run(id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id,
                  task_type="freeform", task_input={}, document_ids=[], status="failed",
                  error="RuntimeError: engine fell over")
        db.add(run)
        await db.flush()
        other.status, other.run_id = "running", run.id
        await db.commit()
        await service._resettle_targets(db, [other.id])
        await db.refresh(c)
        return c.settings.get("auto_resume_count")

    assert run_db(client, world, own_failure) == 0
    _, scheduled = _reboot(client, world, slug)
    assert scheduled == 1
    assert detail(client, cid)["status"] == "running"


def test_a_batch_whose_only_row_was_cut_off_comes_back_running_and_launches_it(client, world, launches):
    cid = create(client).json()["id"]

    async def go(db):
        targets = list((await db.execute(select(CollectionTarget))).scalars())
        for t in targets[1:]:
            t.status = "completed"  # every other row is done: the cut-off one is all that is left
        await db.commit()
        slug = await _interrupt(db, world, cid)
        return slug, *(await _sweep(db))

    slug, _, scheduled = run_db(client, world, go)
    settle(client)
    assert scheduled == 1 and launches == [uuid.UUID(cid)]
    d = detail(client, cid)
    assert d["status"] == "running" and by_slug(d)[slug]["status"] == "pending"


def test_auto_resume_does_not_release_held_rows(client, world, launches):
    cid = create(client, first_batch_size=1).json()["id"]

    async def go(db):
        slug = await _interrupt(db, world, cid)
        held = list((await db.execute(select(CollectionTarget).where(CollectionTarget.slug != slug))).scalars())
        for t in held:
            t.status = "held"
        await db.commit()
        return slug, *(await _sweep(db))

    slug, _, scheduled = run_db(client, world, go)
    d = detail(client, cid)
    assert scheduled == 1 and d["status"] == "running"
    assert by_slug(d)[slug]["status"] == "pending"
    assert {t["status"] for t in d["targets"] if t["slug"] != slug} == {"held"}


def test_a_failing_boot_launch_pauses_the_batch_with_a_note(client, world, monkeypatch):
    async def boom(collection_id):
        raise RuntimeError("launch exploded")

    monkeypatch.setattr(core, "_launch", boom)
    cid = create(client).json()["id"]
    _boot(client, world, cid)
    settle(client)
    d = detail(client, cid)
    assert d["status"] == "paused"
    assert "automatic resume" in d["last_error"]
