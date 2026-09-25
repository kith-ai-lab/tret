"""Enforce the single-instance assumption fly.toml documents but nothing in
code checked until now.

tret's run event bus (`engine/events.py`) is in-process: a run's live SSE
updates and the extension seam's post-run hooks only ever reach clients
connected to the *same* machine that executed the run. `fly.toml` says "Do
NOT scale horizontally" in a comment; this module is what actually notices a
second process and says so.

On Postgres, `pg_try_advisory_lock` gives a lock scoped to one *session*
(connection) rather than one transaction, which is exactly the shape needed
here: acquired on a dedicated connection at boot, held for the lifetime of
the process by never returning that connection to a pool, released only at
shutdown. A second process trying the same fixed key gets `false` back
immediately rather than blocking — which is what lets it retry with its own
timeout instead of hanging on the database.

A brief overlap is normal and must not be an error: a Fly deploy starts the
new machine before stopping the old one (a "handover"), so the new process's
first attempt is *expected* to lose to the still-running old one. It retries
for `TRET_INSTANCE_LOCK_WAIT_SECONDS` (default 90s, sized to outlast a normal
handover plus the keepalive reaping below) before deciding the lock is
genuinely held by something else, not a machine mid-swap.

A harder case than a graceful handover: the previous VM crashes outright
(host failure, OOM kill) with its lock connection never closed. Postgres has
no way to know that socket is dead until its own TCP keepalive probing gives
up — which defaults to roughly two hours, far longer than any wait budget an
operator would accept for `TRET_INSTANCE_LOCK=strict`. `acquire_instance_lock`
fixes this by setting aggressive keepalive timing on the lock session itself
right after opening the connection (`tcp_keepalives_idle=15`,
`_interval=5`, `_count=3`): Postgres starts probing after 15s of silence and
gives up after 3 failed probes 5s apart, so a genuinely dead holder's
advisory lock is reaped within about half a minute — comfortably inside the
90s default wait. Not every Postgres accepts session-level keepalive
`SET`s (a managed provider may reject or ignore them), so this is
best-effort: refused entirely, it just falls back to the server's own
(much slower) default.

This tunes the *holder's own* lock connection, at the moment it opens it —
not the server globally. A machine already running a prior release, holding
the lock under the old (much slower) default timing, keeps that timing
until it itself restarts; only the process that opens the connection after
this code ships gets the faster reaping. Practically, the first cutover to
this behavior needs no special handling: a normal rolling deploy's handover
closes the old machine's lock connection cleanly anyway, so there is no
window where a crash actually needs the old default's ~2 hour probing.

SQLite (every test, `tret run`) has no advisory locks and is never the
horizontally-scaled deployment this guards against, so the check is a
silent no-op there — the same non-Postgres carve-out `db/migrate.py`'s
schema lock makes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, InterfaceError
from sqlalchemy.ext.asyncio import AsyncConnection

from tret.config import get_settings
from tret.db.engine import get_engine

log = logging.getLogger("tret.instance_lock")

# The lock connection is held for the process lifetime and never returned to
# a pool, so by the time `release()` runs at shutdown its socket may already
# be gone (the driver notices on the next statement, not proactively) — the
# raw asyncpg exception can surface unwrapped by SQLAlchemy in that case.
# asyncpg is an optional dependency of the (non-Postgres) test/CLI install,
# so this import is guarded; an empty tuple below just never matches.
try:
    from asyncpg.exceptions import InterfaceError as _AsyncpgInterfaceError

    ASYNCPG_INTERFACE_ERRORS: tuple[type[BaseException], ...] = (_AsyncpgInterfaceError,)
except ImportError:  # pragma: no cover - asyncpg absent (sqlite-only install)
    ASYNCPG_INTERFACE_ERRORS = ()

# Arbitrary but fixed, exactly like db/migrate.py's SCHEMA_LOCK_KEY, and
# deliberately a different value so the two locks (schema-upgrade-in-
# progress vs. only-one-process-running) never collide. Never change this
# once shipped: a new release using a different key would stop noticing an
# old release's still-running process during a deploy.
INSTANCE_LOCK_KEY = 0x74726574_494C4B  # "tret" + "ILK" ("instance lock key")

# How often a losing process retries while waiting out a possible deploy
# handover. Not itself a setting — TRET_INSTANCE_LOCK_WAIT_SECONDS is the
# knob an operator has reason to tune (a slower handover); how finely that
# budget is polled is not a decision they need to make.
RETRY_INTERVAL_SECONDS = 2.0

WARN_MESSAGE = (
    "another tret instance holds the instance lock; the in-process run event "
    "bus cannot be shared across machines — live run updates will be unreliable"
)

# Session-level (not postgresql.conf-level) keepalive tuning for the lock
# connection specifically — see the module docstring for why. Applied before
# the lock is even attempted, so a crashed prior holder is already on the
# clock by the time this process starts retrying.
KEEPALIVE_STATEMENTS = (
    "SET tcp_keepalives_idle = 15",
    "SET tcp_keepalives_interval = 5",
    "SET tcp_keepalives_count = 3",
)


class InstanceLockError(RuntimeError):
    """Raised at boot (TRET_INSTANCE_LOCK=strict) when another process holds
    the lock and the wait budget has run out."""


async def _set_session_keepalives(conn: AsyncConnection) -> None:
    """Best-effort: not every Postgres (a managed provider fronting its own
    pooler, say) accepts session-level keepalive `SET`s. A refusal must never
    fail boot over what is purely a faster-reaping optimization — the lock
    still works, it just falls back to the server's own (much slower)
    default keepalive timing for a crashed holder.

    These `SET`s are the first statements on a freshly started connection,
    so they open SQLAlchemy's implicit transaction; a refusal aborts that
    transaction server-side (Postgres will bounce every further statement
    with `InFailedSqlTransaction` until it is rolled back), so a refusal
    must roll back before returning — otherwise the `pg_try_advisory_lock`
    attempt right after this call would itself fail, in every
    TRET_INSTANCE_LOCK mode including `warn`. On success, committing closes
    out that same implicit transaction so the advisory lock attempt after it
    starts clean (mirrors the post-lock commit in `acquire_instance_lock`
    below, for the same reason).
    """
    try:
        for statement in KEEPALIVE_STATEMENTS:
            await conn.execute(text(statement))
        await conn.commit()
    except Exception:
        await conn.rollback()
        log.warning(
            "instance lock connection: server refused session-level TCP keepalive "
            "tuning; a crashed prior holder will take the server's own (much slower) "
            "keepalive timeout to be reaped instead of ~30s",
            exc_info=True,
        )


async def _try_lock(conn: AsyncConnection) -> bool:
    """One non-blocking attempt on `conn`. Split out from the retry loop so
    tests can monkeypatch this single call against a fake connection —
    `pg_try_advisory_lock` itself is never exercised by a unit test."""
    result = await conn.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": INSTANCE_LOCK_KEY})
    return bool(result.scalar())


async def _acquire_with_retry(conn: AsyncConnection, wait_seconds: float) -> bool:
    """Try immediately, then every `RETRY_INTERVAL_SECONDS` until `wait_seconds`
    total have elapsed. Pure retry policy over `_try_lock`, so a test can
    verify it (attempt counts, the give-up point) without a real clock or a
    real database — monkeypatch `_try_lock` and pass a fake `conn`.
    """
    elapsed = 0.0
    while True:
        if await _try_lock(conn):
            return True
        if elapsed >= wait_seconds:
            return False
        await asyncio.sleep(RETRY_INTERVAL_SECONDS)
        elapsed += RETRY_INTERVAL_SECONDS


def _verdict(mode: str, held: bool) -> None:
    """Apply TRET_INSTANCE_LOCK once the retry loop above has an answer.

    Split out from `acquire()` so the warn/strict/off dispatch is testable on
    its own, with no connection or engine involved at all.
    """
    if held or mode == "off":
        return
    if mode == "strict":
        raise InstanceLockError(f"{WARN_MESSAGE}. Refusing to start (TRET_INSTANCE_LOCK=strict).")
    log.error(WARN_MESSAGE)  # mode == "warn": logged, boot continues


# The three shapes `acquire_instance_lock()` can hand back. Distinguishing
# "lost" from "not_applicable" is the whole point: both leave `InstanceLock`
# holding no connection, but they mean opposite things to a caller deciding
# whether a prior process's state (its still-`running` rows, say) is safe to
# treat as abandoned. "held": this process holds the lock and owns releasing
# it. "lost": Postgres, TRET_INSTANCE_LOCK=warn, and another process already
# held the key — boot continued (see `_verdict`), but this process must NOT
# assume it is the only one running. "not_applicable": SQLite, or
# TRET_INSTANCE_LOCK=off — the check never ran at all, so there is nothing to
# have lost.
InstanceLockState = Literal["held", "lost", "not_applicable"]


class InstanceLock:
    """What `acquire()` hands the lifespan: `release()` at shutdown, whether
    or not this process actually ended up holding the lock."""

    def __init__(self, conn: AsyncConnection | None, state: InstanceLockState):
        self._conn = conn
        self.state = state

    @property
    def held(self) -> bool:
        """True only for "held". Callers that would otherwise treat "lost" the
        same as "not_applicable" (both have no connection) must check this —
        see `main.py`'s lifespan, which skips the orphaned-run sweep on
        "lost" precisely because it is not the same as "not_applicable"."""
        return self.state == "held"

    async def release(self) -> None:
        """No-op if this process never held the lock (SQLite, TRET_INSTANCE_
        LOCK=off, or a `warn` that lost the race) — `acquire()` already closed
        that connection, if any, before returning.

        Tolerates a connection that has already died or been terminated out
        from under it (the Postgres server restarting, say, or an operator
        killing the session): Postgres itself drops a session-scoped advisory
        lock the instant that connection's underlying socket goes away, so a
        failure here is noisy shutdown logging, never a stuck lock — and it
        must never be allowed to raise out of the lifespan's `finally`.

        The expected shape of that is the process's *own* shutdown having
        already closed this exact connection before the lifespan gets here —
        `pg_advisory_unlock` then raises `InterfaceError` (or a connection-
        invalidated `DBAPIError`, or the raw asyncpg error if it isn't wrapped)
        for a lock Postgres already released the instant the socket went
        away. That expected case is one INFO line, not a traceback — every
        deploy was logging a full traceback for exactly this, drowning out
        real shutdown errors. Anything else is still a WARNING (no traceback)
        so a genuinely new failure mode does not go unnoticed. Either way,
        `conn.close()` still has to run — hence the `finally` below.
        """
        if self._conn is None:
            return
        conn, self._conn = self._conn, None
        warning = (
            "failed to cleanly release the instance lock connection — it was likely "
            "already dead or terminated; Postgres drops a session-scoped advisory lock "
            "on its own once the underlying connection is gone, so this is not a stuck lock: %s"
        )
        try:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": INSTANCE_LOCK_KEY})
            await conn.commit()
        except (InterfaceError, DBAPIError, *ASYNCPG_INTERFACE_ERRORS) as exc:
            already_closed = isinstance(exc, (InterfaceError, *ASYNCPG_INTERFACE_ERRORS)) or (
                isinstance(exc, DBAPIError) and exc.connection_invalidated
            )
            if already_closed:
                log.info(
                    "instance lock connection was already closed at shutdown; the "
                    "session-level advisory lock is already released along with it"
                )
            else:
                log.warning(warning, exc)
        except Exception as exc:
            log.warning(warning, exc)
        finally:
            try:
                await conn.close()
            except Exception as exc:
                log.warning(warning, exc)


async def acquire_instance_lock() -> InstanceLock:
    """Lifespan startup: take the fixed advisory lock, or decide what to do
    without it. Always returns an `InstanceLock` (a no-op one on SQLite, on
    TRET_INSTANCE_LOCK=off, or on a `warn` that never got the lock) so the
    caller can unconditionally `await lock.release()` at shutdown.
    """
    settings = get_settings()
    engine = get_engine()
    if settings.instance_lock == "off" or engine.dialect.name != "postgresql":
        return InstanceLock(None, "not_applicable")

    # engine.connect() returns an unstarted AsyncConnection — `async with`
    # would call this for us, but the connection must outlive that block
    # (it is held for the process lifetime), so it is started explicitly.
    conn = await engine.connect().start()
    try:
        await _set_session_keepalives(conn)
        held = await _acquire_with_retry(conn, settings.instance_lock_wait_seconds)
    except Exception:
        await conn.close()
        raise

    if held:
        # SQLAlchemy 2.0's "begin once" behavior starts an implicit
        # transaction on this connection's first statement (the
        # pg_try_advisory_lock SELECT above) and never ends it on its own —
        # left alone, this connection would sit "idle in transaction" in
        # `pg_stat_activity` for the rest of the process's life, which is a
        # false alarm for any monitoring watching for exactly that. A session-
        # level advisory lock survives a commit (unlike its transaction-level
        # sibling, `pg_advisory_xact_lock`), so this closes that transaction
        # out without releasing the lock it just took — see db/migrate.py:344
        # for the identical move on the schema-upgrade lock.
        await conn.commit()
        # Kept open, never returned to the pool: the lock is session-scoped,
        # so releasing it means closing this exact connection.
        return InstanceLock(conn, "held")

    await conn.close()
    _verdict(settings.instance_lock, held=False)
    return InstanceLock(None, "lost")
