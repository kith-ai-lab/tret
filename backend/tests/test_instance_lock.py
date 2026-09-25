"""tret/services/instance_lock.py: the retry policy and the warn/strict/off
verdict, both independent of a real Postgres connection.

`_acquire_with_retry` is exercised against a fake connection object with
`_try_lock` monkeypatched — no SQL, no event loop time actually spent waiting
(the retry interval is patched to 0 so the test suite doesn't sleep for real).
`_verdict` takes no connection at all. `acquire_instance_lock`'s own
Postgres-only gate is checked against a real (but unconnected) SQLite engine,
which never reaches the network either.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from tret.config import Settings, get_settings
from tret.services import instance_lock as il


def test_default_wait_is_ninety_seconds():
    """90s = a normal Fly handover plus the ~30s the keepalive tuning below
    takes to reap a crashed prior holder's dangling connection."""
    assert Settings().instance_lock_wait_seconds == 90.0


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """No test here should spend real wall-clock time on the poll interval —
    the interval's *value* still matters for the elapsed-time bookkeeping
    (see test_retry_count_matches_the_wait_budget), so this stubs the actual
    wait rather than zeroing the interval itself."""
    monkeypatch.setattr(il, "RETRY_INTERVAL_SECONDS", 0)

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(il.asyncio, "sleep", instant_sleep)


# ── the retry loop ────────────────────────────────────────────────────────────
async def test_succeeds_on_the_first_try_with_no_retries(monkeypatch):
    calls = []

    async def fake_try_lock(conn):
        calls.append(conn)
        return True

    monkeypatch.setattr(il, "_try_lock", fake_try_lock)
    held = await il._acquire_with_retry("fake-conn", wait_seconds=30.0)

    assert held is True
    assert calls == ["fake-conn"]


async def test_retries_until_a_later_attempt_succeeds(monkeypatch):
    """Loses twice, wins the third try — well inside the wait budget."""
    results = iter([False, False, True])
    calls = []

    async def fake_try_lock(conn):
        calls.append(conn)
        return next(results)

    monkeypatch.setattr(il, "_try_lock", fake_try_lock)
    held = await il._acquire_with_retry("fake-conn", wait_seconds=30.0)

    assert held is True
    assert len(calls) == 3


async def test_gives_up_once_the_wait_budget_is_exhausted(monkeypatch):
    """Never succeeds; wait_seconds=0 means exactly one attempt before giving
    up — the first failure already has elapsed (0.0) >= wait_seconds (0.0)."""
    calls = []

    async def always_fails(conn):
        calls.append(conn)
        return False

    monkeypatch.setattr(il, "_try_lock", always_fails)
    held = await il._acquire_with_retry("fake-conn", wait_seconds=0.0)

    assert held is False
    assert len(calls) == 1


async def test_retry_count_matches_the_wait_budget(monkeypatch):
    """wait_seconds=2 with a (patched) 1s interval: attempts at t=0, t=1, t=2 —
    three tries, the last one landing exactly on the budget, before giving up."""
    monkeypatch.setattr(il, "RETRY_INTERVAL_SECONDS", 1.0)
    calls = []

    async def always_fails(conn):
        calls.append(conn)
        return False

    monkeypatch.setattr(il, "_try_lock", always_fails)
    held = await il._acquire_with_retry("fake-conn", wait_seconds=2.0)

    assert held is False
    assert len(calls) == 3


# ── the warn/strict/off verdict ────────────────────────────────────────────────
def test_held_lock_never_raises_or_logs_regardless_of_mode(caplog):
    for mode in ("warn", "strict", "off"):
        with caplog.at_level(logging.ERROR, logger="tret.instance_lock"):
            il._verdict(mode, held=True)  # must not raise
        assert il.WARN_MESSAGE not in caplog.text


def test_off_mode_is_silent_when_the_lock_was_not_held(caplog):
    with caplog.at_level(logging.ERROR, logger="tret.instance_lock"):
        il._verdict("off", held=False)  # must not raise
    assert il.WARN_MESSAGE not in caplog.text


def test_warn_mode_logs_at_error_and_does_not_raise(caplog):
    with caplog.at_level(logging.ERROR, logger="tret.instance_lock"):
        il._verdict("warn", held=False)  # must not raise
    assert il.WARN_MESSAGE in caplog.text


def test_strict_mode_raises_instance_lock_error():
    with pytest.raises(il.InstanceLockError, match="another tret instance holds"):
        il._verdict("strict", held=False)


