"""`tret.storage` s3 backend, against an in-memory fake client."""
from __future__ import annotations

import hashlib
import os
import time
from types import SimpleNamespace

import pytest

from tests._fake_s3 import FakeS3
from tret.storage import describe_line, document_bytes, document_local_path, get_store, reset_store
from tret.storage.s3 import S3Store

SHA = hashlib.sha256(b"x").hexdigest()


def make(tmp_path, client=None, **kw) -> tuple[S3Store, FakeS3]:
    fake = client or FakeS3()
    return S3Store(storage_dir=tmp_path / "storage", bucket="b", client=fake, **kw), fake


def age(path, seconds=3600):
    t = time.time() - seconds
    os.utime(path, (t, t))


async def test_put_uploads_and_leaves_the_bytes_in_the_cache(tmp_path):
    store, fake = make(tmp_path)
    key = f"{SHA}-a.txt"
    await store.put(key, b"hello", content_type="text/plain")
    assert fake.objects[key] == b"hello"
    assert fake.content_types[key] == "text/plain"
    assert (store.cache_root / key).read_bytes() == b"hello"
    fake.calls.clear()
    assert await store.get(key) == b"hello"
    assert "get" not in fake.calls  # served from the cache


async def test_get_on_a_cache_miss_downloads_and_caches(tmp_path):
    store, fake = make(tmp_path)
    fake.objects["k.bin"] = b"remote"
    assert not (store.cache_root / "k.bin").exists()
    assert await store.get("k.bin") == b"remote"
    assert (store.cache_root / "k.bin").read_bytes() == b"remote"
    fake.calls.clear()
    await store.get("k.bin")
    assert fake.calls == []


async def test_local_path_stages_a_real_file_and_missing_keys_raise(tmp_path):
    store, fake = make(tmp_path)
    fake.objects["packages/f/x.pdf"] = b"%PDF-"
    path = await store.local_path("packages/f/x.pdf")
    assert path == store.cache_root / "packages/f/x.pdf" and path.read_bytes() == b"%PDF-"
    with pytest.raises(FileNotFoundError):
        await store.local_path("nope")


async def test_a_path_put_streams_the_file_and_move_consumes_it(tmp_path):
    store, fake = make(tmp_path)
    spool = tmp_path / "spool.part"
    spool.write_bytes(b"uploaded")
    await store.put("u.bin", spool, move=True)
    assert fake.objects["u.bin"] == b"uploaded"
    assert not spool.exists() and (store.cache_root / "u.bin").read_bytes() == b"uploaded"


async def test_exists_and_size_treat_404_as_absent_and_other_errors_as_errors(tmp_path):
    store, fake = make(tmp_path)
    assert await store.exists("nope") is False
    assert await store.size("nope") is None
    fake.objects["yes"] = b"123"
    assert await store.exists("yes") and await store.size("yes") == 3

    from botocore.exceptions import ClientError

    def deny(**_kw):
        raise ClientError({"Error": {"Code": "403", "Message": "denied"}}, "HeadObject")

    fake.head_object = deny
    with pytest.raises(ClientError):
        await store.exists("yes")


async def test_eviction_removes_oldest_first_and_never_the_returned_file(tmp_path):
    store, fake = make(tmp_path, cache_max_mb=1)
    mb = 1024 * 1024
    for name, size in (("old", 400_000), ("mid", 400_000), ("new", 400_000)):
        fake.objects[name] = b"x" * size
    for name, seconds in (("old", 3000), ("mid", 2000)):
        await store.local_path(name)
        age(store.cache_root / name, seconds)
    # Staging a third file pushes the cache past 1 MB: "old" goes, "mid" stays.
    path = await store.local_path("new")
    assert path.exists()
    assert not (store.cache_root / "old").exists()
    assert (store.cache_root / "mid").exists()
    assert store.cache_size_bytes() <= mb
    # The file just returned survives even when it alone exceeds the cap.
    store.cache_max_mb = 0.1
    age(path)
    again = await store.local_path("mid")
    assert again.exists()


async def test_fresh_cache_files_get_a_grace_period(tmp_path):
    store, fake = make(tmp_path, cache_max_mb=1)
    fake.objects["a"] = b"x" * 900_000
    fake.objects["b"] = b"x" * 900_000
    await store.local_path("a")
    await store.local_path("b")
    assert (store.cache_root / "a").exists()  # too fresh to evict, so the cap is soft


async def test_delete_removes_the_object_and_its_cache_file(tmp_path):
    store, fake = make(tmp_path)
    await store.put("k", b"1")
    await store.delete("k")
    assert "k" not in fake.objects and not (store.cache_root / "k").exists()


