"""`net/fetch/snapshot.py::store_snapshot` writes its content-addressed file
atomically, and leaves an identical existing file alone."""
from __future__ import annotations

import hashlib
import os
import stat
import uuid
from types import SimpleNamespace

import pytest

from tret.net.fetch import snapshot as snapshot_module
from tret.net.fetch.fetch import FetchedPage

URL = "https://example.org/report.txt"


class _FakeDb:
    def __init__(self):
        self.added = []

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _page(body: bytes) -> FetchedPage:
    return FetchedPage(
        url=URL,
        requested_url=URL,
        status_code=200,
        content_type="text/plain",
        extension=".txt",
        body=body,
        sha256=hashlib.sha256(body).hexdigest(),
        duration_ms=1,
        redirects=(),
        truncated=False,
        headers=(),
    )


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(snapshot_module, "get_settings", lambda: SimpleNamespace(storage_dir=str(tmp_path)))
    return tmp_path.resolve()


def _target(storage, page: FetchedPage):
    return storage / f"{page.sha256}-{snapshot_module.snapshot_filename(page.url, page.extension)}"


async def test_a_plain_write_leaves_the_file_and_no_temp_files(storage):
    page = _page(b"hello world")
    doc = await snapshot_module.store_snapshot(_FakeDb(), page, uuid.uuid4(), None)
    target = _target(storage, page)
    assert doc.storage_path == str(target)
    assert target.read_bytes() == b"hello world"
    assert stat.S_IMODE(target.stat().st_mode) == 0o644
    assert sorted(p.name for p in storage.iterdir()) == [target.name]


async def test_an_existing_read_only_copy_is_kept_without_rewriting(storage):
    page = _page(b"same bytes")
    target = _target(storage, page)
    target.write_bytes(page.body)
    os.chmod(target, 0o444)
    before = target.stat()
    try:
        doc = await snapshot_module.store_snapshot(_FakeDb(), page, uuid.uuid4(), None)
        after = target.stat()
        assert doc.storage_path == str(target)
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert sorted(p.name for p in storage.iterdir()) == [target.name]
    finally:
        os.chmod(target, 0o644)


async def test_a_short_existing_file_is_replaced_atomically(storage):
    page = _page(b"the full body")
    target = _target(storage, page)
    target.write_bytes(b"partial")
    os.chmod(target, 0o444)  # the rename needs only directory write permission
    await snapshot_module.store_snapshot(_FakeDb(), page, uuid.uuid4(), None)
    assert target.read_bytes() == b"the full body"
    assert sorted(p.name for p in storage.iterdir()) == [target.name]


@pytest.mark.skipif(
    not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file; needs POSIX permissions"
)
async def test_an_unreadable_same_size_copy_is_replaced(storage):
    page = _page(b"same bytes")
    target = _target(storage, page)
    target.write_bytes(page.body)
    os.chmod(target, 0o000)
    try:
        await snapshot_module.store_snapshot(_FakeDb(), page, uuid.uuid4(), None)
        assert stat.S_IMODE(target.stat().st_mode) == 0o644
        assert target.read_bytes() == b"same bytes"
        assert sorted(p.name for p in storage.iterdir()) == [target.name]
    finally:
        os.chmod(target, 0o644)
