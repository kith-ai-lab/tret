"""Object storage for document bytes: a local directory or an S3-compatible bucket.

Everything tret keeps as a file (uploads, fetched snapshots, files imported from
SharePoint/Drive, packaged page subsets, batch records) goes through a
`BlobStore`. The local backend is byte-for-byte the layout tret has always had
under `TRET_STORAGE_DIR`; the s3 backend (Fly Tigris, AWS, MinIO) keeps the
bytes in a bucket and stages files into a bounded local cache for the readers
that need a real file (Poppler, pypdf, tesseract). See docs/storage.md.

Imports here are stdlib-only: `tret.cli` and `tret.config` must stay importable
without the server extra, and boto3 is imported lazily by `S3Store`.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BlobStore",
    "batch_record_key",
    "describe_line",
    "document_bytes",
    "document_key",
    "document_local_path",
    "get_store",
    "key_for_document",
    "package_key",
    "reset_store",
    "validate_key",
]


@runtime_checkable
class BlobStore(Protocol):
    backend: str  # "local" | "s3"

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
        """Store `data` under `key`. A `Path` is streamed from disk; `move=True` lets the
        store consume that file (rename it into place) instead of copying it.

        Returns True when this call created the object. With `exclusive=True` an
        existing object is left untouched and False is returned, atomically (the
        create-if-absent a concurrent importer needs); otherwise it is always True.
        `cache=False` keeps an s3 upload out of the local cache (bulk migration).
        """

    async def get(self, key: str) -> bytes: ...

    async def read_through(self, key: str) -> bytes:
        """`get` that never trusts a local cache (migration verification)."""

    async def exists(self, key: str) -> bool: ...

    async def size(self, key: str) -> int | None:
        """Byte size of the stored object, or None when it is absent."""

    async def delete(self, key: str) -> None:
        """Best effort. Packages are evidence; only debris cleanup uses this."""

    async def local_path(self, key: str) -> Path:
        """A real file holding the object's bytes, for subprocess readers. The local
        backend returns the stored file itself; s3 stages it into the cache."""

    def pin(self, path: Path) -> None:
        """Keep `path` out of cache eviction until `unpin` (no-op on the local backend)."""

    def unpin(self, path: Path) -> None: ...

    def pinned(self, path: Path) -> contextlib.AbstractAsyncContextManager[None]:
        """`async with store.pinned(path):` around work that reads a staged file."""

    def staging_dir(self, prefix: str) -> Path:
        """The local directory a key prefix maps to (a packager writes real files)."""

    def iter_objects(self, prefix: str = "") -> AsyncIterator[tuple[str, int]]:
        """(key, size) for every object under `prefix`."""

    def list_keys(self, prefix: str = "") -> AsyncIterator[str]: ...

    def describe(self) -> dict[str, Any]:
        """Backend facts for `tret storage status` and the boot log. Never secrets."""


# ── pins ─────────────────────────────────────────────────────────────────────
# Process-wide: a cache file in use by a long-running reader (a packaging run can
# last ten minutes) must survive eviction triggered by any other request.
_PINS: dict[str, int] = {}


def pin_path(path: Path) -> None:
    k = str(Path(path).resolve())
    _PINS[k] = _PINS.get(k, 0) + 1


def unpin_path(path: Path) -> None:
    k = str(Path(path).resolve())
    n = _PINS.get(k, 0) - 1
    if n > 0:
        _PINS[k] = n
    else:
        _PINS.pop(k, None)


def is_pinned(path: Path) -> bool:
    """True when `path` or any ancestor directory is pinned."""
    if not _PINS:
        return False
    p = Path(path).resolve()
    if str(p) in _PINS:
        return True
    return any(str(parent) in _PINS for parent in p.parents)


@contextlib.asynccontextmanager
async def pinned(path: Path):
    pin_path(path)
    try:
        yield
    finally:
        unpin_path(path)


# ── keys ─────────────────────────────────────────────────────────────────────
def validate_key(key: str) -> str:
    """A key is a POSIX-relative path: no leading slash, no `.`/`..`/empty parts."""
    if not isinstance(key, str) or not key:
        raise ValueError("storage key must be a non-empty string")
    if "\x00" in key or "\\" in key:
        raise ValueError(f"storage key {key!r} contains a forbidden character")
    if key.startswith("/"):
        raise ValueError(f"storage key {key!r} must be relative")
    if any(part in ("", ".", "..") for part in key.split("/")):
        raise ValueError(f"storage key {key!r} must not contain empty, '.' or '..' parts")
    return key


def document_key(sha256: str, filename: str) -> str:
    """`<sha256>-<filename>`, flat: a filename that could name a path is refused."""
    if "/" in filename or "\\" in filename:
        raise ValueError("Invalid filename")
    return validate_key(f"{sha256}-{filename}")


def package_key(finding_id: object, name: str) -> str:
    return validate_key(f"packages/{finding_id}/{name}")


def batch_record_key(batch_id: object, name: str) -> str:
    """Where a batch's records live when they are kept as stored files."""
    return validate_key(f"batches/{batch_id}/{name}")


