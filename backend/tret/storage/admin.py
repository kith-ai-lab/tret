"""Operator tooling behind `tret storage status|migrate`.

Kept out of `tret.cli` so the CLI stays importable without SQLAlchemy: the
handlers import this module inside the function, and this module takes the
session factory as an argument and imports the ORM models only when it runs.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any

from tret.storage import BlobStore, key_for_document

SMALL_FILE_BYTES = 8 * 1024 * 1024
COMMIT_EVERY = 50


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


async def status(store: BlobStore, session_factory: Any = None) -> dict[str, Any]:
    """Backend facts, object count and bytes, cache size, and how many Document rows
    carry a `storage_key`. The DB half is `None` when no session factory is given."""
    count = total = 0
    async for _key, size in store.iter_objects():
        count += 1
        total += size
    out: dict[str, Any] = {**store.describe(), "objects": count, "bytes": total}
    cache_size = getattr(store, "cache_size_bytes", None)
    if cache_size is not None:
        out["cache_bytes"] = await asyncio.to_thread(cache_size)
    cache_status = getattr(store, "cache_status", None)
    if cache_status is not None:
        out.update(await asyncio.to_thread(cache_status))
    out["documents"] = None
    if session_factory is not None:
        from sqlalchemy import func, select

        from tret.db.models import Document

        async with session_factory() as db:
            with_key = (
                await db.execute(select(func.count()).where(Document.storage_key.is_not(None)))
            ).scalar_one()
            without = (
                await db.execute(select(func.count()).where(Document.storage_key.is_(None)))
            ).scalar_one()
        out["documents"] = {"with_storage_key": with_key, "without_storage_key": without}
    return out


async def migrate(
    store: BlobStore,
    session_factory: Any,
    *,
    dry_run: bool = False,
    delete_local: bool = False,
    verify_sample: int = 20,
) -> dict[str, Any]:
    """Upload every Document's file to `store` and record its `storage_key`.

    Resumable and idempotent: an object already in the store at the local file's
    size is skipped, so a re-run after success changes nothing. Each upload is
    size-checked against the store; the first `verify_sample` uploads and every file
    under 8 MB are also re-downloaded (bypassing any cache) and hashed. `storage_path`
    is left alone, so the volume copy stays a valid fallback and pointing the backend
    back at `local` still works. Files are deleted from the volume only with
    `delete_local`, and only once every row that uses them verified.
    """
    from sqlalchemy import select
    from sqlalchemy.orm import load_only

    from tret.db.models import Document

    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "scanned": 0,
        "uploaded": 0,
        "skipped": 0,
        "keys_set": 0,
        "verified": 0,
        "deleted_local": 0,
        "bytes_uploaded": 0,
        "failures": [],
    }
    failures: list[str] = summary["failures"]
    verified_paths: set[Path] = set()
    failed_paths: set[Path] = set()
    sampled_large = 0
    pending = 0  # rows changed since the last commit

    async with session_factory() as db:
        # Only the columns this needs: extracted_text can be megabytes per row.
        rows = (
            (
                await db.execute(
                    select(Document)
                    .options(
                        load_only(
                            Document.id,
                            Document.filename,
                            Document.content_type,
                            Document.storage_path,
                            Document.storage_key,
                            Document.sha256,
                            Document.byte_size,
                        )
                    )
                    .order_by(Document.created_at, Document.id)
                )
            )
            .scalars()
            .all()
        )
        for doc in rows:
            summary["scanned"] += 1
            label = f"{doc.id} ({doc.filename})"
            try:
                key = key_for_document(doc)
            except ValueError as e:
                failures.append(f"{label}: no usable key: {e}")
                continue
            source = Path(doc.storage_path)
            local_size: int | None = None
            if await asyncio.to_thread(source.is_file):
                local_size = (await asyncio.to_thread(source.stat)).st_size
            remote_size = await store.size(key)

            if local_size is None:
                # Nothing to upload. Fine when the store already has it (a re-run after
                # --delete-local); a failure otherwise.
                if remote_size is None:
                    failures.append(f"{label}: file missing locally and not in the store ({doc.storage_path})")
                    continue
                summary["skipped"] += 1
            elif remote_size == local_size:
                # Present at the right size, but this run did not check the bytes, so the
                # volume copy is not eligible for --delete-local.
                summary["skipped"] += 1
            elif dry_run:
                summary["uploaded"] += 1  # would upload
                summary["bytes_uploaded"] += local_size
                if doc.storage_key != key:
                    summary["keys_set"] += 1
                continue
            else:
                try:
                    await store.put(key, source, content_type=doc.content_type, cache=False)
                    after = await store.size(key)
                    if after != local_size:
                        raise RuntimeError(f"size mismatch after upload: local {local_size}, store {after}")
                    # Every file under 8 MB is hashed; the sample budget counts only larger ones.
                    large = local_size >= SMALL_FILE_BYTES
                    if not large or sampled_large < verify_sample:
                        sampled_large += 1 if large else 0
                        expected = await asyncio.to_thread(_hash_file, source)
                        if hashlib.sha256(await store.read_through(key)).hexdigest() != expected:
                            raise RuntimeError("re-downloaded bytes do not match the local file")
                        summary["verified"] += 1
                except Exception as e:  # noqa: BLE001 - one bad file must not stop the run
                    failures.append(f"{label}: {type(e).__name__}: {e}")
                    failed_paths.add(source)
                    continue
                summary["uploaded"] += 1
                summary["bytes_uploaded"] += local_size
                verified_paths.add(source)

            if doc.storage_key != key:
                summary["keys_set"] += 1
                if not dry_run:
                    doc.storage_key = key
                    pending += 1
                    if pending >= COMMIT_EVERY:  # a crash keeps the progress made so far
                        await db.commit()
                        pending = 0
        if not dry_run:
            await db.commit()

    if delete_local and not dry_run:
        cache_root = getattr(store, "cache_root", None)
        for path in sorted(verified_paths - failed_paths):
            if cache_root is not None and path.resolve().is_relative_to(cache_root.resolve()):
                continue  # the s3 cache is not the volume copy; eviction owns it
            try:
                await asyncio.to_thread(path.unlink)
                summary["deleted_local"] += 1
            except FileNotFoundError:
                pass
            except OSError as e:
                failures.append(f"{path}: could not delete the local copy: {e}")
    return summary
