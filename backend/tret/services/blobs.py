"""Blob lifetime. A blob key is content-addressed (`tret/storage::document_key`), so
more than one `documents` row can point at the same object: a re-upload of the same
bytes, a file a workspace copied out of a batch shared with it. Anything that deletes a
blob because "its document went away" must go through here, so a blob another row still
references is never removed from under it."""
from __future__ import annotations

import logging

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Document
from tret.storage import BlobStore

log = logging.getLogger(__name__)


async def blob_is_referenced(db: AsyncSession, key: str) -> bool:
    """True when any `documents` row refers to storage key `key` (or, for a row from
    before the `storage_key` column, to a path ending in it)."""
    like = "%/" + key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    row = (
        await db.execute(
            select(Document.id)
            .where(
                or_(
                    Document.storage_key == key,
                    and_(Document.storage_key.is_(None), Document.storage_path.like(like, escape="\\")),
                )
            )
            .limit(1)
        )
    ).first()
    return row is not None


async def delete_blob_unless_referenced(db: AsyncSession, store: BlobStore, key: str) -> bool:
    """Delete the blob `key` unless a document row still references it. Returns whether
    it was deleted."""
    if await blob_is_referenced(db, key):
        log.info("kept blob %s: another document still references it", key)
        return False
    await store.delete(key)
    return True