async def test_list_keys_and_objects_paginate(tmp_path):
    store, fake = make(tmp_path, client=FakeS3(page_size=2))
    for i in range(5):
        fake.objects[f"d/{i}"] = b"x" * i
    fake.objects["other"] = b"y"
    assert [k async for k in store.list_keys("d/")] == [f"d/{i}" for i in range(5)]
    assert sum([s async for _k, s in store.iter_objects()]) == 10 + 1


async def test_read_through_bypasses_the_cache(tmp_path):
    store, fake = make(tmp_path)
    await store.put("k", b"good")
    (store.cache_root / "k").write_bytes(b"stale")
    assert await store.read_through("k") == b"good"


def test_staging_dir_maps_a_prefix_into_the_cache(tmp_path):
    store, _ = make(tmp_path)
    assert store.staging_dir("packages/f1") == store.cache_root / "packages/f1"
    with pytest.raises(ValueError):
        store.staging_dir("../x")


def test_settings_and_the_fly_fallback_env_vars(tmp_path, monkeypatch):
    reset_store()
    monkeypatch.setenv("BUCKET_NAME", "fly-bucket")
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://fly.storage.tigris.dev")
    monkeypatch.setenv("AWS_REGION", "auto")
    store = get_store(SimpleNamespace(storage_dir=str(tmp_path), storage_backend="s3"))
    d = store.describe()
    assert (d["bucket"], d["endpoint_url"], d["region"]) == (
        "fly-bucket", "https://fly.storage.tigris.dev", "auto"
    )
    assert "AWS_SECRET" not in repr(d)
    assert "bucket=fly-bucket" in describe_line(store)
    # explicit settings beat the fallbacks
    explicit = get_store(
        SimpleNamespace(
            storage_dir=str(tmp_path), storage_backend="s3", storage_s3_bucket="mine",
            storage_s3_endpoint_url="http://localhost:9000", storage_s3_region="us-east-1",
            storage_cache_max_mb=7,
        )
    )
    assert explicit.describe()["bucket"] == "mine" and explicit.cache_max_mb == 7
    reset_store()


async def test_document_helpers_stage_from_the_store_and_fall_back_to_the_volume(tmp_path):
    reset_store()
    settings = SimpleNamespace(storage_dir=str(tmp_path), storage_backend="s3", storage_s3_bucket="b")
    store = get_store(settings)
    store._client = fake = FakeS3()
    fake.objects[f"{SHA}-a.txt"] = b"in bucket"
    doc = SimpleNamespace(storage_key=f"{SHA}-a.txt", storage_path=str(tmp_path / "old-volume-path"))
    assert (await document_local_path(doc, settings)).read_bytes() == b"in bucket"
    assert await document_bytes(doc, settings) == b"in bucket"
    # not uploaded yet (migration pending): the volume file still serves
    legacy = tmp_path / "vol.bin"
    legacy.write_bytes(b"volume")
    pending = SimpleNamespace(storage_key=None, storage_path=str(legacy))
    assert await document_local_path(pending, settings) == legacy
    assert await document_bytes(pending, settings) == b"volume"
    gone = SimpleNamespace(storage_key=None, storage_path=str(tmp_path / "gone.bin"))
    with pytest.raises(FileNotFoundError):
        await document_bytes(gone, settings)
    reset_store()


async def test_exclusive_put_uses_if_none_match_and_treats_412_as_not_created(tmp_path):
    store, fake = make(tmp_path)
    assert await store.put("k", b"first", exclusive=True) is True
    assert await store.put("k", b"second", exclusive=True) is False
    assert fake.objects["k"] == b"first"
    assert await store.put("k", b"plain") is True and fake.objects["k"] == b"plain"


async def test_exclusive_put_falls_back_to_exists_then_put_on_501(tmp_path, caplog):
    store, fake = make(tmp_path)
    fake.no_conditional_writes = True
    assert await store.put("k", b"1", exclusive=True) is True
    assert await store.put("k", b"2", exclusive=True) is False
    assert fake.objects["k"] == b"1"
    assert store._conditional_writes is False
    assert sum("conditional writes" in r.message for r in caplog.records) <= 1


async def test_put_without_cache_does_not_populate_the_cache(tmp_path):
    store, fake = make(tmp_path)
    src = tmp_path / "vol.bin"
    src.write_bytes(b"volume bytes")
    assert await store.put("k", src, cache=False) is True
    assert fake.objects["k"] == b"volume bytes"
    assert not store.cache_root.exists() or not any(store.cache_root.rglob("*"))
    await store.put("j", b"bytes", cache=False)
    assert not (store.cache_root / "j").exists()