# ── acquire_instance_lock: the non-Postgres and TRET_INSTANCE_LOCK=off gates ──
@pytest.fixture()
def sqlite_engine(monkeypatch):
    """A real engine, never actually connected to — `acquire_instance_lock`
    must bail out on the dialect check before any SQL is attempted."""
    engine = create_async_engine("sqlite+aiosqlite://")
    monkeypatch.setattr(il, "get_engine", lambda: engine)
    yield engine


@pytest.fixture(autouse=True)
def _reset_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_sqlite_is_a_silent_no_op(sqlite_engine, monkeypatch):
    monkeypatch.setattr(il, "get_settings", lambda: Settings(instance_lock="strict"))
    lock = await il.acquire_instance_lock()
    assert lock._conn is None
    assert lock.state == "not_applicable"
    assert lock.held is False
    await lock.release()  # must not raise even though nothing was ever held


async def test_instance_lock_off_skips_even_on_postgres_dialect(monkeypatch):
    """off must short-circuit before the dialect is even inspected — a fake
    engine whose .dialect access would fail proves the check never fires."""

    class _ExplodingEngine:
        @property
        def dialect(self):
            raise AssertionError("dialect should never be inspected when off")

    monkeypatch.setattr(il, "get_engine", lambda: _ExplodingEngine())
    monkeypatch.setattr(il, "get_settings", lambda: Settings(instance_lock="off"))
    lock = await il.acquire_instance_lock()
    assert lock._conn is None
    assert lock.state == "not_applicable"
    assert lock.held is False


# ── acquire_instance_lock on Postgres: "held" vs "lost" ────────────────────────
class _FakeResult:
    """Stands in for the `CursorResult` `conn.execute()` returns — only
    `.scalar()` is ever read, by `_try_lock`."""

    def __init__(self, value):
        self._value = value

    def scalar(self):
        return self._value


class _FakeConn:
    """Stands in for the dedicated AsyncConnection `acquire_instance_lock`
    keeps for the process lifetime. `_try_lock` is monkeypatched in most
    tests that use this, so `execute` is never asked to actually answer
    `pg_try_advisory_lock` — only `release()`'s own unlock statement reaches
    it for real. A couple of tests below exercise the real (unpatched)
    `_try_lock` instead, so `execute` answers `pg_try_advisory_lock` with a
    real-shaped `_FakeResult(True)`.

    Models real Postgres transaction semantics closely enough to catch the
    bug `_set_session_keepalives`'s rollback fixes: once a statement raises,
    the (implicit) transaction is left aborted, and every further statement
    on this same connection also raises — with the same
    "InFailedSqlTransaction"-shaped message a real refusal leaves behind —
    until something calls `rollback()`. `raise_on={"execute"}` fails every
    execute unconditionally (for tests that only ever issue one and don't
    care about this distinction); `raise_on={"keepalive_set"}` fails only a
    statement whose text names a keepalive GUC, letting a later, different
    statement on the same (rolled-back) connection succeed — the shape the
    real refusal takes.
    """

    def __init__(self, *, raise_on: set[str] | None = None):
        self.calls: list[str] = []
        self.executed_sql: list[str] = []
        self._raise_on = raise_on or set()
        self._failed = False

    async def execute(self, stmt, params=None):
        self.calls.append("execute")
        sql = str(stmt)
        self.executed_sql.append(sql)
        if self._failed:
            raise RuntimeError("current transaction is aborted, commands ignored until end of transaction block")
        if "execute" in self._raise_on or ("keepalive_set" in self._raise_on and "tcp_keepalives" in sql):
            self._failed = True
            raise RuntimeError("server closed the connection unexpectedly")
        if "pg_try_advisory_lock" in sql:
            return _FakeResult(True)
        return _FakeResult(None)

    async def commit(self):
        self.calls.append("commit")
        if "commit" in self._raise_on:
            raise RuntimeError("server closed the connection unexpectedly")

    async def rollback(self):
        self.calls.append("rollback")
        self._failed = False
        if "rollback" in self._raise_on:
            raise RuntimeError("server closed the connection unexpectedly")

    async def close(self):
        self.calls.append("close")
        if "close" in self._raise_on:
            raise RuntimeError("server closed the connection unexpectedly")


class _FakeConnectable:
    """`engine.connect()`'s return value: unstarted until `.start()` is
    awaited, exactly like the real `AsyncConnection` `acquire_instance_lock`
    depends on (see its own comment on why `async with` is not used)."""

    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def start(self) -> _FakeConn:
        return self._conn


class _FakePostgresEngine:
    class dialect:
        name = "postgresql"

    def __init__(self, conn: _FakeConn):
        self._conn = conn

    def connect(self) -> _FakeConnectable:
        return _FakeConnectable(self._conn)


