"""`tret storage status|migrate`: the operator tooling, against a fake bucket and sqlite."""
from __future__ import annotations

import hashlib
import sys
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests._fake_s3 import FakeS3
from tests.evals.golden_world import install_sqlite_type_shims
from tret import cli
from tret.db.models import Base, Document
from tret.storage import reset_store
from tret.storage.admin import migrate, status
from tret.storage.local import LocalStore
from tret.storage.s3 import S3Store


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    engine = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _row(path, name, data, **kw) -> Document:
    return Document(
        project_id=uuid.uuid4(),
        filename=name,
        content_type="application/octet-stream",
        byte_size=len(data),
        storage_path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        **kw,
    )


@pytest_asyncio.fixture
async def world(tmp_path, session_factory):
    """Two documents and one package file on a 'volume', rows on the test DB."""
    root = tmp_path / "storage"
    local = LocalStore(root)
    files = {}
    for name, data in (("a.txt", b"alpha bytes"), ("b.pdf", b"%PDF- bravo bytes")):
        key = f"{hashlib.sha256(data).hexdigest()}-{name}"
        await local.put(key, data)
        files[name] = (key, data)
    pkg = b"%PDF- package"
    await local.put("packages/F1/pkg.pdf", pkg)
    files["pkg.pdf"] = ("packages/F1/pkg.pdf", pkg)
    async with session_factory() as db:
        for name, (key, data) in files.items():
            db.add(_row(root / key, name, data))  # no storage_key yet: a pre-migration row
        await db.commit()
    fake = FakeS3()
    store = S3Store(storage_dir=root, bucket="b", client=fake)
    return root, store, fake, files


async def test_dry_run_changes_nothing(world, session_factory):
    _root, store, fake, _files = world
    summary = await migrate(store, session_factory, dry_run=True)
    assert summary["dry_run"] and summary["uploaded"] == 3 and not summary["failures"]
    assert fake.objects == {}
    async with session_factory() as db:
        keys = (await db.execute(select(Document.storage_key))).scalars().all()
    assert keys == [None, None, None]


async def test_a_real_run_uploads_sets_keys_and_a_rerun_skips_everything(world, session_factory):
    root, store, fake, files = world
    summary = await migrate(store, session_factory)
    assert not summary["failures"]
    assert (summary["uploaded"], summary["skipped"], summary["keys_set"]) == (3, 0, 3)
    assert summary["verified"] == 3  # all small: re-downloaded and hashed
    assert not store.cache_root.exists() or not any(store.cache_root.rglob("*"))  # cache untouched
    assert {k: v for k, v in fake.objects.items()} == {key: data for key, data in files.values()}
    async with session_factory() as db:
        rows = (await db.execute(select(Document))).scalars().all()
    assert {r.storage_key for r in rows} == {key for key, _ in files.values()}
    # storage_path is untouched, so the volume copy stays a valid fallback
    assert all(r.storage_path.startswith(str(root)) for r in rows)
    fake.calls.clear()

    again = await migrate(store, session_factory)
    assert (again["uploaded"], again["skipped"], again["keys_set"]) == (0, 3, 0)
    assert "put" not in fake.calls and not again["failures"]


async def test_a_size_mismatch_is_reported_and_fails_the_run_but_not_the_other_files(
    world, session_factory
):
    _root, store, fake, files = world
    fake.corrupt_on_put = True
    summary = await migrate(store, session_factory)
    assert len(summary["failures"]) == 3 and summary["uploaded"] == 0
    assert all("size mismatch" in f for f in summary["failures"])
    async with session_factory() as db:
        keys = (await db.execute(select(Document.storage_key))).scalars().all()
    assert keys == [None, None, None]
    # a clean second attempt repairs it (the wrong-size objects are re-uploaded)
    fake.corrupt_on_put = False
    ok = await migrate(store, session_factory)
    assert not ok["failures"] and ok["uploaded"] == 3
    assert fake.objects[files["a.txt"][0]] == files["a.txt"][1]


async def test_a_missing_local_file_is_a_failure_unless_the_store_already_has_it(
    world, session_factory
):
    root, store, fake, files = world
    key, data = files["a.txt"]
    (root / key).unlink()
    first = await migrate(store, session_factory)
    assert len(first["failures"]) == 1 and "missing locally" in first["failures"][0]
    fake.objects[key] = data  # it made it to the bucket some other way
    second = await migrate(store, session_factory)
    assert not second["failures"]


async def test_delete_local_removes_only_verified_volume_copies(world, session_factory):
    root, store, _fake, files = world
    summary = await migrate(store, session_factory, delete_local=True)
    assert summary["deleted_local"] == 3 and not summary["failures"]
    assert not any((root / key).exists() for key, _ in files.values())