async def test_eviction_skips_pinned_and_dot_paths_and_packages_only_while_pinned(tmp_path):
    store, fake = make(tmp_path, cache_max_mb=1)
    cache = store.cache_root
    victims = {
        "pinned.bin": cache / "pinned.bin",
        ".packaging-stage-1/src.pdf": cache / ".packaging-stage-1/src.pdf",
        "packages/F/out.pdf": cache / "packages/F/out.pdf",
        "plain.bin": cache / "plain.bin",
    }
    for path in victims.values():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * 400_000)
        age(path, 600 if path.parent.name.startswith(".") else 3600)  # live stage dir
    store.pin(victims["pinned.bin"])
    store.pin(cache / "packages/F")
    fake.objects["new"] = b"y"
    await store.local_path("new")  # triggers eviction: 1.6 MB cached, 1 MB cap
    assert not victims["plain.bin"].exists()
    for name in ("pinned.bin", ".packaging-stage-1/src.pdf", "packages/F/out.pdf"):
        assert victims[name].exists(), name
    store.unpin(victims["pinned.bin"])
    store.unpin(cache / "packages/F")
    store.cache_max_mb = 0.5
    await store.local_path("new")  # packages/ is ordinary cache once unpinned
    assert not victims["packages/F/out.pdf"].exists()
    assert not (cache / "packages/F").exists()  # empty dir pruned
    async with store.pinned(victims["pinned.bin"]):
        from tret.storage import is_pinned

        assert is_pinned(victims["pinned.bin"])
    from tret.storage import is_pinned

    assert not is_pinned(victims["pinned.bin"])


def test_client_is_built_from_a_dedicated_session_with_relaxed_checksums(tmp_path, monkeypatch):
    import boto3

    seen = {}

    class FakeSession:
        def client(self, name, **kw):
            seen.update(kw, name=name)
            return object()

    monkeypatch.setattr(boto3.session, "Session", FakeSession)
    store = S3Store(storage_dir=tmp_path, bucket="b", endpoint_url="https://e.example", region="")
    store._s3()
    assert seen["name"] == "s3" and seen["region_name"] == "auto"
    assert seen["endpoint_url"] == "https://e.example"
    assert "config" in seen  # botocore here supports the checksum options
    seen.clear()
    S3Store(storage_dir=tmp_path, bucket="b", endpoint_url="https://e.example", region="eu-1")._s3()
    assert seen["region_name"] == "eu-1"


MB = 1024 * 1024


def _snapshot_then_touch(store, path):
    snap = store.cache_files()
    os.utime(path)
    return snap


async def test_stale_snapshot_floor_above_cap_terminates_and_spares_touched_file(tmp_path):
    from tret.storage import pin_path, unpin_path

    store, _ = make(tmp_path, cache_max_mb=1)
    c = store.cache_root
    c.mkdir(parents=True)
    pinned = c / "pin.bin"
    pinned.write_bytes(b"x" * (2 * MB))
    touched, stale = c / "touched", c / "stale"
    touched.write_bytes(b"y" * 100_000)
    stale.write_bytes(b"y" * 100_000)
    age(touched)
    age(stale)
    pin_path(pinned)
    try:
        snap = store.cache_files()  # mtimes read before the touch
        os.utime(touched)  # another worker's local_path touches it
        store.cache_files = lambda: snap  # type: ignore[method-assign]
        store._evict_sync(c / "other")
    finally:
        unpin_path(pinned)
    assert touched.exists() and pinned.exists()
    assert not stale.exists()


async def test_directory_pin_protects_children_until_unpinned(tmp_path):
    from tret.storage import is_pinned

    store, _ = make(tmp_path, cache_max_mb=1)
    d = store.cache_root / "packages/F1"
    d.mkdir(parents=True)
    a, b = d / "a.pdf", d / "b.pdf"
    a.write_bytes(b"x" * 600_000)
    b.write_bytes(b"x" * 600_000)
    age(a, 7200)
    age(b, 3600)
    store.pin(d)
    assert is_pinned(a) and not is_pinned(store.cache_root / "packages/F2/a.pdf")
    store._evict_sync(store.cache_root / "none")
    assert a.exists() and b.exists()
    store.unpin(d)
    assert not is_pinned(a)
    store._evict_sync(store.cache_root / "none")
    assert not a.exists() and b.exists()  # oldest first, stops under the cap
    age(b, 7200)
    store.cache_max_mb = 0.1
    store._evict_sync(store.cache_root / "none")
    assert not d.exists() and store.cache_root.exists()


