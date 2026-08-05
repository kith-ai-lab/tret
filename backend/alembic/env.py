"""Alembic environment. Two callers have to keep working:

* the **CLI** (`alembic upgrade head`), which owns nothing and must open its own
  connection — the app's URL is asyncpg, so that means an async engine driven by
  `asyncio.run`;
* the **app** (`bench.db.migrate.ensure_schema`), which already holds a
  connection inside a running event loop and an advisory lock, and hands it over
  via `config.attributes["connection"]`. Opening our own engine there would
  nest event loops and drop the lock, so a supplied connection always wins.
"""
import asyncio

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from bench.config import get_settings
from bench.db.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    """The configured URL, with the asyncpg dialect and libpq-only params handled."""
    from bench.db.engine import normalize_database_url

    return normalize_database_url(get_settings().database_url)


def run_migrations_offline() -> None:
    context.configure(
        url=_url().replace("+asyncpg", ""),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        # Programmatic path: a sync-facade Connection owned by the caller, who is
        # also responsible for committing it.
        do_run_migrations(connection)
    else:
        asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
