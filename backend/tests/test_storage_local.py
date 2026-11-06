"""`tret.storage` local backend: byte-for-byte the layout tret always had."""
from __future__ import annotations

import hashlib
import os
import stat
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tret.storage import (
    batch_record_key,
    describe_line,
    document_bytes,
    document_key,
    document_local_path,
    get_store,
    key_for_document,
    package_key,
    reset_store,
    validate_key,
)
from tret.storage.local import LocalStore

SHA = hashlib.sha256(b"x").hexdigest()


@pytest.fixture
def store(tmp_path) -> LocalStore:
    return LocalStore(tmp_path / "storage")


async def test_put_get_exists_local_path_round_trip(store):
    key = f"{SHA}-notes.txt"
    assert not await store.exists(key)
    assert await store.size(key) is None
    await store.put(key, b"hello")
    assert await store.exists(key)
    assert await store.get(key) == b"hello"
    assert await store.size(key) == 5
    path = await store.local_path(key)
    assert path == store.root / key
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert sorted(p.name for p in store.root.iterdir()) == [key]


async def test_a_same_size_content_addressed_file_is_kept_not_rewritten(store):
    key = f"{SHA}-a.txt"
    await store.put(key, b"same bytes")
    path = await store.local_path(key)
    os.chmod(path, 0o444)
    before = path.stat()
    try:
        await store.put(key, b"same bytes")
        after = path.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    finally:
        os.chmod(path, 0o644)


async def test_other_keys_are_always_replaced_even_at_the_same_size(store):
    key = "packages/f1/out.pdf"
    await store.put(key, b"AAAA")
    await store.put(key, b"BBBB")
    assert await store.get(key) == b"BBBB"


async def test_a_failed_write_leaves_no_temp_debris(store, monkeypatch):
    def boom(src, dst):
        raise OSError("disk on fire")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        await store.put(f"{SHA}-b.txt", b"data")
    monkeypatch.undo()
    assert [p.name for p in store.root.iterdir()] == []


async def test_put_of_a_path_that_is_the_target_is_a_no_op(store):
    key = "packages/f1/out.pdf"
    target = store.staging_dir("packages/f1") / "out.pdf"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"packager wrote me")
    before = target.stat()
    await store.put(key, target)
    after = target.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert target.read_bytes() == b"packager wrote me"


async def test_put_of_another_path_copies_and_move_renames(store, tmp_path):
    src = tmp_path / "elsewhere.bin"
    src.write_bytes(b"payload")
    await store.put("a-copy.bin", src)
    assert src.exists() and (await store.get("a-copy.bin")) == b"payload"
    await store.put("a-move.bin", src, move=True)
    assert not src.exists() and (await store.get("a-move.bin")) == b"payload"
    assert not [p for p in store.root.iterdir() if p.name.startswith(".")]


async def test_delete_is_best_effort(store):
    await store.put("k.txt", b"1")
    await store.delete("k.txt")
    await store.delete("k.txt")
    assert not await store.exists("k.txt")


async def test_get_of_a_missing_key_is_file_not_found(store):
    with pytest.raises(FileNotFoundError):
        await store.get("nope.txt")


@pytest.mark.parametrize(
    "bad", ["", "/abs/path", "../escape", "a/../b", "a//b", "./x", "a/./b", "a\\b", "x\x00y", "a/.."]
)
def test_key_validation_rejects_traversal_and_absolute_keys(bad):
    with pytest.raises(ValueError):
        validate_key(bad)


async def test_the_store_refuses_a_key_that_resolves_outside_its_root(store, tmp_path):
    (store.root).mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    (store.root / "link").symlink_to(tmp_path / "outside")
    with pytest.raises(ValueError):
        await store.put("link/file.txt", b"x")
    with pytest.raises(ValueError):
        await store.put("../x", b"x")


def test_document_key_is_flat():
    assert document_key(SHA, "a.pdf") == f"{SHA}-a.pdf"
    with pytest.raises(ValueError):
        document_key(SHA, "../a.pdf")
    assert package_key("fid", "x.pdf") == "packages/fid/x.pdf"
    assert batch_record_key("bid", "r.json") == "batches/bid/r.json"


