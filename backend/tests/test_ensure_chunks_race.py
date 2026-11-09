"""ensure_chunks must survive losing a chunking race without a session-wide
rollback (which would expire the run's ORM objects -> MissingGreenlet)."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select

from tret.db.models import DocumentChunk
from tret.engine.tools import search_documents
from tret.services.retrieval import ensure_chunks

from test_retrieval import _ctx, _make_document, db, project  # noqa: F401  (fixtures)


async def test_ensure_chunks_losing_the_unique_race_does_not_expire_the_session(db, project):
    doc = await _make_document(db, project.id, "race.md", "# H\nRevenue grew strongly.")
    await db.commit()
    doc_id = doc.id

    # Simulate the winner: another session commits chunk (doc, 0) between our
    # "already chunked?" check and our insert.
    import tret.db.engine as db_engine

    real_execute = db.execute
    raced = False

    async def execute_then_race(*a, **kw):
        nonlocal raced
        result = await real_execute(*a, **kw)
        if not raced:
            raced = True
            async with db_engine._session_factory() as other:
                other.add(
                    DocumentChunk(
                        document_id=doc_id, ordinal=0, kind="prose", locator={},
                        context="", body="Revenue grew strongly.", token_est=4,
                    )
                )
                await other.commit()
        return result

    db.execute = execute_then_race  # type: ignore[method-assign]
    try:
        out = await ensure_chunks(db, doc)
    finally:
        db.execute = real_execute  # type: ignore[method-assign]
    assert raced and out == []

    # Objects loaded before the call stay loaded: sync attribute reads work.
    assert not sa_inspect(doc).expired_attributes
    assert doc.filename == "race.md"
    assert not sa_inspect(project).expired_attributes
    assert project.name == "P"

    # The session is still usable and sees the winner's chunk; search works.
    rows = (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc_id))).scalars().all()
    assert len(rows) == 1
    result = await search_documents(_ctx(db, project.id, [doc_id]), query="revenue")
    assert str(doc_id) in result


async def test_ensure_chunks_pending_flush_failure_propagates_not_swallowed(db, project):
    import pytest

    doc = await _make_document(db, project.id, "flush.md", "# H\nBody text here.")
    await db.commit()

    async def boom(*a, **kw):
        raise RuntimeError("pending state is broken")

    real_flush = db.flush
    db.flush = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(RuntimeError, match="pending state is broken"):
            await ensure_chunks(db, doc)
    finally:
        db.flush = real_flush  # type: ignore[method-assign]
