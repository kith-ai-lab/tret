"""The batch orchestrator: one register of targets, one engine run per target.

A batch takes a register (one row per target), launches one run per target on
the profile's harness with the profile's templated task input, and tracks each
target from run to review to whatever the profile writes afterwards. After the
first batch it can stop at a review gate (`first_batch_size`) until an approver
releases the rest. `finalize` writes the run-level records the profile builds.
What differs per kind of batch lives in `tret/batches/profiles/`; the disclosure
collection (shipped 2026-11-05) is the first profile and its behaviour is the
reference for everything here.

Where the state lives:

- `BatchTarget.status` is re-derived from facts, never advanced by hand: the
  run's status, its review findings (draft / approved / rejected) and the
  outcome the run reported through `report_collection_outcome`
  (`engine/collection_tools.py`). The rules are the profile's `derive_status`;
  `_settle_target` applies them and performs the write step they call for.
- `Batch.status` is likewise derived from its targets (`derive_collection_status`),
  except that `draft`, `paused` and `finalized` are set by the operator and stay
  until the operator moves them.

What moves a target: its run finishing (`_run_target`), a decision on one of its
review findings (`notify_decided`, from `api/findings.py`), and packaging
finishing (`notify_package_finished`, from the packaging task). Launching is
slot-limited: at most the batch's `concurrency` targets are `running` at once,
and a process-wide semaphore (`TRET_COLLECTIONS_MAX_CONCURRENT`) caps every
batch together. Each finishing target launches the next pending one.

Test patch point: `get_session_factory`, `get_harness_engine` and
`CANCEL_GRACE_SECONDS` are resolved through `tret.services.collections` at call
time (`_compat()`), because the existing tests patch them there. Collapse that
when the tests move to `tret.batches`.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib
import json
import logging
import re
import shutil
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable
from urllib.parse import urlsplit

from fastapi import HTTPException
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from tret.batches.profiles import BatchProfile, RegisterError, get_profile, profile_for
from tret.config import get_settings
from tret.db.models import (
    Batch,
    BatchTarget,
    Finding,
    Project,
    Run,
)
from tret.engine.collection_tools import COLLECTION_ID_KEY, COLLECTION_TARGET_KEY
from tret.net import policy as egress_policy
from tret.packs.links import packs_for_harness, resolve_pack_for_task
from tret.services import connections as connections_service
from tret.services import lifecycle
from tret.storage import batch_record_key, get_store

log = logging.getLogger("tret.collections")

COLLECTION_STATUSES = ("draft", "running", "paused", "gate", "review", "completed", "finalized", "failed")
TARGET_STATUSES = (
    "pending",
    "held",
    "running",
    "review",
    "packaging",
    "written",
    "completed",
    "gap",
    "failed",
    "rejected",
    "write_failed",
)
TERMINAL_TARGET_STATUSES = frozenset({"written", "completed", "gap", "failed", "rejected", "write_failed"})
DONE_TARGET_STATUSES = frozenset({"written", "completed"})
# What a profile's `derive_status` returns when the batch writes automatically
# and some output is not written yet. Never stored: `_settle_target` turns it
# into `written` or `write_failed`.
WRITE_PENDING = "write_pending"

RUN_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,60}$")
TARGET_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
MAX_TARGETS = 500
CONCURRENCY_MIN, CONCURRENCY_MAX = 1, 6
# How long a timed-out target run gets to notice the engine's cooperative cancel
# before its task is torn down (the same grace `delegate_parallel` gives).
CANCEL_GRACE_SECONDS = 30.0
SUCCESS_RUN_STATUSES = ("completed", "completed_without_output")
# Set on a target whose packages were ready only after its collection was
# finalized: nothing is uploaded until an operator asks (retry-write).
FINALIZED_BEFORE_WRITE = "collection finalized before write; re-finalize after retry-write"
MAX_WARNINGS = 20

# also what the shutdown drain in `tret/main.py` waits on.
_collection_tasks: set[asyncio.Task] = set()
# (event loop, semaphore / lock), created lazily on the loop that first needs
# them so a restarted loop (tests, reloads) never inherits one bound to a dead loop.
_semaphore_state: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None
_lock_state: tuple[asyncio.AbstractEventLoop, asyncio.Lock] | None = None
# One lock per target, so two settlers (a decision, a packaging callback, a
# retry-write) never upload the same target's files at once.
_target_locks: dict[uuid.UUID, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}
# The app whose draining state gates launches (`bind_app`, from main's lifespan).
_app: Any = None


def draining() -> bool:
    """True while the bound app is shutting down (no new batch may start)."""
    return lifecycle.is_draining(_app)


def bind_app(app: Any) -> None:
    """Tell the service which app's draining flag to honour: no new target is
    launched while `lifecycle.is_draining(app)`."""
    global _app
    _app = app


def _target_lock(target_id: uuid.UUID) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    entry = _target_locks.get(target_id)
    if entry is None or entry[0] is not loop:
        entry = (loop, asyncio.Lock())
        _target_locks[target_id] = entry
    return entry[1]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def clamp_concurrency(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        n = 3
    return max(CONCURRENCY_MIN, min(CONCURRENCY_MAX, n))


def _semaphore() -> asyncio.Semaphore:
    global _semaphore_state
    loop = asyncio.get_running_loop()
    if _semaphore_state is None or _semaphore_state[0] is not loop:
        size = clamp_concurrency(get_settings().collections_max_concurrent)
        _semaphore_state = (loop, asyncio.Semaphore(size))
    return _semaphore_state[1]


def _launch_lock() -> asyncio.Lock:
    global _lock_state
    loop = asyncio.get_running_loop()
    if _lock_state is None or _lock_state[0] is not loop:
        _lock_state = (loop, asyncio.Lock())
    return _lock_state[1]


def _compat():
    """`tret.services.collections`, resolved at call time: test patch point (see
    the module docstring)."""
    return importlib.import_module("tret.services.collections")


def _session_factory():
    # Resolved per call (not bound at import) so tests can swap
    # `get_session_factory` on the compatibility module.
    return _compat().get_session_factory()


_URL_RE = re.compile(r"https?://\S+")
ERROR_SUMMARY_MAX = 300


def summarise_run_error(text: str | None) -> str | None:
    """A run's error made safe for a customer-facing record. A provider error
    often carries the raw upstream JSON body, with an internal key identifier
    and account URL in it. The first `{` to the last `}` is replaced by the
    object's `error.message` (or `message`) when it parses, else dropped; then
    every http(s) URL is removed, whitespace collapsed and the result capped at
    300 characters. A plain error passes through unchanged."""
    if text is None:
        return None
    s = str(text)
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        message = ""
        with contextlib.suppress(ValueError, TypeError):
            obj = json.loads(s[start : end + 1])
            if isinstance(obj, dict):
                err = obj.get("error")
                found = err.get("message") if isinstance(err, dict) else None
                if not isinstance(found, str):
                    found = obj.get("message")
                if isinstance(found, str):
                    message = found
        s = s[:start] + message + s[end + 1 :]
    s = _URL_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()[:ERROR_SUMMARY_MAX]


def is_infra_error(text: str | None) -> bool:
    """Does a target's error read as an I/O or storage failure (a full disk, a
    read-only file system, the capture store) rather than a problem with the
    target itself? Finalizing over such targets would record an infrastructure
    fault as if it were a finding about the target."""
    return bool(text) and _INFRA_ERROR_RE.search(str(text)) is not None


_INFRA_ERROR_RE = re.compile(
    r"no space left|enospc|errno 28|read-only file system|disk full|disk quota|oserror|ioerror"
    r"|permissionerror|insufficient storage|storage (?:full|quota|unavailable)|capture store",
    re.IGNORECASE,
)


class FinalizeBody(BaseModel):
    force: bool = False


def review_schema_of(profile: BatchProfile, batch: Any = None) -> str | None:
    """The finding schema `profile` reviews for `batch`: `review_schema_for(batch)`
    when the profile has it (the schema can depend on the batch's settings), else
    the static `review_schema`."""
    per_batch = getattr(profile, "review_schema_for", None)
    if per_batch is not None:
        return per_batch(batch)
    return getattr(profile, "review_schema", None)


_MB = 1024 * 1024
# A review schema meaning "any draft finding the run proposes, whatever its type".
ANY_REVIEW = "any"


def schema_matches(review_schema: str | None, schema_slug: str | None) -> bool:
    """Is a finding of `schema_slug` one the batch reviews?"""
    if review_schema is None:
        return False
    return review_schema == ANY_REVIEW or review_schema == schema_slug


def free_storage_mb(settings: Any = None) -> int | None:
    """Free space, in MB, on the volume holding `storage_dir` (the nearest
    existing parent when the directory is not created yet); None when it cannot
    be read."""
    from pathlib import Path

    settings = settings or get_settings()
    path = Path(settings.storage_dir).resolve()
    while not path.exists() and path != path.parent:
        path = path.parent
    try:
        return int(shutil.disk_usage(path).free // _MB)
    except OSError:
        return None


def check_headroom(targets: Iterable[BatchTarget]) -> None:
    """409 when the local capture store has less free space than
    `batches_min_free_mb_per_target` x the targets still to run (pending or
    held). Skipped when the setting is 0 or the storage backend is not local."""
    settings = get_settings()
    per_target = int(settings.batches_min_free_mb_per_target or 0)
    if per_target <= 0 or get_store(settings).backend != "local":
        return
    remaining = sum(1 for t in targets if t.status in ("pending", "held"))
    if remaining <= 0:
        return
    free = free_storage_mb(settings)
    needed = remaining * per_target
    if free is not None and free < needed:
        raise HTTPException(
            409,
            f"Not enough free space on the capture store: {free} MB free, {needed} MB needed "
            f"for {remaining} targets at {per_target} MB each (TRET_BATCHES_MIN_FREE_MB_PER_TARGET)",
        )


def derive_collection_status(current: str, target_statuses: Iterable[str]) -> str:
    """The collection's status from its targets'. `draft`, `paused`,
    `finalized` and `failed` are the operator's (or reserved) and stay put."""
    if current in ("draft", "paused", "finalized", "failed"):
        return current
    statuses = list(target_statuses)
    if "running" in statuses or "pending" in statuses:
        return "running"
    # Review outranks the gate, so decisions still owed stay visible.
    if any(s in ("review", "packaging") for s in statuses):
        return "review"
    if "held" in statuses:
        return "gate"
    return "completed"


# ══════════════════════════════════════════════════════════════════════════
# derived fields (shared by the API serializers and the records)
# ══════════════════════════════════════════════════════════════════════════
def run_cost(run: Run | None) -> float:
    if run is None:
        return 0.0
    return float(run.cost_usd or 0) + float(run.delegated_cost_usd or 0)


def target_cost(t: BatchTarget, run: Run | None) -> float:
    """Every attempt's spend: the current run plus the archived attempts."""
    past = sum(float(h.get("cost_usd") or 0) for h in (t.history or []))
    return run_cost(run) + past


def written_files_latest(t: BatchTarget) -> list[dict]:
    """`written_files` is append-only; a re-upload of the same path replaces the
    file in SharePoint, so the latest record per filename is the live one."""
    latest: dict[str, dict] = {}
    for w in t.written_files or []:
        latest.pop(w.get("filename"), None)
        latest[w.get("filename")] = w
    return list(latest.values())


def target_out(
    t: BatchTarget, run: Run | None, findings: list[Finding], profile: BatchProfile | None = None
) -> dict:
    profile = profile or get_profile("disclosure")
    return {
        "id": str(t.id),
        "collection_id": str(t.collection_id),
        "position": t.position,
        "slug": t.slug,
        "label": t.label,
        "task_input": dict(t.task_input or {}),
        **profile.target_extra(t, run, findings),
        "status": t.status,
        "run_id": str(t.run_id) if t.run_id else None,
        "finding_ids": list(t.finding_ids or []),
        "attempts": t.attempts or 0,
        "error": summarise_run_error(t.error),
        "outcome": t.outcome,
        "written_files": [
            {k: w.get(k) for k in ("filename", "web_url", "sha256", "byte_size", "uploaded_at")}
            for w in written_files_latest(t)
        ],
        "history": list(t.history or []),
        "cost_usd": round(target_cost(t, run), 6),
        "model_used": run.model_used if run else None,
        "iterations": run.iterations if run else None,
        "started_at": _iso(t.started_at),
        "finished_at": _iso(t.finished_at),
    }


def collection_counts(targets: list[BatchTarget], profile: BatchProfile | None = None) -> dict:
    profile = profile or get_profile("disclosure")
    by_status = {s: 0 for s in TARGET_STATUSES}
    for t in targets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    return {"total": len(targets), "by_status": by_status, **profile.counts_extra(targets)}


def collection_out(c: Batch, targets: list[BatchTarget], runs: dict[uuid.UUID, Run]) -> dict:
    profile = profile_for(c)
    return {
        "id": str(c.id),
        "workspace_id": str(c.workspace_id),
        "name": c.name,
        "run_slug": c.run_slug,
        "profile": c.profile or "disclosure",
        "harness_id": str(c.harness_id) if c.harness_id else None,
        "parent_run_id": str(c.parent_run_id) if c.parent_run_id else None,
        "status": c.status,
        "settings": {k: (c.settings or {}).get(k) for k in profile.settings_keys()},
        "created_by": str(c.created_by) if c.created_by else None,
        "created_at": _iso(c.created_at),
        "started_at": _iso(c.started_at),
        "finalized_at": _iso(c.finalized_at),
        "counts": collection_counts(targets, profile),
        "cost_usd": round(
            sum(target_cost(t, runs.get(t.run_id) if t.run_id else None) for t in targets), 6
        ),
        "warnings": list(c.warnings or []),
        "finalize": c.finalize,
        "last_error": (c.settings or {}).get("last_error"),
    }


async def load_targets(db: AsyncSession, collection_ids: list[uuid.UUID]) -> list[BatchTarget]:
    if not collection_ids:
        return []
    return list(
        (
            await db.execute(
                select(BatchTarget)
                .where(BatchTarget.collection_id.in_(collection_ids))
                .order_by(BatchTarget.collection_id, BatchTarget.position)
                .execution_options(populate_existing=True)
            )
        ).scalars()
    )


async def load_runs(db: AsyncSession, targets: list[BatchTarget]) -> dict[uuid.UUID, Run]:
    run_ids = [t.run_id for t in targets if t.run_id]
    if not run_ids:
        return {}
    rows = (
        await db.execute(
            select(Run).where(Run.id.in_(run_ids)).execution_options(populate_existing=True)
        )
    ).scalars()
    return {r.id: r for r in rows}


async def load_review_findings(
    db: AsyncSession, profile: BatchProfile, targets: list[BatchTarget], batch: Any = None
) -> dict[uuid.UUID, list[Finding]]:
    """The profile's review findings (its review schema for `batch`) by run id,
    oldest first. Empty for a profile or a batch with no review step."""
    run_ids = [t.run_id for t in targets if t.run_id]
    schema = review_schema_of(profile, batch)
    if not run_ids or schema is None:
        return {}
    query = select(Finding).where(Finding.run_id.in_(run_ids))
    if schema != ANY_REVIEW:
        query = query.where(Finding.schema_slug == schema)
    rows = (
        await db.execute(
            query.order_by(Finding.created_at)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    out: dict[uuid.UUID, list[Finding]] = {}
    for f in rows:
        out.setdefault(f.run_id, []).append(f)
    return out


# ══════════════════════════════════════════════════════════════════════════
# lifecycle
# ══════════════════════════════════════════════════════════════════════════
async def get_collection(db: AsyncSession, collection_id: uuid.UUID, workspace_id: uuid.UUID) -> Batch:
    c = await db.get(Batch, collection_id)
    if c is None or c.workspace_id != workspace_id:
        raise HTTPException(404, "Collection not found")
    return c



def _require(c: Batch, allowed: tuple[str, ...], verb: str) -> None:
    if c.status not in allowed:
        raise HTTPException(409, f"Collection is '{c.status}'; it cannot be {verb} now")


async def start(db: AsyncSession, c: Batch) -> None:
    _require(c, ("draft",), "started")
    profile = profile_for(c)
    if await profile.harness_for(db, c) is None:
        raise HTTPException(409, profile.no_harness_error)
    check_headroom(await load_targets(db, [c.id]))
    c.status = "running"
    c.started_at = _now()
    await db.commit()
    await _advance(c.id)


async def pause(db: AsyncSession, c: Batch) -> None:
    _require(c, ("running", "gate", "review"), "paused")
    c.status = "paused"
    await db.commit()


async def resume(db: AsyncSession, c: Batch) -> None:
    _require(c, ("paused",), "resumed")
    c.status = "running"
    if (c.settings or {}).get("last_error"):
        c.settings = {k: v for k, v in c.settings.items() if k != "last_error"}
    await db.commit()
    await _advance(c.id)


async def release(db: AsyncSession, c: Batch) -> None:
    _require(c, ("gate", "review", "completed", "paused"), "released")
    held = [t for t in await load_targets(db, [c.id]) if t.status == "held"]
    if not held:
        raise HTTPException(409, "No held targets")
    check_headroom(await load_targets(db, [c.id]))
    for t in held:
        t.status = "pending"
    if c.status != "paused":
        c.status = "running"
    await db.commit()
    await _advance(c.id)


def _archive_attempt(t: BatchTarget, run: Run | None) -> None:
    """Push the attempt that is about to be replaced onto `history`, then clear
    what belongs to it (the outcome it reported, its error, its maps)."""
    if t.run_id is None and t.outcome is None and t.error is None:
        return
    t.history = [
        *(t.history or []),
        {
            "attempt": t.attempts or 0,
            "run_id": str(t.run_id) if t.run_id else None,
            "status": t.status,
            "error": summarise_run_error(t.error),
            "outcome": t.outcome,
            "cost_usd": round(run_cost(run), 6),
            "model_used": run.model_used if run else None,
            "started_at": _iso(t.started_at),
            "finished_at": _iso(t.finished_at),
        },
    ]
    t.run_id = None
    t.outcome = None
    t.error = None
    t.finding_ids = []
    t.started_at = None
    t.finished_at = None


async def retry_target(db: AsyncSession, c: Batch, t: BatchTarget) -> None:
    if c.status in ("draft", "finalized"):
        raise HTTPException(409, f"Collection is '{c.status}'; its targets cannot be retried")
    if t.status not in ("failed", "gap", "rejected", "write_failed"):
        raise HTTPException(
            409,
            f"Target is '{t.status}'; only failed, gap, rejected or write_failed targets can be retried",
        )
    run = await db.get(Run, t.run_id) if t.run_id else None
    _archive_attempt(t, run)
    t.status = "pending"
    if c.status != "paused":
        c.status = "running"
    await db.commit()
    await _advance(c.id)


async def retry_write(db: AsyncSession, c: Batch, t: BatchTarget) -> None:
    finalized_hold = c.status == "finalized" and t.error == FINALIZED_BEFORE_WRITE
    if t.status != "write_failed" and not finalized_hold:
        raise HTTPException(409, f"Target is '{t.status}'; only write_failed targets can retry the write")
    await _settle_target(db, c, t, force_write=True)
    await _recompute(db, c)


async def _advance(collection_id: uuid.UUID) -> None:
    """Launch whatever pending targets fit the collection's free slots, then
    re-derive its status (which is how `gate`/`review`/`completed` are reached
    when nothing is left to launch)."""
    await _launch(collection_id)
    async with _session_factory()() as db:
        c = await db.get(Batch, collection_id)
        if c is not None:
            await _recompute(db, c)


async def _recompute(db: AsyncSession, c: Batch) -> str:
    # Re-read under a row lock: an operator's pause (or a finalize) committed
    # by another request since `c` was loaded must win over a derived status.
    await db.refresh(c, with_for_update=True)
    targets = await load_targets(db, [c.id])
    new = derive_collection_status(c.status, [t.status for t in targets])
    if new != c.status:
        c.status = new
    await db.commit()
    await rollup_parent_cost(db, c)
    return new


async def _launch(collection_id: uuid.UUID) -> int:
    """Create runs for the pending targets that fit the free slots (the
    collection's `concurrency` minus its running targets), in position order,
    and start a task for each. Returns how many were launched; none while the
    process is draining for shutdown."""
    if lifecycle.is_draining(_app):
        return 0
    async with _launch_lock():
        async with _session_factory()() as db:
            c = await db.get(Batch, collection_id)
            if c is None or c.status != "running":
                return 0
            targets = await load_targets(db, [c.id])
            running = sum(1 for t in targets if t.status == "running")
            slots = max(0, clamp_concurrency((c.settings or {}).get("concurrency")) - running)
            pending = [t for t in targets if t.status == "pending"]
            if not pending or slots == 0:
                return 0
            profile = profile_for(c)
            harness = await profile.harness_for(db, c)
            if harness is None:
                c.status = "paused"
                c.settings = {**(c.settings or {}), "last_error": profile.missing_harness_message}
                await db.commit()
                return 0
            pack = resolve_pack_for_task(await packs_for_harness(db, harness), "freeform")
            model_override = (c.settings or {}).get("model_override")
            max_iterations = (c.settings or {}).get("max_iterations")
            previous = await load_runs(db, pending)
            launched: list[tuple[uuid.UUID, uuid.UUID]] = []
            for t in pending:
                if len(launched) >= slots:
                    break
                if t.run_id is not None:  # defensive: retry_target archives first
                    _archive_attempt(t, previous.get(t.run_id))
                try:
                    built = profile.task_input(c, t, len(targets))
                except Exception as e:  # noqa: BLE001 - one bad row must not stop the batch
                    log.warning("batch target %s could not build its task: %s", t.id, e)
                    t.status = "failed"
                    t.error = summarise_run_error(f"task could not be built: {type(e).__name__}: {e}")
                    t.finished_at = _now()
                    continue  # a failed target takes no slot
                task_input = {
                    **built,
                    COLLECTION_ID_KEY: str(c.id),
                    COLLECTION_TARGET_KEY: str(t.id),
                }
                if model_override:
                    task_input["_model_override"] = model_override
                if max_iterations:
                    task_input["_max_iterations"] = int(max_iterations)
                run = Run(
                    id=uuid.uuid4(),
                    project_id=c.project_id,
                    harness_id=harness.id,
                    pack_id=pack.id if pack else None,
                    task_type="freeform",
                    task_input=task_input,
                    document_ids=[],
                    created_by=c.created_by,
                )
                db.add(run)
                # Insert the run before the target points at it: with no ORM
                # relationship between the two, the unit of work does not order
                # the UPDATE after the INSERT and Postgres rejects the FK.
                await db.flush()
                t.status = "running"
                t.run_id = run.id
                t.started_at = _now()
                t.finished_at = None
                t.error = None
                t.outcome = None
                t.finding_ids = []
                t.attempts = (t.attempts or 0) + 1
                launched.append((t.id, run.id))
            await db.commit()
        for target_id, run_id in launched:
            _spawn(_run_target(collection_id, target_id, run_id))
        return len(launched)


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _collection_tasks.add(task)
    task.add_done_callback(_collection_tasks.discard)
    return task


async def _close_out_run(run_id: uuid.UUID, error: str) -> None:
    with contextlib.suppress(Exception):
        async with _session_factory()() as db:
            run = await db.get(Run, run_id)
            if run is not None and run.status in ("queued", "running"):
                run.status = "cancelled"
                run.error = error
                run.finished_at = _now()
                await db.commit()


async def _run_target(collection_id: uuid.UUID, target_id: uuid.UUID, run_id: uuid.UUID) -> None:
    """One target's run, start to settle. Never raises (except cancellation)."""
    cancelled = False
    try:
        async with _semaphore():
            async with _session_factory()() as db:
                c = await db.get(Batch, collection_id)
                t = await db.get(BatchTarget, target_id)
                if c is None or t is None or t.run_id != run_id:
                    return
                if c.status != "running":
                    # Paused while this target waited for a process-wide slot:
                    # give the slot back untouched, to launch on resume.
                    run = await db.get(Run, run_id)
                    if run is not None and run.status == "queued":
                        run.status = "cancelled"
                        run.error = "collection paused before this run started"
                        run.finished_at = _now()
                    t.status = "pending"
                    t.run_id = None
                    t.started_at = None
                    t.attempts = max(0, (t.attempts or 1) - 1)
                    await db.commit()
                    return
            engine = _compat().get_harness_engine()
            timeout = max(1, int(get_settings().collections_run_timeout_seconds))
            task = asyncio.create_task(engine.execute(run_id))
            done, _ = await asyncio.wait({task}, timeout=timeout)
            timed_out = not done
            if timed_out:
                engine.cancel(run_id)
                done, _ = await asyncio.wait({task}, timeout=_compat().CANCEL_GRACE_SECONDS)
                if not done:
                    task.cancel()
                    # asyncio.wait never raises the task's CancelledError, so a
                    # cancellation of THIS task still propagates.
                    await asyncio.wait({task}, timeout=_compat().CANCEL_GRACE_SECONDS)
                await _close_out_run(run_id, "timeout: stopped by the collection run's time limit")
                forget = getattr(engine, "forget_cancelled", None)
                if forget is not None:
                    forget(run_id)
            else:
                task.result()  # an engine crash is a target failure below
        async with _session_factory()() as db:
            c = await db.get(Batch, collection_id)
            t = await db.get(BatchTarget, target_id)
            if c is None or t is None or t.run_id != run_id:
                return
            status = await _settle_target(db, c, t)
            if timed_out and status == "failed":
                # A run that finished within the cancel grace settled normally
                # above; only one the time limit actually stopped is a timeout.
                t.error = "timeout"
                await db.commit()
    except asyncio.CancelledError:
        cancelled = True
        raise
    except Exception as e:  # noqa: BLE001 - a background task has no caller to raise to
        log.exception("collection target %s crashed", target_id)
        with contextlib.suppress(Exception):
            async with _session_factory()() as db:
                t = await db.get(BatchTarget, target_id)
                if t is not None and t.run_id == run_id:
                    t.status = "failed"
                    t.error = summarise_run_error(f"{type(e).__name__}: {e}")
                    t.finished_at = _now()
                    await db.commit()
    finally:
        if not cancelled:
            try:
                await _advance(collection_id)
            except Exception:  # noqa: BLE001
                log.exception("collection %s could not advance", collection_id)


async def _settle_target(
    db: AsyncSession, c: Batch, t: BatchTarget, *, force_write: bool = False
) -> str:
    """Re-derive `t`'s status from its run, its maps and its outcome, and upload
    its packages whenever every approved map is packaged and some file is not
    written yet, whatever status was stored before. Idempotent; serialized per
    target. After the collection is finalized nothing is uploaded unless
    `force_write` (retry-write). Commits."""
    async with _target_lock(t.id):
        # Another settler may have committed (written files, a new run) while
        # this one waited for the lock.
        await db.refresh(t)
        await db.refresh(c)
        run = await db.get(Run, t.run_id, populate_existing=True) if t.run_id else None
        profile = profile_for(c)
        maps = (await load_review_findings(db, profile, [t], c)).get(t.run_id, []) if t.run_id else []
        t.finding_ids = [str(f.id) for f in maps]
        settle = getattr(profile, "settle_outcome", None)
        if settle is not None and run is not None:
            outcome = await settle(db, c, t, run, maps)
            if outcome is not None:
                t.outcome = outcome
        auto_write = bool((c.settings or {}).get("auto_write"))
        status, error = profile.derive_status(
            run.status if run else None,
            t.outcome,
            maps,
            run_error=summarise_run_error(run.error) if run else "run missing",
            auto_write=auto_write or force_write,
            written=profile.all_written(t, maps),
        )
        if status == WRITE_PENDING:
            if c.status == "finalized" and not force_write:
                status, error = "completed", FINALIZED_BEFORE_WRITE
            else:
                status, write_error = await profile.write_outputs(db, c, t, maps)
                error = write_error or error
        t.status = status
        t.error = summarise_run_error(error)
        if status in TERMINAL_TARGET_STATUSES:
            t.finished_at = t.finished_at or _now()
        else:
            t.finished_at = None
        await db.commit()
        await rollup_parent_cost(db, c)
        return status


async def rollup_parent_cost(db: AsyncSession, c: Batch) -> None:
    """Fold the batch's spend into the run that started it (`parent_run_id`, set
    by the `start_batch` tool) so the parent's `delegated_cost_usd` reflects the
    work it caused. Delta-based and idempotent: `total` is every target's cost
    (`target_cost`, retries included); `settings["rolled_up_cost_usd"]` is what
    was already added, so only `total - already` goes onto the parent, in the
    same commit that records the new `already`. Serialised by the launch lock so
    concurrent settles never both add the same delta; a crash between the two
    writes cannot happen (one commit), and a lost commit is corrected by the
    next settle. Skipped while the parent run is still queued or running (its
    own cost cap would see the spend); `_recompute` calls it too, so it catches
    up after the parent finishes. Never raises: a roll-up failure must not fail
    a settle."""
    if c.parent_run_id is None:
        return
    try:
        async with _launch_lock():
            await db.refresh(c)
            targets = await load_targets(db, [c.id])
            runs = await load_runs(db, targets)
            total = round(
                sum(target_cost(t, runs.get(t.run_id) if t.run_id else None) for t in targets), 6
            )
            already = float((c.settings or {}).get("rolled_up_cost_usd") or 0)
            delta = round(total - already, 6)
            if delta == 0:
                return
            parent = await db.get(Run, c.parent_run_id, populate_existing=True)
            if parent is None:
                return
            if parent.status in ("queued", "running"):
                # The parent's own cost cap counts `delegated_cost_usd`; adding the
                # batch's spend mid-turn could stop the turn that started it. The
                # next settle or recompute after the parent finishes catches up.
                return
            # Atomic in SQL, so a concurrent `_record_delegated_cost` is not lost.
            await db.execute(
                update(Run)
                .where(Run.id == parent.id)
                .values(delegated_cost_usd=Run.delegated_cost_usd + Decimal(str(delta)))
                .execution_options(synchronize_session=False)
            )
            c.settings = {**(c.settings or {}), "rolled_up_cost_usd": total}
            await db.commit()
    except Exception:  # noqa: BLE001 - see docstring
        log.exception("batch %s could not roll its cost up to run %s", c.id, c.parent_run_id)
        with contextlib.suppress(Exception):
            await db.rollback()


# ══════════════════════════════════════════════════════════════════════════
# create
# ══════════════════════════════════════════════════════════════════════════
def _allowlist_warnings(rows: list[dict]) -> list[str]:
    """Warnings for known-URL hosts `fetch_url` would refuse: when research is on
    with a non-empty allowlist, a host outside it is a target that cannot be
    fetched. Warnings, never errors; at most MAX_WARNINGS."""
    if egress_policy.effective_mode(egress_policy.CLASS_RESEARCH) == egress_policy.MODE_OFF:
        return []
    if not egress_policy.research_allow_hosts():
        return []
    research = egress_policy.policy_for(egress_policy.CLASS_RESEARCH)
    out: list[str] = []
    for row in rows:
        label = row.get("label") or row.get("company") or row.get("slug")
        seen: set[str] = set()
        for url in row.get("known_urls") or []:
            try:
                host = (urlsplit(str(url)).hostname or "").lower().rstrip(".")
            except ValueError:  # a malformed URL is the register's problem, not create's
                continue
            if not host or host in seen:
                continue
            seen.add(host)
            if not egress_policy.host_allowed(host, research):
                out.append(
                    f"{label}: known URL host {host} is not in the research allowlist "
                    "(TRET_EGRESS_RESEARCH_ALLOW_HOSTS); fetch_url will refuse it"
                )
    if len(out) > MAX_WARNINGS:
        extra = len(out) - MAX_WARNINGS
        out = [*out[:MAX_WARNINGS], f"... and {extra} more"]
    return out


async def create_batch(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    body: Any,
    profile_slug: str,
) -> tuple[Batch, list[BatchTarget]]:
    try:
        profile = get_profile(profile_slug)
    except KeyError as e:
        raise HTTPException(422, f"Unknown batch profile '{profile_slug}'") from e
    if not RUN_SLUG_RE.match(body.run_slug):
        raise HTTPException(
            422, "run_slug must be 3-61 lowercase letters, digits or hyphens, starting with a letter or digit"
        )
    try:
        rows, warnings = await profile.parse_targets(body)
    except RegisterError as e:
        raise HTTPException(422, str(e)) from e
    warnings = [*warnings, *_allowlist_warnings(rows)]
    existing = (
        await db.execute(
            select(Batch.id).where(Batch.workspace_id == workspace_id, Batch.run_slug == body.run_slug)
        )
    ).first()
    if existing is not None:
        raise HTTPException(409, f"A collection with run_slug '{body.run_slug}' already exists")
    settings = await profile.validate_settings(db, workspace_id, body)
    project = (
        await db.execute(
            select(Project).where(Project.workspace_id == workspace_id).order_by(Project.created_at)
        )
    ).scalars().first()
    if project is None:
        raise HTTPException(500, "No project exists")

    batch = Batch(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        project_id=project.id,
        name=body.name.strip(),
        run_slug=body.run_slug,
        profile=profile.slug,
        harness_id=getattr(body, "harness_id", None),
        parent_run_id=getattr(body, "parent_run_id", None),
        status="draft",
        settings=settings,
        warnings=warnings,
        created_by=user_id,
        created_at=_now(),
    )
    db.add(batch)
    gate = body.first_batch_size
    targets = []
    for row in rows:
        t = BatchTarget(
            id=uuid.uuid4(),
            collection_id=batch.id,
            position=row["position"],
            slug=row["slug"],
            label=row.get("label") or row.get("company") or row["slug"],
            task_input=row.get("task_input") or {},
            company=row.get("company") or "",
            market=row.get("market") or "",
            domicile_country=row.get("domicile_country"),
            sector=row.get("sector"),
            category=row.get("category"),
            expected_form=row.get("expected_form") or "unknown",
            fiscal_year=row.get("fiscal_year"),
            known_urls=row.get("known_urls") or [],
            notes=row.get("notes"),
            status="held" if gate > 0 and row["position"] > gate else "pending",
            finding_ids=[],
            attempts=0,
            written_files=[],
        )
        targets.append(t)
    await db.flush()  # the batch row first: targets reference it
    for t in targets:
        db.add(t)
    await db.commit()
    return batch, targets


async def create_collection(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID | None, body: Any
) -> tuple[Batch, list[BatchTarget]]:
    return await create_batch(
        db, workspace_id=workspace_id, user_id=user_id, body=body, profile_slug="disclosure"
    )


async def _targets_for_findings(db: AsyncSession, finding_ids: Iterable[uuid.UUID]) -> list[BatchTarget]:
    """The batch targets whose run produced one of these findings, when the
    finding is of the review schema of that target's batch profile."""
    schemas_by_run: dict[uuid.UUID, set[str]] = {}
    for fid in finding_ids:
        f = await db.get(Finding, fid)
        if f is not None and f.schema_slug and f.run_id is not None:
            schemas_by_run.setdefault(f.run_id, set()).add(f.schema_slug)
    if not schemas_by_run:
        return []
    candidates = (
        await db.execute(select(BatchTarget).where(BatchTarget.run_id.in_(list(schemas_by_run))))
    ).scalars()
    out = []
    for t in candidates:
        c = await db.get(Batch, t.collection_id)
        if c is not None and any(
            schema_matches(review_schema_of(profile_for(c), c), slug)
            for slug in schemas_by_run.get(t.run_id, ())
        ):
            out.append(t)
    return out


async def _resettle_targets(db: AsyncSession, target_ids: Iterable[uuid.UUID]) -> None:
    """Settle each target (always: the derivation decides, whatever is stored)
    and re-derive the collections they belong to."""
    touched: set[uuid.UUID] = set()
    for tid in target_ids:
        t = await db.get(BatchTarget, tid)
        c = await db.get(Batch, t.collection_id) if t is not None else None
        if t is None or c is None:
            continue
        try:
            await _settle_target(db, c, t)
        except Exception:  # noqa: BLE001 - one target must not stop the rest
            log.exception("could not settle collection target %s", tid)
            with contextlib.suppress(Exception):
                await db.rollback()
        touched.add(c.id)
    for cid in touched:
        c = await db.get(Batch, cid)
        if c is not None:
            await _recompute(db, c)


async def _after_approve(db: AsyncSession, finding_ids: Iterable[uuid.UUID]) -> None:
    """Tell each batch profile about the review findings of its targets that
    were approved. A profile hook that fails must not stop the re-settle."""
    for fid in finding_ids:
        try:
            f = await db.get(Finding, fid)
            if f is None or f.status != "approved" or f.run_id is None:
                continue
            t = (
                await db.execute(select(BatchTarget).where(BatchTarget.run_id == f.run_id))
            ).scalars().first()
            c = await db.get(Batch, t.collection_id) if t is not None else None
            if c is None:
                continue
            profile = profile_for(c)
            schema = review_schema_of(profile, c)
            if schema_matches(schema, f.schema_slug):
                await profile.after_approve(db, f)
        except Exception:  # noqa: BLE001
            log.exception("after_approve failed for finding %s", fid)
            with contextlib.suppress(Exception):
                await db.rollback()


async def _resettle_in_background(
    target_ids: list[uuid.UUID], session_factory, decided_ids: Iterable[uuid.UUID] = ()
) -> None:
    try:
        async with session_factory()() as db:
            await _after_approve(db, decided_ids)
            await _resettle_targets(db, target_ids)
    except Exception:  # noqa: BLE001 - a background task has no caller to raise to
        log.exception("collection re-settle failed for targets %s", target_ids)


async def notify_decided(
    finding_ids: Iterable[uuid.UUID], *, db: AsyncSession, session_factory=None
) -> asyncio.Task | None:
    """Approvals moved: find the collection targets these findings belong to
    (a cheap read on the caller's session) and re-settle them in a background
    task, so a settle that uploads never runs inside the HTTP request. None
    when no finding belongs to a collection."""
    ids = list(finding_ids)
    if not ids:
        return None
    targets = await _targets_for_findings(db, ids)
    if not targets:
        return None
    factory = session_factory if session_factory is not None else _session_factory
    return _spawn(_resettle_in_background([t.id for t in targets], factory, ids))


async def notify_package_finished(finding_id: uuid.UUID, *, session_factory=None) -> None:
    """Packaging finished (or failed) for `finding_id`: re-settle its target,
    which is what moves `packaging` -> `written` / `completed` / `failed`."""
    factory = session_factory() if session_factory is not None else _session_factory()
    async with factory() as db:
        targets = await _targets_for_findings(db, [finding_id])
        if targets:
            await _resettle_targets(db, [t.id for t in targets])


async def drain_collection_tasks(deadline_seconds: float) -> bool:
    """Wait up to `deadline_seconds` for every in-flight target task (including
    the ones finishing tasks launch). True when none are left."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + deadline_seconds
    while True:
        pending = {t for t in _collection_tasks if not t.done()}
        if not pending:
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        await asyncio.wait(pending, timeout=remaining)


async def sweep_orphaned_collections(db: AsyncSession) -> int:
    """Boot sweep, after `sweep_orphaned_runs` and the packaging sweep. Targets
    left `running` whose run is no longer queued/running, and targets in
    `review`/`packaging`, are re-settled from the facts (a run that completed
    is judged by its maps; only one the orphan sweep failed becomes `failed`;
    a restart mid-upload resumes the upload). Collections still `running` with
    pending targets are paused for an operator's resume rather than launched at
    boot. Returns how many targets were re-settled."""
    targets = list(
        (
            await db.execute(
                select(BatchTarget).where(
                    BatchTarget.status.in_(["running", "review", "packaging"])
                )
            )
        ).scalars()
    )
    runs = await load_runs(db, targets)
    to_settle = []
    for t in targets:
        run = runs.get(t.run_id) if t.run_id else None
        if t.status == "running" and run is not None and run.status in ("queued", "running"):
            continue  # still live (only possible without the instance lock)
        to_settle.append(t.id)
    if to_settle:
        await _resettle_targets(db, to_settle)
        log.warning("boot sweep: re-settled %d collection target(s)", len(to_settle))
    collections = list(
        (await db.execute(select(Batch).where(Batch.status == "running"))).scalars()
    )
    for c in collections:
        if await _recompute(db, c) == "running":
            c.status = "paused"
            c.settings = {
                **(c.settings or {}),
                "last_error": "The server restarted during this collection; resume to continue.",
            }
            await db.commit()
    return len(to_settle)


async def finalize(db: AsyncSession, c: Batch, *, force: bool = False) -> None:
    """Write the profile's run-level records to `<write_root>/<run_slug>/` on the
    SharePoint target, or keep them as Documents when the batch has no target,
    replacing any earlier finalize's files. Allowed once nothing is executing,
    packaging or waiting for a decision; a finalized batch may be finalized again
    (after a retry-write, say). Targets that failed on an I/O or storage error
    block it unless `force`: the records would otherwise present an
    infrastructure fault as a finding about the target."""
    _require(c, ("gate", "review", "completed", "paused", "finalized"), "finalized")
    profile = profile_for(c)
    targets = await load_targets(db, [c.id])
    busy = [t for t in targets if t.status in ("running", "pending", "packaging")]
    if busy:
        raise HTTPException(
            409,
            f"{len(busy)} target(s) are still running, pending or packaging; finalize when they settle",
        )
    infra = [t for t in targets if t.status == "failed" and is_infra_error(t.error)]
    if infra and not force:
        raise HTTPException(
            409,
            f"{len(infra)} target(s) failed on an I/O or storage error "
            f"(first: {infra[0].slug}: {infra[0].error}); fix the cause and retry them, "
            "or finalize with force=true",
        )
    maps = await load_review_findings(db, profile, targets, c)
    blockers = profile.finalize_blockers(targets, maps)
    if blockers:
        raise HTTPException(409, blockers[0])
    settings = c.settings or {}
    now = _now()
    records = await profile.records(db, c, finalized_at=now)
    record_files = profile.record_files(records)
    if settings.get("target_slug"):
        files, error = await _upload_records(db, c, settings, record_files)
    else:
        count = int(settings.get("finalize_count") or 0) + 1
        files, error = await _store_records(db, c, record_files, count)
        c.settings = {**(c.settings or {}), "finalize_count": count}
        if error is not None:
            # Keep the previous finalize's files: their Documents are intact.
            previous = c.finalize or {}
            c.finalize = {**previous, "files": list(previous.get("files") or []), "error": error}
            await db.commit()
            return
    c.finalize = {"written_at": now.isoformat(), "files": files, "error": error}
    if error is None:
        c.status = "finalized"
        c.finalized_at = now
    await db.commit()


async def _upload_records(
    db: AsyncSession, c: Batch, settings: dict, record_files: dict[str, tuple[bytes, str]]
) -> tuple[list[dict], str | None]:
    """The record files to `<write_root>/<run_slug>/` on the SharePoint target."""
    root = f"{settings.get('write_root') or 'runs'}/{c.run_slug}"
    files: list[dict] = []
    for name, (data, content_type) in record_files.items():
        try:
            result = await connections_service.upload_connected_file(
                db,
                workspace_id=c.workspace_id,
                target_slug=settings.get("target_slug"),
                filename=f"{root}/{name}",
                data=data,
                content_type=content_type,
                actor_user_id=c.created_by,
                conflict="replace",
            )
        except (connections_service.ConnectionWriteError, connections_service.ConnectionUnavailable) as e:
            return files, f"{name}: {e.reason}"
        except Exception as e:  # noqa: BLE001 - recorded, finalize can be retried
            log.exception("finalize upload failed for %s", name)
            return files, f"{name}: {type(e).__name__}: {e}"
        files.append({"name": name, "web_url": result.web_url, "sha256": hashlib.sha256(data).hexdigest()})
    return files, None


async def _store_records(
    db: AsyncSession, c: Batch, record_files: dict[str, tuple[bytes, str]], count: int
) -> tuple[list[dict], str | None]:
    """The record files as Documents (`source_kind="batch_record"`) in the
    capture store, for a batch with no write target. Each finalize stores under its
    own key prefix (`batches/<id>/<count>/<name>`), so a re-finalize adds new
    Document rows and never changes bytes an older row points at; the batch
    points at the latest."""
    from tret.services.documents import ingest_document  # lazy: heavy imports

    store = get_store(get_settings())
    files: list[dict] = []
    for name, (data, content_type) in record_files.items():
        sha = hashlib.sha256(data).hexdigest()
        try:
            key = batch_record_key(c.id, f"{count}/{name}")
            await store.put(key, data, content_type=content_type)
            doc = await ingest_document(
                project_id=c.project_id,
                filename=name,
                content_type=content_type,
                storage_path=await store.local_path(key),
                storage_key=key,
                sha256=sha,
                byte_size=len(data),
                data=data,
                uploaded_by=c.created_by,
                source_kind="batch_record",
                meta_extra={"batch_id": str(c.id), "record": name},
            )
            db.add(doc)
            await db.flush()
        except Exception as e:  # noqa: BLE001 - recorded, finalize can be retried
            log.exception("finalize could not store %s", name)
            await db.rollback()  # drops this finalize's Documents, so none are listed
            await db.refresh(c)
            return [], f"{name}: {type(e).__name__}: {e}"
        files.append({"name": name, "sha256": sha, "document_id": str(doc.id), "byte_size": len(data)})
    return files, None


# Generic aliases for the batch names; the collection names above stay the
# surface the HTTP layer and the tests use.
notify_review_event = notify_decided
sweep_orphaned_batches = sweep_orphaned_collections
drain_batch_tasks = drain_collection_tasks
