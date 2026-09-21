"""`db.engine.get_engine()`'s explicit pool sizing (2026-09-20 — parallel
delegation makes the pool load-bearing, see the comment in `tret/db/engine.py`).

No real connection is made: `create_async_engine` builds the pool object
eagerly but lazily, so its `pool_size`/`max_overflow` can be asserted without
ever dialing out. The module globals are swapped the same way
`tests/test_run_harness_task_selection.py`'s `db` fixture does it, so this
can't leak a live engine into another test.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import tret.db.engine as db_engine


@pytest.fixture()
def _reset_engine_globals():
    saved = (db_engine._engine, db_engine._session_factory)
    db_engine._engine, db_engine._session_factory = None, None
    yield
    db_engine._engine, db_engine._session_factory = saved


async def test_postgres_url_gets_explicit_pool_settings(monkeypatch, _reset_engine_globals):
    settings = SimpleNamespace(
        database_url="postgresql+asyncpg://u:p@localhost:5432/x",
        db_pool_size=3,
        db_max_overflow=7,
    )
    monkeypatch.setattr(db_engine, "get_settings", lambda: settings)

    engine = db_engine.get_engine()
    try:
        assert engine.pool.size() == 3
        assert engine.pool._max_overflow == 7
    finally:
        await engine.dispose()


async def test_sqlite_url_still_constructs_without_pool_kwargs(monkeypatch, _reset_engine_globals):
    settings = SimpleNamespace(
        database_url="sqlite+aiosqlite:///:memory:",
        db_pool_size=3,
        db_max_overflow=7,
    )
    monkeypatch.setattr(db_engine, "get_settings", lambda: settings)

    # sqlite's async pool class rejects pool_size/max_overflow as unknown
    # kwargs, so this would raise a TypeError if the Postgres-only guard in
    # `get_engine()` ever regressed to passing them unconditionally.
    engine = db_engine.get_engine()
    try:
        assert engine is not None
    finally:
        await engine.dispose()