# ── session keepalive tuning ────────────────────────────────────────────────
async def test_set_session_keepalives_issues_all_three_statements():
    """The exact SQL text `_set_session_keepalives` sends, in order — this is
    what makes a crashed prior holder's dangling connection get reaped by
    Postgres in well under a minute instead of its own ~2 hour default. On
    success it must also commit, closing out the implicit transaction those
    SETs opened (see test_a_held_lock_commits_to_end_the_implicit_transaction
    for why leaving it open is a problem)."""
    conn = _FakeConn()
    await il._set_session_keepalives(conn)
    assert conn.executed_sql == list(il.KEEPALIVE_STATEMENTS)
    assert conn.calls == ["execute", "execute", "execute", "commit"]


async def test_a_refused_keepalive_set_is_tolerated(caplog):
    """Some Postgres (a managed provider fronting its own pooler, say) may
    reject a session-level keepalive SET. That must never fail boot — it
    just falls back to the server's own, much slower, default reaping."""
    conn = _FakeConn(raise_on={"execute"})
    with caplog.at_level(logging.WARNING, logger="tret.instance_lock"):
        await il._set_session_keepalives(conn)  # must not raise
    assert "refused session-level TCP keepalive" in caplog.text
    # The refused SET is the connection's first statement, so it opens (and,
    # left alone, would leave aborted) an implicit transaction — this rolls
    # it back rather than just logging and moving on.
    assert conn.calls == ["execute", "rollback"]


async def test_a_refused_keepalive_set_does_not_poison_the_lock_attempt(monkeypatch):
    """The bug this fixes: `_set_session_keepalives`'s SETs are the first
    statements on a freshly started connection, so a refusal aborts the
    implicit transaction they opened. Without the rollback above, every
    further statement on this exact connection — including the very next
    `pg_try_advisory_lock` attempt — would fail with
    `InFailedSqlTransactionError`, killing boot in every TRET_INSTANCE_LOCK
    mode, including `warn` and `off`. With the rollback, `_acquire_with_retry`
    on this same (rolled-back) connection still acquires normally.
    """
    conn = _FakeConn(raise_on={"keepalive_set"})

    await il._set_session_keepalives(conn)
    assert "rollback" in conn.calls  # refusal was rolled back...

    held = await il._acquire_with_retry(conn, wait_seconds=0.0)  # ...real _try_lock, not monkeypatched
    assert held is True
    assert conn.calls[-1] == "execute"  # the (successful) pg_try_advisory_lock select

    # Mirrors what acquire_instance_lock() itself does right after a held
    # lock: commit to end the implicit transaction the select above opened.
    await conn.commit()
    assert conn.calls[-1] == "commit"


async def test_a_held_lock_commits_to_end_the_implicit_transaction(monkeypatch):
    """SQLAlchemy 2.0's begin-once behavior opens an implicit transaction on
    this connection's first statement (the pg_try_advisory_lock SELECT) and
    never closes it — left alone, the connection sits "idle in transaction"
    for the rest of the process's life. A session-level advisory lock
    survives a commit, so `acquire()` must issue one right after a successful
    acquire (db/migrate.py:344 does the same for the schema lock).
    """
    async def _always_wins(_conn):
        return True

    conn = _FakeConn()
    monkeypatch.setattr(il, "get_engine", lambda: _FakePostgresEngine(conn))
    monkeypatch.setattr(il, "get_settings", lambda: Settings(instance_lock="warn"))
    monkeypatch.setattr(il, "_try_lock", _always_wins)

    lock = await il.acquire_instance_lock()

    assert lock.state == "held"
    assert lock.held is True
    assert lock._conn is conn
    # The three keepalive SETs (issued before the lock is even attempted,
    # since `_try_lock` is monkeypatched here and never touches `conn`
    # itself), then `_set_session_keepalives`'s own commit ending that
    # implicit transaction, then the lock's own commit — which would
    # otherwise be ending the implicit transaction the (monkeypatched, so
    # invisible to `conn`) advisory-lock select opened, but here is really
    # just a second consecutive commit on an already-clean connection.
    assert conn.calls == ["execute", "execute", "execute", "commit", "commit"]
    assert conn.executed_sql == list(il.KEEPALIVE_STATEMENTS)