async def test_status_counts_objects_cache_and_key_coverage(world, session_factory):
    _root, store, _fake, _files = world
    before = await status(store, session_factory)
    assert before["backend"] == "s3" and before["objects"] == 0
    assert before["documents"] == {"with_storage_key": 0, "without_storage_key": 3}
    await migrate(store, session_factory)
    after = await status(store, session_factory)
    assert after["objects"] == 3 and after["bytes"] > 0 and after["cache_bytes"] == 0  # migrate leaves the cache empty
    assert after["documents"] == {"with_storage_key": 3, "without_storage_key": 0}
    assert "secret" not in repr(after).lower()


async def test_local_status_walks_the_directory_without_packs_or_cache(tmp_path):
    local = LocalStore(tmp_path)
    await local.put("k-1.txt", b"12345")
    (tmp_path / "packs").mkdir()
    (tmp_path / "packs" / "x.yaml").write_text("zzz")
    info = await status(local, None)
    assert (info["backend"], info["objects"], info["bytes"], info["documents"]) == ("local", 1, 5, None)


# ── the CLI wiring ───────────────────────────────────────────────────────────
def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["tret", *argv])
    cli.main()


def test_cli_status_on_the_local_backend(tmp_path, monkeypatch, capsys):
    from tret.config import get_settings

    reset_store()
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path))
    (tmp_path / "k-1.txt").write_bytes(b"abc")

    def no_db():
        raise RuntimeError("no database in this test")

    monkeypatch.setattr("tret.db.engine.get_session_factory", no_db)
    _run_cli(monkeypatch, "storage", "status")
    out = capsys.readouterr().out
    assert "backend:   local" in out and "objects:   1 (3 B)" in out
    assert "documents: unavailable" in out


def test_cli_migrate_runs_against_the_fake_bucket_and_exits_nonzero_on_failure(
    tmp_path, monkeypatch, capsys, session_factory
):
    import asyncio

    from tret.config import get_settings

    root = tmp_path / "storage"
    data = b"cli bytes"
    key = f"{hashlib.sha256(data).hexdigest()}-c.txt"
    root.mkdir()
    (root / key).write_bytes(data)

    async def seed():
        async with session_factory() as db:
            db.add(_row(root / key, "c.txt", data))
            await db.commit()

    asyncio.run(seed())
    fake = FakeS3()
    store = S3Store(storage_dir=root, bucket="b", client=fake)
    monkeypatch.setattr("tret.storage.get_store", lambda settings: store)
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: session_factory)
    monkeypatch.setattr(get_settings(), "storage_dir", str(root))

    _run_cli(monkeypatch, "storage", "migrate", "--to", "s3", "--dry-run")
    assert fake.objects == {}
    _run_cli(monkeypatch, "storage", "migrate", "--to", "s3")
    assert fake.objects[key] == data
    assert "uploaded:     1" in capsys.readouterr().out

    fake.objects.clear()
    fake.corrupt_on_put = True
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "storage", "migrate", "--to", "s3")
    assert exc.value.code == 1
    assert "FAILED" in capsys.readouterr().err


def test_cli_migrate_requires_an_explicit_target(monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, "storage", "migrate")
    assert exc.value.code == 2


async def test_the_hash_sample_counts_only_large_files(tmp_path, session_factory, monkeypatch):
    import tret.storage.admin as admin

    monkeypatch.setattr(admin, "SMALL_FILE_BYTES", 10)  # "large" is 10+ bytes in this test
    local = LocalStore(tmp_path / "s")
    async with session_factory() as db:
        for i in range(3):
            data = bytes([65 + i]) * 20  # large
            key = f"{hashlib.sha256(data).hexdigest()}-l{i}.bin"
            await local.put(key, data)
            db.add(_row(local.root / key, f"l{i}.bin", data))
        for i in range(2):
            data = bytes([97 + i]) * 3  # small
            key = f"{hashlib.sha256(data).hexdigest()}-s{i}.bin"
            await local.put(key, data)
            db.add(_row(local.root / key, f"s{i}.bin", data))
        await db.commit()
    store = S3Store(storage_dir=tmp_path / "s", bucket="b", client=FakeS3())
    summary = await migrate(store, session_factory, verify_sample=1)
    assert summary["verified"] == 2 + 1  # both small, plus the first large one only


async def test_size_matched_skips_are_not_deleted_by_delete_local(world, session_factory):
    root, store, _fake, files = world
    await migrate(store, session_factory)  # everything uploaded, volume copies kept
    again = await migrate(store, session_factory, delete_local=True)
    assert again["skipped"] == 3 and again["deleted_local"] == 0
    assert all((root / key).exists() for key, _ in files.values())