def test_key_for_document_prefers_the_column_then_derives_from_the_path():
    assert key_for_document(SimpleNamespace(storage_key="k-1", storage_path="/x/y")) == "k-1"
    legacy_doc = SimpleNamespace(storage_key=None, storage_path=f"/data/storage/{SHA}-a.pdf")
    assert key_for_document(legacy_doc) == f"{SHA}-a.pdf"
    legacy_pkg = SimpleNamespace(storage_key=None, storage_path="/data/storage/packages/F-1/doc.pdf")
    assert key_for_document(legacy_pkg) == "packages/F-1/doc.pdf"
    # the last /packages/ wins, exactly like the migration's greedy regex
    nested = SimpleNamespace(storage_key=None, storage_path="/data/packages/storage/packages/F/x.pdf")
    assert key_for_document(nested) == "packages/F/x.pdf"


async def test_document_helpers_on_the_local_backend_use_storage_path_as_is(tmp_path, monkeypatch):
    from tret.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "s"))
    f = tmp_path / "anywhere.bin"
    f.write_bytes(b"abc")
    doc = SimpleNamespace(storage_key=None, storage_path=str(f))
    assert await document_local_path(doc) == f
    assert await document_bytes(doc) == b"abc"


def test_get_store_follows_patched_settings_and_treats_a_namespace_as_local(tmp_path):
    reset_store()
    a = get_store(SimpleNamespace(storage_dir=str(tmp_path / "a")))
    b = get_store(SimpleNamespace(storage_dir=str(tmp_path / "b")))
    assert a.backend == b.backend == "local"
    assert a.root != b.root
    assert get_store(SimpleNamespace(storage_dir=str(tmp_path / "a"))) is a  # keyed on values
    assert "storage: local dir=" in describe_line(a)


def test_get_store_rejects_an_unknown_backend_and_a_bucketless_s3(tmp_path, monkeypatch):
    monkeypatch.delenv("BUCKET_NAME", raising=False)
    with pytest.raises(RuntimeError, match="not one of"):
        get_store(SimpleNamespace(storage_dir=str(tmp_path), storage_backend="gcs"))
    with pytest.raises(RuntimeError, match="needs a bucket"):
        get_store(SimpleNamespace(storage_dir=str(tmp_path), storage_backend="s3"))


async def test_list_keys_skips_packs_cache_and_spool_files(store):
    await store.put(f"{SHA}-a.txt", b"1")
    await store.put("packages/f/x.pdf", b"2")
    for hidden in ("packs/p/pack.yaml", "cache/k", ".incoming-1.part"):
        path = store.root / hidden
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"z")
    keys = [k async for k in store.list_keys()]
    assert keys == sorted([f"{SHA}-a.txt", "packages/f/x.pdf"])
    assert [k async for k in store.list_keys("packages/")] == ["packages/f/x.pdf"]


async def test_the_new_store_does_not_change_what_lands_on_disk(tmp_path):
    root = tmp_path / "storage"
    store = LocalStore(root)
    before = time.time()
    await store.put(f"{SHA}-n.txt", b"v")
    assert [p.name for p in Path(root).iterdir()] == [f"{SHA}-n.txt"]
    assert Path(root, f"{SHA}-n.txt").stat().st_mtime >= before - 1


async def test_exclusive_put_creates_once_and_never_touches_an_existing_file(store):
    key = f"{SHA}-x.txt"
    assert await store.put(key, b"first", exclusive=True) is True
    path = await store.local_path(key)
    before = path.stat()
    assert await store.put(key, b"second!", exclusive=True) is False
    after = path.stat()
    assert path.read_bytes() == b"first"
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert sorted(p.name for p in store.root.iterdir()) == [key]  # no temp debris
    assert await store.put(key, b"plain put returns True") is True


async def test_exclusive_put_falls_back_to_open_xb_without_hard_links(store, monkeypatch):
    def nolink(src, dst):
        raise OSError("hard links not supported")

    monkeypatch.setattr(os, "link", nolink)
    assert await store.put("a.bin", b"1", exclusive=True) is True
    assert await store.put("a.bin", b"2", exclusive=True) is False
    assert await store.get("a.bin") == b"1"


async def test_local_pinning_is_a_no_op(store, tmp_path):
    store.pin(tmp_path)
    store.unpin(tmp_path)
    async with store.pinned(tmp_path):
        pass
