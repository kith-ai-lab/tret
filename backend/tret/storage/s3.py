"""`S3Store`: an S3-compatible bucket (Fly Tigris, AWS S3, MinIO) with a bounded
local cache.

boto3 is synchronous, so every call runs in a worker thread. The cache lives at
`storage_dir/cache/<key>` and is what `local_path` hands to subprocess readers
(Poppler, pypdf, tesseract, which need a real file). Cache files are named by
key, and every key of a document is content-addressed, so evicting one is always
safe: the next `local_path` downloads it again. boto3 is imported on first use,
so the local backend and the CLI never load it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from tret.storage import is_pinned, pin_path, pinned, unpin_path, validate_key
from tret.storage.local import copy_atomic, is_content_addressed, write_atomic

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}
# A cache file this fresh is never evicted: it was just staged for a reader that
# may not have opened it yet. The cache cap is therefore soft by this much.
_EVICT_GRACE_SECONDS = 60.0
_CHUNK = 1024 * 1024
_PRECONDITION_CODES = {"PreconditionFailed", "412"}
_UNSUPPORTED_CODES = {"NotImplemented", "501"}

log = logging.getLogger("tret.storage")


def _error_code(exc: BaseException) -> tuple[str, str]:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return "", ""
    return (
        str((response.get("Error") or {}).get("Code", "")),
        str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", "")),
    )


def _is_not_found(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    code = str((response.get("Error") or {}).get("Code", ""))
    status = str((response.get("ResponseMetadata") or {}).get("HTTPStatusCode", ""))
    return code in _NOT_FOUND_CODES or status == "404"


class S3Store:
    backend = "s3"

    def __init__(
        self,
        *,
        storage_dir: str | Path,
        bucket: str,
        endpoint_url: str = "",
        region: str = "",
        cache_max_mb: int = 512,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise RuntimeError("S3Store needs a bucket")
        self.bucket = bucket
        self.endpoint_url = endpoint_url
        self.region = region
        self.cache_max_mb = cache_max_mb
        self.root = Path(storage_dir).resolve()
        self.cache_root = self.root / "cache"
        if client is None:
            try:
                import boto3  # noqa: F401 - fail at configuration time, not on the first upload
            except ImportError as e:
                raise RuntimeError(
                    "TRET_STORAGE_BACKEND=s3 needs boto3: pip install 'tret[s3]'"
                ) from e
        self._client = client
        self._client_lock = threading.Lock()
        self._conditional_writes = True  # flips off if the endpoint rejects If-None-Match
        self._evict_lock = asyncio.Lock()

    # ── plumbing ─────────────────────────────────────────────────────────────
    def _s3(self) -> Any:
        if self._client is None:
            with self._client_lock:  # boto3's default session is not safe to race on first use
                if self._client is None:
                    import boto3  # lazy: only the s3 backend needs it

                    kwargs: dict[str, Any] = {
                        "endpoint_url": self.endpoint_url or None,
                        # Tigris wants "auto"; with a region configured, use that.
                        "region_name": self.region or ("auto" if self.endpoint_url else None),
                    }
                    if self.endpoint_url:
                        # Non-AWS stores may lack the CRC32 checksums newer botocore sends by default.
                        try:
                            from botocore.config import Config

                            kwargs["config"] = Config(
                                request_checksum_calculation="when_required",
                                response_checksum_validation="when_required",
                            )
                        except (TypeError, ImportError):
                            pass  # an older botocore has no such options
                    self._client = boto3.session.Session().client("s3", **kwargs)
        return self._client

    def _cache_path(self, key: str) -> Path:
        validate_key(key)
        path = self.cache_root / key
        if not path.resolve().is_relative_to(self.cache_root.resolve()):
            raise ValueError(f"storage key {key!r} resolves outside the cache")
        return path

    def _touch(self, path: Path) -> None:
        try:
            os.utime(path)
        except OSError:
            pass

    # ── blob operations ──────────────────────────────────────────────────────
    def _upload_sync(
        self, key: str, data: bytes | Path, content_type: str | None, exclusive: bool
    ) -> bool:
        """Upload; with `exclusive`, only if absent. True if this call created it."""
        kwargs: dict[str, Any] = {"Bucket": self.bucket, "Key": key}
        if content_type:
            kwargs["ContentType"] = content_type
        if exclusive and self._conditional_writes:
            try:
                self._put_object(data, IfNoneMatch="*", **kwargs)
                return True
            except Exception as e:  # noqa: BLE001 - classify the S3 answer
                code, status = _error_code(e)
                if code in _PRECONDITION_CODES or status == "412":
                    return False
                if (
                    code in _UNSUPPORTED_CODES
                    or status == "501"
                    or type(e).__name__ == "ParamValidationError"
                ):
                    log.warning(
                        "storage endpoint does not support conditional writes (If-None-Match); "
                        "falling back to exists-then-put for exclusive creates"
                    )
                    self._conditional_writes = False
                else:
                    raise
        if exclusive:
            if self._head_sync(key) is not None:
                return False
            self._put_object(data, **kwargs)
            return True
        self._put_object(data, **kwargs)
        return True

    def _put_object(self, data: bytes | Path, **kwargs: Any) -> None:
        if isinstance(data, Path):
            with data.open("rb") as fh:
                self._s3().put_object(Body=fh, **kwargs)
        else:
            self._s3().put_object(Body=data, **kwargs)

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
        validate_key(key)
        cached = self._cache_path(key)
        created = await asyncio.to_thread(self._upload_sync, key, data, content_type, exclusive)
        if not created:
            return False
        if not cache:
            return True  # bulk migration: do not refill the volume with what was just uploaded
        # Leave the bytes in the cache so an immediate `local_path` costs nothing.
        if isinstance(data, Path):
            if data.resolve() != cached.resolve():
                if move:
                    cached.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(os.replace, data, cached)
                else:
                    await asyncio.to_thread(copy_atomic, cached, data)
        else:
            await asyncio.to_thread(
                write_atomic, cached, data, keep_if_same_size=is_content_addressed(key)
            )
        await self._evict(keep=cached)
        return True

    def pin(self, path: Path) -> None:
        pin_path(path)

    def unpin(self, path: Path) -> None:
        unpin_path(path)

    def pinned(self, path: Path):
        return pinned(path)

    def _download_sync(self, key: str, target: Path) -> None:
        """Stream the object into `target` atomically; FileNotFoundError when absent."""
        try:
            body = self._s3().get_object(Bucket=self.bucket, Key=key)["Body"]
        except Exception as e:  # noqa: BLE001 - classify, then re-raise anything else
            if _is_not_found(e):
                raise FileNotFoundError(key) from e
            raise
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.NamedTemporaryFile(dir=target.parent, prefix=".tmp-", delete=False)
        try:
            with tmp:
                while chunk := body.read(_CHUNK):
                    tmp.write(chunk)
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, target)
        except BaseException:
            Path(tmp.name).unlink(missing_ok=True)
            raise

    async def local_path(self, key: str) -> Path:
        path = self._cache_path(key)
        if await asyncio.to_thread(path.is_file):
            self._touch(path)
        else:
            await asyncio.to_thread(self._download_sync, key, path)
        await self._evict(keep=path)
        return path

    async def get(self, key: str) -> bytes:
        path = await self.local_path(key)
        return await asyncio.to_thread(path.read_bytes)

    async def read_through(self, key: str) -> bytes:
        """Download straight from the bucket, ignoring the cache: how a migration
        proves what it uploaded is what the bucket now holds."""
        validate_key(key)

        def fetch() -> bytes:
            try:
                return self._s3().get_object(Bucket=self.bucket, Key=key)["Body"].read()
            except Exception as e:  # noqa: BLE001
                if _is_not_found(e):
                    raise FileNotFoundError(key) from e
                raise

        return await asyncio.to_thread(fetch)

    def _head_sync(self, key: str) -> int | None:
        try:
            return int(self._s3().head_object(Bucket=self.bucket, Key=key)["ContentLength"])
        except Exception as e:  # noqa: BLE001
            if _is_not_found(e):
                return None
            raise

    async def size(self, key: str) -> int | None:
        validate_key(key)
        return await asyncio.to_thread(self._head_sync, key)

    async def exists(self, key: str) -> bool:
        return await self.size(key) is not None

    async def delete(self, key: str) -> None:
        validate_key(key)
        await asyncio.to_thread(self._s3().delete_object, Bucket=self.bucket, Key=key)
        await asyncio.to_thread(self._cache_path(key).unlink, True)

    def staging_dir(self, prefix: str) -> Path:
        return self._cache_path(prefix.strip("/"))

    async def iter_objects(self, prefix: str = "") -> AsyncIterator[tuple[str, int]]:
        paginator = self._s3().get_paginator("list_objects_v2")
        pages = iter(paginator.paginate(Bucket=self.bucket, Prefix=prefix))
        sentinel = object()
        while True:
            page = await asyncio.to_thread(next, pages, sentinel)
            if page is sentinel:
                return
            for obj in page.get("Contents") or []:
                yield obj["Key"], int(obj.get("Size", 0))

    async def list_keys(self, prefix: str = "") -> AsyncIterator[str]:
        async for key, _size in self.iter_objects(prefix):
            yield key

    # ── cache ────────────────────────────────────────────────────────────────
    def cache_files(self) -> list[tuple[Path, float, int]]:
        out: list[tuple[Path, float, int]] = []
        for dirpath, _dirs, names in os.walk(self.cache_root):
            for name in names:
                path = Path(dirpath) / name
                try:
                    st = path.stat()
                except FileNotFoundError:
                    continue
                out.append((path, st.st_mtime, st.st_size))
        return out

    def cache_size_bytes(self) -> int:
        return sum(size for _p, _m, size in self.cache_files())

    def _evict_sync(self, keep: Path) -> None:
        limit = self.cache_max_mb * 1024 * 1024
        if limit <= 0:
            return
        files = self.cache_files()
        total = sum(size for _p, _m, size in files)
        if total <= limit:
            return
        now = time.time()
        keep = keep.resolve()
        for path, mtime, size in sorted(files, key=lambda f: f[1]):
            if total <= limit:
                break
            if path.resolve() == keep or is_pinned(path):
                continue
            rel = path.relative_to(self.cache_root).parts
            # Dot-prefixed segments are staging/temp work in progress; `packages/` holds
            # packager inputs and evidence a long run still reads. Neither is evictable.
            if rel[0] == "packages" or any(part.startswith(".") for part in rel):
                continue
            if now - mtime < _EVICT_GRACE_SECONDS:
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            total -= size

    async def _evict(self, *, keep: Path) -> None:
        if self.cache_max_mb <= 0:
            return
        async with self._evict_lock:
            await asyncio.to_thread(self._evict_sync, keep)

    def describe(self) -> dict[str, Any]:
        return {
            "backend": "s3",
            "bucket": self.bucket,
            "endpoint_url": self.endpoint_url,
            "region": self.region,
            "cache_dir": str(self.cache_root),
            "cache_max_mb": self.cache_max_mb,
        }
