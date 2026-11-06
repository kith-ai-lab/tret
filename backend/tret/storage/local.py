"""`LocalStore`: the filesystem backend, `storage_dir/<key>` for every key.

This is the layout tret has always used (`<sha256>-<filename>` flat under the
storage directory, `packages/<finding_id>/<name>` below it), so switching the
code to the store changes no bytes on disk.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any

from tret.storage import validate_key

_CONTENT_ADDRESSED = re.compile(r"^[0-9a-f]{64}-")
# Directories under storage_dir that are not blobs: installed packs and the s3 cache.
_NOT_BLOBS = ("packs", "cache")
_TMP_PREFIX = ".tmp-"


def is_content_addressed(key: str) -> bool:
    """`<sha256>-<name>`: the name carries the hash of the bytes."""
    return bool(_CONTENT_ADDRESSED.match(PurePosixPath(key).name))


def write_atomic(target: Path, body: bytes, *, keep_if_same_size: bool = False) -> None:
    """Write `body` to `target` through a temp file in the same directory.

    With `keep_if_same_size` (content-addressed names only) an existing readable
    file of the same size already holds these bytes and is kept as is: it may
    belong to another OS user and be unwritable. Otherwise a temp file is renamed
    into place, which needs only write permission on the directory.
    """
    if keep_if_same_size:
        try:
            if target.stat().st_size == len(body) and os.access(target, os.R_OK):
                return
        except FileNotFoundError:
            pass
    for attempt in (0, 1):
        # A concurrent cache prune can rmdir the emptied directory between mkdir and
        # the rename; recreate it and retry once.
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(dir=target.parent, prefix=_TMP_PREFIX, delete=False)
            with tmp:
                tmp.write(body)
            # NamedTemporaryFile is 0600; other service users must still read it.
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, target)
            return
        except FileNotFoundError:
            if tmp is not None:
                Path(tmp.name).unlink(missing_ok=True)
            if attempt:
                raise
        except BaseException:
            if tmp is not None:
                Path(tmp.name).unlink(missing_ok=True)
            raise


def copy_atomic(target: Path, source: Path) -> None:
    """Copy `source` over `target` through a temp file in the target's directory."""
    for attempt in (0, 1):
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = None
        try:
            tmp = tempfile.NamedTemporaryFile(dir=target.parent, prefix=_TMP_PREFIX, delete=False)
            with tmp, source.open("rb") as src:
                while chunk := src.read(1024 * 1024):
                    tmp.write(chunk)
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, target)
            return
        except FileNotFoundError:
            if tmp is not None:
                Path(tmp.name).unlink(missing_ok=True)
            # A missing source is a real error; only a pruned target directory is retried.
            if attempt or not source.exists():
                raise
        except BaseException:
            if tmp is not None:
                Path(tmp.name).unlink(missing_ok=True)
            raise


@contextlib.asynccontextmanager
async def _noop_pinned():
    yield


def create_exclusive(target: Path, body: bytes | None, source: Path | None) -> bool:
    """Create `target` with the given bytes (or a copy of `source`) only if it does
    not exist; True if this call created it. The content lands in a temp file first
    and is hard-linked into place, so a reader never sees a half-written file and two
    racers cannot both succeed. Filesystems without hard links fall back to
    `open("xb")` (create-if-absent, but not atomic with respect to readers)."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=target.parent, prefix=_TMP_PREFIX, delete=False)
    try:
        with tmp:
            if source is not None:
                with source.open("rb") as src:
                    shutil.copyfileobj(src, tmp, 1024 * 1024)
            else:
                tmp.write(body or b"")
        os.chmod(tmp.name, 0o644)
        try:
            os.link(tmp.name, target)
            return True
        except FileExistsError:
            return False
        except OSError:  # no hard links here
            try:
                with open(target, "xb") as sink, open(tmp.name, "rb") as src:
                    shutil.copyfileobj(src, sink, 1024 * 1024)
            except FileExistsError:
                return False
            os.chmod(target, 0o644)
            return True
    finally:
        Path(tmp.name).unlink(missing_ok=True)


class LocalStore:
    backend = "local"

    def __init__(self, storage_dir: str | Path) -> None:
        self.root = Path(storage_dir).resolve()

    def _path(self, key: str) -> Path:
        validate_key(key)
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or path == self.root:
            raise ValueError(f"storage key {key!r} resolves outside the storage directory")
        return path

    def _put_sync(self, key: str, data: bytes | Path, move: bool, exclusive: bool) -> bool:
        target = self._path(key)
        if exclusive:
            source = data.resolve() if isinstance(data, Path) else None
            if source is not None and source == target:
                return False  # already in place
            created = create_exclusive(target, None if source else data, source)  # type: ignore[arg-type]
            if move and source is not None:
                source.unlink(missing_ok=True)
            return created
        if isinstance(data, Path):
            source = data.resolve()
            if source == target:
                return True  # a packager already wrote it in place
            if move:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(source, target)
            else:
                copy_atomic(target, source)
            return True
        write_atomic(target, data, keep_if_same_size=is_content_addressed(key))
        return True

    async def put(
        self,
        key: str,
        data: bytes | Path,
        *,
        content_type: str | None = None,
        move: bool = False,
        exclusive: bool = False,
        cache: bool = True,
    ) -> bool:
        return await asyncio.to_thread(self._put_sync, key, data, move, exclusive)

    # Nothing is ever evicted from the local backend, so pinning has nothing to do.
    def pin(self, path: Path) -> None:
        return None

    def unpin(self, path: Path) -> None:
        return None

    def pinned(self, path: Path):
        return _noop_pinned()

    async def get(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    async def read_through(self, key: str) -> bytes:
        """Same as `get`; the s3 store's version bypasses its cache."""
        return await self.get(key)

    async def exists(self, key: str) -> bool:
        return await asyncio.to_thread(self._path(key).is_file)

    async def size(self, key: str) -> int | None:
        try:
            return (await asyncio.to_thread(self._path(key).stat)).st_size
        except FileNotFoundError:
            return None

    async def delete(self, key: str) -> None:
        await asyncio.to_thread(self._path(key).unlink, True)

    async def local_path(self, key: str) -> Path:
        return self._path(key)

    def staging_dir(self, prefix: str) -> Path:
        return self._path(prefix.strip("/"))

    def _walk(self, prefix: str) -> list[tuple[str, int]]:
        out: list[tuple[str, int]] = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            here = Path(dirpath)
            if here == self.root:
                dirnames[:] = [d for d in dirnames if d not in _NOT_BLOBS and not d.startswith(".")]
            for name in filenames:
                if name.startswith("."):  # spool and temp files
                    continue
                path = here / name
                key = path.relative_to(self.root).as_posix()
                if not key.startswith(prefix):
                    continue
                try:
                    out.append((key, path.stat().st_size))
                except FileNotFoundError:
                    continue
        out.sort()
        return out

    async def iter_objects(self, prefix: str = "") -> AsyncIterator[tuple[str, int]]:
        for item in await asyncio.to_thread(self._walk, prefix):
            yield item

    async def list_keys(self, prefix: str = "") -> AsyncIterator[str]:
        async for key, _size in self.iter_objects(prefix):
            yield key

    def describe(self) -> dict[str, Any]:
        return {"backend": "local", "root": str(self.root)}