async def test_a_lost_lock_in_warn_mode_is_state_lost_not_not_applicable(monkeypatch):
    """The bug this fixes: `InstanceLock(None)` looked identical whether the
    lock was never attempted (SQLite, TRET_INSTANCE_LOCK=off) or attempted and
    LOST to another live process (Postgres, warn mode). Only the second one
    means a second instance may still be running — see main.py's lifespan,
    which must sweep orphaned runs for the first case and skip it for this
    one.
    """
    async def _always_loses(_conn):
        return False

    conn = _FakeConn()
    monkeypatch.setattr(il, "get_engine", lambda: _FakePostgresEngine(conn))
    monkeypatch.setattr(
        il, "get_settings", lambda: Settings(instance_lock="warn", instance_lock_wait_seconds=0.0)
    )
    monkeypatch.setattr(il, "RETRY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(il, "_try_lock", _always_loses)

    lock = await il.acquire_instance_lock()

    assert lock.state == "lost"
    assert lock.held is False
    assert lock._conn is None
    assert "close" in conn.calls  # the losing connection is not kept around


# ── InstanceLock.release ──────────────────────────────────────────────────────
async def test_release_is_a_no_op_with_no_connection():
    lock = il.InstanceLock(None, "not_applicable")
    await lock.release()  # must not raise


async def test_release_unlocks_commits_and_closes_the_connection():
    conn = _FakeConn()
    lock = il.InstanceLock(conn, "held")
    await lock.release()

    assert conn.calls == ["execute", "commit", "close"]
    # A second release must be a genuine no-op, not a double-close.
    conn.calls.clear()
    await lock.release()
    assert conn.calls == []


@pytest.mark.parametrize("raise_on", [{"execute"}, {"commit"}, {"close"}])
async def test_release_tolerates_a_dead_connection(caplog, raise_on):
    """Shutdown's `finally` (main.py) must never raise here: whichever step
    the connection died on (a Postgres restart, an operator killing the
    session — the exact statement it happened to be mid-flight on varies),
    Postgres has already dropped the session-scoped advisory lock on its own
    once the underlying connection went away. This is noisy logging, not a
    correctness problem.
    """
    conn = _FakeConn(raise_on=raise_on)
    lock = il.InstanceLock(conn, "held")

    with caplog.at_level(logging.WARNING, logger="tret.instance_lock"):
        await lock.release()  # must not raise

    assert "failed to cleanly release the instance lock" in caplog.text
    # Still a genuine one-shot: a second call must not try again.
    caplog.clear()
    await lock.release()
    assert caplog.text == ""


class _FakeReleaseConn:
    """A lock connection at shutdown whose `execute` raises one specific
    exception (or none) — simpler than `_FakeConn` above since these tests
    don't care about implicit-transaction bookkeeping, only which exception
    from the unlock statement gets which log level, and that `close()` still
    runs regardless."""

    def __init__(self, exc: Exception | None):
        self._exc = exc
        self.calls: list[str] = []

    async def execute(self, stmt, params=None):
        self.calls.append("execute")
        if self._exc is not None:
            raise self._exc
        return _FakeResult(None)

    async def commit(self):
        self.calls.append("commit")

    async def close(self):
        self.calls.append("close")


async def test_release_tolerates_an_already_closed_lock_connection(caplog):
    """The bug this fixes: the lock connection is held for the process's
    whole life and closed by the time shutdown's unlock runs, so
    `pg_advisory_unlock` raises `InterfaceError` for a lock Postgres already
    released the instant that socket died — every deploy logged a full
    traceback for exactly this. That expected case is one INFO line, and
    cleanup (`close()`) still has to run even though the unlock itself
    failed."""
    from sqlalchemy.exc import InterfaceError

    conn = _FakeReleaseConn(InterfaceError("connection is closed", None, None))
    lock = il.InstanceLock(conn, "held")

    with caplog.at_level(logging.INFO, logger="tret.instance_lock"):
        await lock.release()  # must not raise

    assert "already closed" in caplog.text
    assert "Traceback" not in caplog.text
    assert conn.calls == ["execute", "close"]  # commit skipped, cleanup still ran


async def test_release_still_unlocks_and_cleans_up_normally():
    conn = _FakeReleaseConn(None)
    lock = il.InstanceLock(conn, "held")

    await lock.release()

    assert conn.calls == ["execute", "commit", "close"]


async def test_release_logs_a_warning_not_info_for_an_unrelated_error(caplog):
    """An exception that isn't the closed-connection family is a real,
    possibly-new failure mode — it must stay visible at WARNING (just without
    the traceback noise), not get swallowed at INFO alongside the expected
    case."""
    conn = _FakeReleaseConn(RuntimeError("boom"))
    lock = il.InstanceLock(conn, "held")

    with caplog.at_level(logging.INFO, logger="tret.instance_lock"):
        await lock.release()  # must not raise

    assert any(r.levelname == "WARNING" for r in caplog.records)
    assert "boom" in caplog.text
    assert "Traceback" not in caplog.text
    assert conn.calls == ["execute", "close"]  # commit skipped, cleanup still ran
