"""A UUID column must not get NUMERIC affinity on SQLite: an all-digit uuid4 would be
stored as a number and read back as a float (a random CI failure, about 1 run in 20).
Production is PostgreSQL, whose DDL must stay `UUID`."""
from __future__ import annotations

import uuid

from sqlalchemy import Column, MetaData, Table, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.schema import CreateTable

import tret.db.models  # noqa: F401 - registers the compile hook

meta = MetaData()
things = Table("things", meta, Column("id", UUID(as_uuid=True), primary_key=True))


def test_sqlite_ddl_is_char_and_postgres_ddl_is_unchanged():
    assert "CHAR(32)" in str(CreateTable(things).compile(dialect=sqlite.dialect()))
    pg = str(CreateTable(things).compile(dialect=postgresql.dialect()))
    assert "UUID" in pg and "CHAR" not in pg


async def test_all_digit_uuids_round_trip_on_sqlite(tmp_path):
    ids = [uuid.UUID("12345678901234567890123456789012"), uuid.UUID("1234567890123456789012345678e012")]
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(meta.create_all)
        for i in ids:
            await conn.execute(things.insert().values(id=i))
        got = (await conn.execute(select(things.c.id))).scalars().all()
    await engine.dispose()
    assert sorted(got) == sorted(ids)