async def test_floor_warning_is_rate_limited(tmp_path, caplog):
    import logging

    store, _ = make(tmp_path, cache_max_mb=1)
    f = store.cache_root / ".packaging-x/big"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x" * (2 * MB))
    with caplog.at_level(logging.WARNING, logger="tret.storage"):
        store._evict_sync(store.cache_root / "none")
        store._evict_sync(store.cache_root / "none")
    assert sum("floor" in r.message for r in caplog.records) == 1
    store._floor_warned_at -= 601
    with caplog.at_level(logging.WARNING, logger="tret.storage"):
        store._evict_sync(store.cache_root / "none")
    assert sum("floor" in r.message for r in caplog.records) == 2


async def test_local_path_redownloads_a_file_that_vanished(tmp_path, monkeypatch):
    store, fake = make(tmp_path)
    fake.objects["k"] = b"data"
    path = await store.local_path("k")
    path.unlink()  # another worker evicted it
    assert (await store.local_path("k")).read_bytes() == b"data"

    real = store._evict

    async def evil(*, keep):
        keep.unlink(missing_ok=True)

    monkeypatch.setattr(store, "_evict", evil)
    again = await store.local_path("k")
    assert again.read_bytes() == b"data"
    monkeypatch.setattr(store, "_evict", real)


async def test_orphan_tmp_files_are_evicted_only_when_old(tmp_path):
    store, _ = make(tmp_path, cache_max_mb=1)
    c = store.cache_root
    c.mkdir(parents=True)
    old, fresh = c / ".tmp-old", c / ".tmp-fresh"
    old.write_bytes(b"x" * 700_000)
    fresh.write_bytes(b"x" * 700_000)
    age(old, 7200)
    age(fresh, 600)
    store._evict_sync(c / "none")
    assert not old.exists() and fresh.exists()


async def test_cache_status_fields_are_consistent(tmp_path):
    from tret.storage import admin

    store, fake = make(tmp_path, cache_max_mb=50)
    c = store.cache_root
    (c / "packages/F").mkdir(parents=True)
    (c / "packages/F/o.pdf").write_bytes(b"x" * 1000)
    (c / "plain").write_bytes(b"y" * 500)
    (c / ".staging").mkdir()
    (c / ".staging/s").write_bytes(b"z" * 200)
    store.pin(c / "packages/F")
    try:
        out = await admin.status(store)
    finally:
        store.unpin(c / "packages/F")
    assert out["cache_max_mb"] == 50
    assert out["cache_packages_bytes"] == 1000 and out["cache_pinned_bytes"] == 1000
    assert out["cache_floor_bytes"] == 1200 and out["cache_evictable_bytes"] == 500
    assert out["cache_floor_bytes"] + out["cache_evictable_bytes"] == out["cache_bytes"]


async def test_download_survives_a_pruned_directory_mid_download(tmp_path, monkeypatch):
    import tempfile as tf

    store, fake = make(tmp_path)
    fake.objects["packages/F/x.pdf"] = b"data"
    real = tf.NamedTemporaryFile
    calls = []

    def flaky(*a, **kw):
        if not calls:
            calls.append(1)
            kw["dir"].rmdir()  # eviction pruned the dir after mkdir
        return real(*a, **kw)

    monkeypatch.setattr("tret.storage.s3.tempfile.NamedTemporaryFile", flaky)
    path = await store.local_path("packages/F/x.pdf")
    assert path.read_bytes() == b"data" and calls


async def test_pruning_is_limited_to_packages_and_batches(tmp_path):
    store, _ = make(tmp_path, cache_max_mb=0.05)
    c = store.cache_root
    for rel in ("packages/F/a", "other/G/a"):
        (c / rel).parent.mkdir(parents=True, exist_ok=True)
        (c / rel).write_bytes(b"x" * 100_000)
        age(c / rel)
    store._evict_sync(c / "none")
    assert not (c / "packages/F").exists() and not (c / "other/G/a").exists()
    assert (c / "other/G").exists() and c.exists()


async def test_crashed_packaging_stage_dirs_are_evictable_when_old(tmp_path):
    store, _ = make(tmp_path, cache_max_mb=1)
    c = store.cache_root
    old, fresh = c / "packages/.packaging-stage-old/s.pdf", c / "packages/.packaging-stage-new/s.pdf"
    for f in (old, fresh):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x" * 700_000)
    age(old, 3 * 3600)
    age(fresh, 300)
    fixed, evictable = store.classify_cache()
    assert [p for p, _m, _s in evictable] == [old] and [p for p, _m, _s in fixed] == [fresh]
    store._evict_sync(c / "none")
    assert not old.exists() and fresh.exists()
