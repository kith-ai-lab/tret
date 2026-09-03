"""Startup reconciliation: fail every run a killed process left mid-flight.

The event bus (`engine/events.py`) and the extension registry are both
in-process, and `HarnessEngine.execute` is the only code path that ever moves
a `Run` out of `queued`/`running` and fires its post-run hooks (billing,
today). A hard process kill — an OOM, a deploy, `fly machine restart` — never
runs that code path: the row is left at `status="running"` (or, more rarely,
still `"queued"`, if the process died between insert and `execute` picking it
up) forever, and the hooks that would have metered it never fire.

Because tret is single-instance by design (`docs/architecture.md`,
`services/instance_lock.py`), any run still non-terminal at the moment a fresh
process boots is not "maybe still running somewhere else" — it is, by
definition, orphaned by the restart that just happened. This module is the
sweep that closes those rows out at boot, before anything else can observe
them stuck in a state nothing will ever finish.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Harness, Run
from tret.engine.extensions import get_extension_registry

log = logging.getLogger("tret.reconcile")

# Every status a run can hold before `HarnessEngine` reaches a terminal one.
# Mirrors the comment on `Run.status` (db/models.py): queued | running |
# completed | completed_without_output | failed | cancelled — everything
# before the pipe is non-terminal.
NON_TERMINAL_STATUSES = ("queued", "running")

ORPHAN_ERROR = "process_restart: the server restarted while this run was in progress"

# How recently a swept run must have started (or, lacking that, been created)
# for its post-run hooks to still fire. The sweep itself is unbounded — every
# non-terminal row gets flipped to `failed` regardless of age, because a row
# nothing will ever finish is exactly as wrong whether it is an hour or a year
# old. Hooks are a different matter: the first boot after an upgrade (or after
# any long enough gap) can find rows that have sat `running` since well before
# this process, or any process it succeeded, was even watching them, and
# re-firing a billing hook for every one of those on a single boot is not
# reconciliation, it is re-metering history nobody asked to revisit. 24 hours
# comfortably outlasts the gap between an ordinary restart and the next boot
# while still excluding that kind of backlog.
HOOK_ELIGIBILITY_WINDOW = timedelta(hours=24)


def _as_aware_utc(dt: datetime) -> datetime:
    """`dt`, guaranteed tz-aware.

    Postgres (`TIMESTAMP(timezone=True)`, what every column here actually is)
    always hands one back already aware. SQLite — every test, `tret run`, and
    any self-hosted deployment that never provisioned Postgres — has no
    genuine timezone-aware storage, so the same column round-trips *naive*
    there; every value ever written to it, though, came from `utcnow()`
    (db/models.py), so a naive one is always already a UTC instant and
    reattaching that tzinfo is correct, not a guess.
    """
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def sweep_orphaned_runs(db: AsyncSession) -> int:
    """Fail every run left `queued`/`running` by a prior process, then run the
    post-run hooks of the ones recent enough for that to mean anything (see
    `HOOK_ELIGIBILITY_WINDOW`). Returns how many runs were swept in total.

    Must run after `load_extensions` has set the process-wide registry
    (`main.py`'s lifespan ordering) — otherwise the hooks below would fire
    against the default no-op registry and never reach a loaded billing
    extension. Safe to call on every boot: with no orphans, this is a single
    empty-result query and nothing else.
    """
    orphans = (
        (await db.execute(select(Run).where(Run.status.in_(NON_TERMINAL_STATUSES))))
        .scalars()
        .all()
    )
    if not orphans:
        return 0

    now = datetime.now(timezone.utc)
    for run in orphans:
        run.status = "failed"
        run.error = ORPHAN_ERROR
        run.finished_at = now
    await db.commit()

    # `started_at` is unset only for a run that died between insert and
    # `HarnessEngine.execute` ever picking it up (still `queued` — see the
    # module docstring); `created_at` is never null, so it is always a
    # meaningful fallback for exactly that case.
    cutoff = now - HOOK_ELIGIBILITY_WINDOW
    recent = [r for r in orphans if _as_aware_utc(r.started_at or r.created_at) >= cutoff]
    skipped = len(orphans) - len(recent)

    # Hooks after the commit, not before: a hook that reads `run.status`
    # (the billing extension does, to decide whether to meter) must see the
    # terminal state that is actually landing in the database, the same
    # ordering `HarnessEngine.execute`'s own crash handler uses.
    registry = get_extension_registry()
    for run in recent:
        # workspace_id via the run's harness — engine/harness.py's execute()
        # resolves it the same way. A harness deleted out from under a run it
        # created is not expected, but a hook must never crash the sweep over
        # a foreign key that no longer resolves.
        harness = await db.get(Harness, run.harness_id)
        workspace_id = harness.workspace_id if harness else None
        await registry.run_post_run_hooks(db, run, workspace_id)

    log.warning(
        "reconciled %d orphaned run(s) left non-terminal by a prior process: %s",
        len(orphans),
        ", ".join(str(r.id) for r in orphans),
    )
    if skipped:
        log.warning(
            "closed %d of those without firing post-run hooks: older than the %s "
            "hook-eligibility window, so this is not the first boot to have seen them",
            skipped,
            HOOK_ELIGIBILITY_WINDOW,
        )
    return len(orphans)