_PACKAGES_PATH = re.compile(r"^.*/(packages/.*)$")


def key_for_document(doc: Any) -> str:
    """`doc.storage_key`, or the key derived from `storage_path` for a row written
    before the column existed (the migration's backfill rule, in Python): the part
    from the last `/packages/` on for package outputs, the basename for the rest."""
    stored = getattr(doc, "storage_key", None)
    if stored:
        return validate_key(stored)
    path = str(doc.storage_path).replace("\\", "/")
    if "/packages/" in path:
        m = _PACKAGES_PATH.match(path)
        if m:
            return validate_key(m.group(1))
    return validate_key(PurePosixPath(path).name)


# ── resolving a store ────────────────────────────────────────────────────────
_stores: dict[tuple, BlobStore] = {}


def _s3_config(settings: Any) -> dict[str, Any]:
    """The s3 settings with the names `fly storage create` injects as fallbacks."""
    return {
        "bucket": getattr(settings, "storage_s3_bucket", "") or os.environ.get("BUCKET_NAME", ""),
        "endpoint_url": getattr(settings, "storage_s3_endpoint_url", "")
        or os.environ.get("AWS_ENDPOINT_URL_S3", ""),
        "region": getattr(settings, "storage_s3_region", "") or os.environ.get("AWS_REGION", ""),
        "cache_max_mb": int(getattr(settings, "storage_cache_max_mb", 512)),
    }


def get_store(settings: Any = None) -> BlobStore:
    """The store for `settings` (default: the process settings), resolved per call.

    Callers pass the settings object they already use, so a test that patches
    `storage_dir` on the cached instance, or swaps in a `SimpleNamespace` with just
    `storage_dir`, gets a store on that directory. Stores are cached on the values
    that define them, not on object identity. A settings object without
    `storage_backend` is the local backend.
    """
    if settings is None:
        from tret.config import get_settings

        settings = get_settings()
    backend = (getattr(settings, "storage_backend", "local") or "local").strip().lower()
    storage_dir = str(Path(settings.storage_dir).resolve())
    if backend == "local":
        key: tuple = ("local", storage_dir)
        store = _stores.get(key)
        if store is None:
            from tret.storage.local import LocalStore

            store = _stores[key] = LocalStore(storage_dir)
        return store
    if backend == "s3":
        cfg = _s3_config(settings)
        if not cfg["bucket"]:
            raise RuntimeError(
                "TRET_STORAGE_BACKEND=s3 needs a bucket: set TRET_STORAGE_S3_BUCKET "
                "(or BUCKET_NAME, which `fly storage create` sets)."
            )
        key = ("s3", storage_dir, cfg["bucket"], cfg["endpoint_url"], cfg["region"], cfg["cache_max_mb"])
        store = _stores.get(key)
        if store is None:
            from tret.storage.s3 import S3Store

            store = _stores[key] = S3Store(storage_dir=storage_dir, **cfg)
        return store
    raise RuntimeError(f"TRET_STORAGE_BACKEND={backend!r} is not one of: local, s3")


def reset_store() -> None:
    """Forget cached stores (tests)."""
    _stores.clear()


def describe_line(store: BlobStore) -> str:
    d = store.describe()
    if d["backend"] == "s3":
        return (
            f"storage: s3 bucket={d['bucket']} endpoint={d['endpoint_url'] or '(aws default)'} "
            f"cache={d['cache_dir']} (max {d['cache_max_mb']} MB)"
        )
    return f"storage: local dir={d['root']}"


# ── Document helpers ─────────────────────────────────────────────────────────
async def document_local_path(doc: Any, settings: Any = None) -> Path:
    """A real file for `doc`'s bytes. On the local backend this is `doc.storage_path`
    exactly as stored. On s3 it is the staged cache file; a row the migration has not
    uploaded yet falls back to its volume file when that still exists.

    Raises `FileNotFoundError` when the bytes are nowhere.
    """
    store = get_store(settings)
    if store.backend == "local":
        return Path(doc.storage_path)
    try:
        return await store.local_path(key_for_document(doc))
    except FileNotFoundError:
        legacy = Path(doc.storage_path)
        if await asyncio.to_thread(legacy.is_file):
            return legacy
        raise


async def document_bytes(doc: Any, settings: Any = None) -> bytes:
    """The whole file. Local: read `doc.storage_path`. s3: `get`, with the same
    volume-file fallback as `document_local_path`."""
    store = get_store(settings)
    if store.backend == "local":
        return await asyncio.to_thread(Path(doc.storage_path).read_bytes)
    try:
        return await store.get(key_for_document(doc))
    except FileNotFoundError:
        legacy = Path(doc.storage_path)
        if await asyncio.to_thread(legacy.is_file):
            return await asyncio.to_thread(legacy.read_bytes)
        raise
