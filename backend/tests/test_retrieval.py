"""`tret.services.retrieval` — the chunked, ranked, structure-aware replacement
for `search_documents`'s old `str.find()` over the whole of a document's text.

Split into two layers, mirroring the module itself:

- the chunker (`chunk_document`) is pure — no DB — and tested directly against
  the heading-marker text shape `services/documents.py`'s extractors produce
  (see its module docstring for the marker convention: `## Sheet: <name>`,
  `## Table <n>`, `## Page <n>`, plus ordinary markdown headings for docx/md);
- `ensure_chunks`/`rank_chunks`/the `search_documents` and `read_document` tool
  handlers are tested against a disposable sqlite database, the same shape
  every other tools.py test suite uses (see tests/test_web_tools.py). sqlite
  is also where `rank_chunks` exercises its BM25 path, since dialect detection
  reads the session's own bind — a real assertion about *this* ranking path,
  not a stand-in for the Postgres one.
"""

from __future__ import annotations

import os
import re
import uuid

import pytest
from sqlalchemy import ARRAY, func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.db.models import Base, Document, DocumentChunk, Project, Workspace
from tret.engine.tools import RunContext, ToolError, read_document, search_documents
from tret.services import retrieval as retrieval_service
from tret.services.retrieval import chunk_document, ensure_chunks, rank_chunks

TRET_TEST_POSTGRES_URL = os.environ.get("TRET_TEST_POSTGRES_URL", "")


# ── the chunker: pure, no DB ─────────────────────────────────────────────────
def test_prose_chunker_builds_heading_paths_and_respects_size_bounds_with_overlap():
    paragraphs = [
        f"Sentence {i} about the topic, with some more words to pad it out." * 3 for i in range(12)
    ]
    text = "# Intro\n" + "\n\n".join(paragraphs)

    drafts = chunk_document("report.md", text)

    assert drafts, "expected at least one chunk"
    assert all(d.kind == "text" for d in drafts)
    assert all(d.locator["heading_path"] == ["Intro"] for d in drafts)
    # Every chunk lands inside the ~250-400 target token band, except possibly
    # a short final remainder.
    for d in drafts[:-1]:
        assert 200 <= d.token_est <= 420, d.token_est
    # Ordinals are assigned in document order, starting at 0.
    assert [d.ordinal for d in drafts] == list(range(len(drafts)))
    # Overlap: the tail of one chunk's body reappears at the head of the next.
    assert len(drafts) > 1
    tail = drafts[0].body[-80:]
    assert tail[-20:] in drafts[1].body


def test_prose_chunker_nests_heading_paths_by_level():
    text = "# A\npara under A.\n## B\npara under B.\n# C\npara under C."
    drafts = chunk_document("doc.md", text)
    paths = [d.locator["heading_path"] for d in drafts]
    assert paths == [["A"], ["A", "B"], ["C"]]


def test_table_is_kept_whole_when_small():
    text = "# Section\n## Table 1\nName | Value\na | 1\nb | 2"
    drafts = chunk_document("report.docx", text)
    tables = [d for d in drafts if d.kind == "table"]
    assert len(tables) == 1
    assert tables[0].locator == {"table_index": 1, "row_start": 1, "row_end": 3}
    assert "Name | Value" in tables[0].body
    assert "b | 2" in tables[0].body


def test_large_table_is_split_into_row_blocks_with_header_repeated():
    header = "col_a | col_b | col_c | col_d"
    rows = [f"row{i}a | row{i}b | row{i}c | row{i}d" for i in range(400)]
    text = "## Table 1\n" + "\n".join([header, *rows])

    drafts = chunk_document("big.docx", text)
    tables = [d for d in drafts if d.kind == "table"]

    assert len(tables) > 1, "expected the oversized table to be split"
    for t in tables:
        # The header row is repeated verbatim at the top of every block.
        assert t.body.splitlines()[0] == header
        assert t.locator["table_index"] == 1
        assert t.locator["row_start"] >= 2  # row 1 is the header
    # Row ranges are contiguous and cover every data row exactly once.
    ranges = [(t.locator["row_start"], t.locator["row_end"]) for t in tables]
    assert ranges[0][0] == 2
    for (_, end), (next_start, _) in zip(ranges, ranges[1:]):
        assert next_start == end + 1
    assert ranges[-1][1] == len(rows) + 1


def test_spreadsheet_extract_gets_sheet_and_row_range_locators_with_header_repeated():
    header = "name\tvalue\tdescription\tcategory\tregion"
    rows = [
        f"item-{i:04d}\t{i}\tsome longer descriptive text for the row\tcategory-{i % 5}\tregion-{i % 3}"
        for i in range(200)
    ]
    text = "## Sheet: Sheet1\n" + "\n".join([header, *rows]) + "\n## Sheet: Sheet2\nx\ty\n1\t2"

    drafts = chunk_document("book.xlsx", text)
    blocks = [d for d in drafts if d.kind == "sheet_block"]

    sheet1_blocks = [d for d in blocks if d.locator["sheet"] == "Sheet1"]
    sheet2_blocks = [d for d in blocks if d.locator["sheet"] == "Sheet2"]
    assert sheet1_blocks and sheet2_blocks
    assert len(sheet1_blocks) > 1, "200 rows should not fit in a single block"
    for b in sheet1_blocks:
        assert b.body.splitlines()[0] == header
    assert sheet2_blocks[0].locator == {"sheet": "Sheet2", "row_start": 2, "row_end": 2}
    assert "Sheet1" in sheet1_blocks[0].context and "rows" in sheet1_blocks[0].context


def test_csv_is_chunked_as_a_single_implicit_sheet():
    text = "name,value\na,1\nb,2"
    drafts = chunk_document("data.csv", text)
    assert len(drafts) == 1
    assert drafts[0].kind == "sheet_block"
    assert drafts[0].locator["sheet"] is None


def test_pdf_pages_get_a_page_locator_and_an_unbroken_page_is_hard_split():
    # pypdf text has no paragraph breaks within a page; the chunker must not
    # let that turn into one giant chunk.
    blob = "word " * 600
    text = f"## Page 1\n{blob}\n## Page 2\nShort page two content."
    drafts = chunk_document("filing.pdf", text)

    page1 = [d for d in drafts if d.locator.get("page") == 1]
    page2 = [d for d in drafts if d.locator.get("page") == 2]
    assert len(page1) > 1, "an unbroken page should be hard-split at the char budget"
    assert page2 and page2[0].body.strip() == "Short page two content."
    for d in page1[:-1]:
        assert d.token_est <= 420


def test_a_short_paragraph_ahead_of_an_oversize_one_is_not_swallowed_into_it():
    # Regression: the hard-split branch used to fire only when `current_paras`
    # was empty, so a short paragraph accumulated just before a giant one
    # (a page title, say) got carried into it instead of flushed on its own —
    # collapsing the whole block into one oversized chunk.
    body = "word " * 2800  # exactly 14000 chars, no internal paragraph breaks
    text = f"## Page 7\nTITLE\n\n{body}"

    drafts = chunk_document("filing.pdf", text)

    assert drafts[0].body.strip() == "TITLE"
    assert drafts[0].token_est < 50, "the title must not have absorbed the body"
    assert len(drafts) > 5, "the oversize body must have been hard-split, not left whole"
    for d in drafts[1:]:
        assert d.token_est <= 420, d.token_est


def test_hard_split_slices_carry_overlap_and_end_at_a_sentence_boundary():
    sentences = [f"This is sentence number {i} in one very long unbroken paragraph." for i in range(400)]
    body = " ".join(sentences)  # a single paragraph with no blank lines at all
    text = f"# Report\n{body}"

    drafts = chunk_document("report.md", text)

    assert len(drafts) > 1
    # Overlap: the tail of one hard-split slice reappears at the head of the
    # next, same contract the paragraph-packing path already gives.
    tail = drafts[0].body[-80:]
    assert tail[-20:] in drafts[1].body
    # Boundary preference: every slice but the last ends right after a
    # sentence-ending punctuation mark, not mid-word.
    for d in drafts[:-1]:
        assert d.body[-1] in ".!?", repr(d.body[-20:])


def test_chunk_document_appends_a_truncation_marker_when_over_the_cap():
    parts = [f"## Table {i}\ncol | val\na | 1" for i in range(1, 2501)]
    text = "\n\n".join(parts)

    drafts = chunk_document("huge.docx", text)

    assert len(drafts) == retrieval_service.MAX_CHUNKS_PER_DOCUMENT + 1
    kept, marker = drafts[:-1], drafts[-1]
    assert len(kept) == retrieval_service.MAX_CHUNKS_PER_DOCUMENT
    assert all(d.kind == "table" for d in kept), "the cap must not have dropped non-table drafts"
    assert marker.kind == "text"
    assert marker.body == retrieval_service.TRUNCATION_MARKER_TEMPLATE.format(
        n=retrieval_service.MAX_CHUNKS_PER_DOCUMENT
    )
    # Ordinals stay contiguous through the marker, so it's addressable via
    # read_document(chunk_ordinal=...) exactly like any other chunk.
    assert [d.ordinal for d in drafts] == list(range(len(drafts)))


# ── ranking: prefers the exact phrase over scattered words ──────────────────
def test_bm25_scores_prefer_the_exact_phrase():
    corpus = [
        "the quarterly emissions report shows a steady decline",
        "emissions decline quarterly the shows report steady",  # same words, scrambled
    ]
    scores = retrieval_service._bm25_scores("quarterly emissions report", corpus)
    boosted = [
        retrieval_service._apply_phrase_boost(s, c, "quarterly emissions report")
        for s, c in zip(scores, corpus)
    ]
    assert boosted[0] > boosted[1]


# ── a disposable sqlite tret ─────────────────────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'retrieval.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import tret.db.engine as db_engine

    saved = (db_engine._engine, db_engine._session_factory)
    db_engine._engine, db_engine._session_factory = engine, factory

    async with factory() as session:
        workspace = Workspace(name="W")
        session.add(workspace)
        await session.flush()
        session.add(Project(workspace_id=workspace.id, name="P"))
        await session.commit()
        yield session

    db_engine._engine, db_engine._session_factory = saved
    await engine.dispose()


@pytest.fixture()
async def project(db):
    return (await db.execute(select(Project))).scalars().one()


async def _make_document(db, project_id, filename, text) -> Document:
    doc = Document(
        project_id=project_id,
        filename=filename,
        content_type="text/plain",
        byte_size=len(text or ""),
        storage_path="/tmp/whatever",
        extracted_text=text,
        extraction_status="done",
        sha256="0" * 64,
    )
    db.add(doc)
    await db.flush()
    return doc


def _ctx(db, project_id, document_ids) -> RunContext:
    return RunContext(
        db=db,
        run_id=uuid.uuid4(),
        project_id=project_id,
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=list(document_ids),
        output_schemas={},
    )


# ── ensure_chunks: lazy, idempotent, best-effort ─────────────────────────────
async def test_ensure_chunks_creates_rows_and_is_idempotent(db, project):
    doc = await _make_document(
        db, project.id, "notes.md", "# Heading\nSome body text about revenue."
    )

    first = await ensure_chunks(db, doc)
    assert first, "expected chunk rows to be created"

    rows = (
        (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)))
        .scalars()
        .all()
    )
    assert len(rows) == len(first)

    # Second call is a no-op: no duplicate rows, nothing new returned.
    second = await ensure_chunks(db, doc)
    assert second == []
    rows_again = (
        (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)))
        .scalars()
        .all()
    )
    assert len(rows_again) == len(rows)


async def test_ensure_chunks_is_a_noop_for_a_document_with_no_text(db, project):
    doc = await _make_document(db, project.id, "empty.txt", None)
    assert await ensure_chunks(db, doc) == []


# ── search_documents tool: ranking, format, isolation, lazy backfill ────────
async def test_search_documents_output_includes_document_id_and_locator(db, project):
    doc = await _make_document(
        db,
        project.id,
        "policy.md",
        "# Scope 1\nOur scope 1 emissions declined this year.\n"
        "## Detail\nMore detail about the decline in emissions.",
    )
    ctx = _ctx(db, project.id, [doc.id])

    result = await search_documents(ctx, query="scope 1 emissions")

    assert str(doc.id) in result
    assert "policy.md" in result
    # A locator descriptor (heading path) is present so a model can cite where
    # a hit came from and read_document can jump to it.
    assert "Scope 1" in result


async def test_search_documents_prints_the_chunk_ordinal_that_read_document_can_jump_to(
    db, project
):
    # Regression: a hit used to name a chunk's location (page/heading/table)
    # but never its ordinal, so `read_document(chunk_ordinal=...)` had nothing
    # in the search result it could act on.
    doc = await _make_document(
        db,
        project.id,
        "notes.md",
        "# Heading\nSome revenue figures are discussed in this section of the document.",
    )
    ctx = _ctx(db, project.id, [doc.id])

    result = await search_documents(ctx, query="revenue figures")

    match = re.search(r"chunk (\d+)", result)
    assert match, f"expected a 'chunk N' marker in the result, got: {result!r}"
    ordinal = int(match.group(1))

    jumped = await read_document(ctx, document_id=str(doc.id), chunk_ordinal=ordinal)
    assert "revenue figures" in jumped


async def test_search_documents_lazily_backfills_a_document_ingested_before_chunks_existed(
    db, project
):
    # Simulate a document that predates this feature: extracted_text set, but
    # no chunk rows — exactly what a pre-existing row looks like.
    doc = await _make_document(
        db, project.id, "legacy.txt", "The annual revenue figure is discussed here."
    )
    existing = (
        (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)))
        .scalars()
        .all()
    )
    assert existing == []

    ctx = _ctx(db, project.id, [doc.id])
    result = await search_documents(ctx, query="revenue")

    assert str(doc.id) in result
    rows = (
        (await db.execute(select(DocumentChunk).where(DocumentChunk.document_id == doc.id)))
        .scalars()
        .all()
    )
    assert rows, "the document should have been chunked on first search"


async def test_search_documents_ranks_the_exact_phrase_above_scattered_words(db, project):
    doc = await _make_document(
        db,
        project.id,
        "mix.md",
        "# One\nThe quarterly emissions report shows a steady decline across every site.\n"
        "# Two\nDecline: emissions, quarterly. Report shows the steady figures scattered about.",
    )
    ctx = _ctx(db, project.id, [doc.id])

    result = await search_documents(ctx, query="quarterly emissions report")

    first_hit_pos = result.find("[")
    one_pos = result.find("One", first_hit_pos)
    two_pos = result.find("Two", first_hit_pos)
    assert one_pos != -1 and (two_pos == -1 or one_pos < two_pos)


async def test_search_documents_never_returns_another_projects_chunks(db, project):
    other_workspace = Workspace(name="W2")
    db.add(other_workspace)
    await db.flush()
    other_project = Project(workspace_id=other_workspace.id, name="Other")
    db.add(other_project)
    await db.flush()

    mine = await _make_document(db, project.id, "mine.md", "Confidential figures about the merger.")
    theirs = await _make_document(
        db, other_project.id, "theirs.md", "Confidential figures about the merger."
    )

    ctx = _ctx(db, project.id, [mine.id])  # `theirs` is deliberately not attached
    result = await search_documents(ctx, query="confidential merger figures")

    assert str(mine.id) in result
    assert str(theirs.id) not in result

    # Direct isolation check at the ranking layer too, not just via the tool's
    # own scoping: rank_chunks must not cross document_ids on its own.
    await ensure_chunks(db, theirs)
    hits = await rank_chunks(db, [mine.id], "confidential merger figures")
    assert all(h.document_id == mine.id for h in hits)


async def test_search_documents_document_id_filter_restricts_to_one_document(db, project):
    a = await _make_document(db, project.id, "a.md", "Alpha content about widgets.")
    b = await _make_document(db, project.id, "b.md", "Beta content about widgets too.")
    ctx = _ctx(db, project.id, [a.id, b.id])

    result = await search_documents(ctx, query="widgets", document_id=str(a.id))

    assert str(a.id) in result
    assert str(b.id) not in result


async def test_search_documents_raises_for_a_document_id_not_attached(db, project):
    a = await _make_document(db, project.id, "a.md", "Alpha content.")
    other = await _make_document(db, project.id, "other.md", "Other content.")
    ctx = _ctx(db, project.id, [a.id])  # `other` not attached

    with pytest.raises(ToolError):
        await search_documents(ctx, query="content", document_id=str(other.id))


async def test_search_documents_no_matches_message(db, project):
    doc = await _make_document(
        db, project.id, "a.md", "Completely unrelated content about gardening."
    )
    ctx = _ctx(db, project.id, [doc.id])
    result = await search_documents(ctx, query="quantum computing")
    assert "No matches" in result


# ── read_document: chunk_ordinal jump ────────────────────────────────────────
async def test_read_document_can_jump_to_a_chunk_by_ordinal(db, project):
    doc = await _make_document(
        db, project.id, "notes.md", "# Heading\nSome body text about revenue figures."
    )
    ctx = _ctx(db, project.id, [doc.id])
    await ensure_chunks(db, doc)

    text = await read_document(ctx, document_id=str(doc.id), chunk_ordinal=0)

    assert "chunk 0" in text
    assert "revenue figures" in text


async def test_read_document_rejects_an_unknown_chunk_ordinal(db, project):
    doc = await _make_document(db, project.id, "notes.md", "Some short text.")
    ctx = _ctx(db, project.id, [doc.id])
    await ensure_chunks(db, doc)

    with pytest.raises(ToolError):
        await read_document(ctx, document_id=str(doc.id), chunk_ordinal=999)


# ── the Postgres FTS path, exercised against a real Postgres ────────────────
# Same server, same fixtures, same skip condition as test_migrations_postgres.py
# and test_tenancy_isolation.py — see that module's docstring for the local
# invocation. Everything above this line runs sqlite's BM25 path; this is the
# one test that proves the *other* dialect branch of rank_chunks/ensure_chunks
# — the generated-at-insert tsvector column, the GIN index, ts_rank_cd — is
# reachable and correct, not just type-checked.
pytestmark_pg = pytest.mark.skipif(
    not TRET_TEST_POSTGRES_URL,
    reason="set TRET_TEST_POSTGRES_URL to a Postgres server where tests may create databases",
)

# Importing this module never touches a database — only running its tests
# does — so this is safe unconditionally; it just reuses the same
# create/drop-a-throwaway-database fixture test_migrations_postgres.py and
# test_tenancy_isolation.py already have.
from test_migrations_postgres import database  # noqa: E402, F401  (fixture, reused)


@pytestmark_pg
async def test_postgres_full_text_search_ranks_the_exact_phrase_above_scattered_words(
    database,  # noqa: F811  (pytest fixture, not a redefinition)
):
    from tret.db.migrate import ensure_schema

    engine = create_async_engine(database)
    try:
        await ensure_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            workspace = Workspace(name="W")
            db.add(workspace)
            await db.flush()
            project = Project(workspace_id=workspace.id, name="P")
            db.add(project)
            await db.flush()

            doc = await _make_document(
                db,
                project.id,
                "mix.md",
                "# One\nThe quarterly emissions report shows a steady decline across every site.\n"
                "# Two\nDecline: emissions, quarterly. Report shows the steady figures scattered about.",
            )
            created = await ensure_chunks(db, doc)
            assert created
            # The dialect-detected column is genuinely populated, not just
            # declared — this is what `_rank_postgres`'s `@@` predicate reads.
            for row in created:
                assert row.search_vector is not None

            hits = await rank_chunks(db, [doc.id], "quarterly emissions report")
            assert hits
            assert "One" in hits[0].context

            # Isolation holds on this dialect too.
            other_project = Project(workspace_id=workspace.id, name="Other")
            db.add(other_project)
            await db.flush()
            other_doc = await _make_document(
                db, other_project.id, "theirs.md", "quarterly emissions report"
            )
            await ensure_chunks(db, other_doc)
            scoped = await rank_chunks(db, [doc.id], "quarterly emissions report")
            assert all(h.document_id == doc.id for h in scoped)
    finally:
        await engine.dispose()


@pytestmark_pg
async def test_postgres_falls_back_to_or_semantics_when_not_every_term_is_present(
    database,  # noqa: F811  (pytest fixture, not a redefinition)
):
    """`websearch_to_tsquery` ANDs bare terms the same as `plainto_tsquery`
    did — probed against a live Postgres, a 5-term query against a chunk
    containing only 3 of them returned zero rows through that primary query
    alone. `_rank_postgres`'s OR fallback (`_or_tsquery_expr`) is what makes
    `rank_chunks` match sqlite BM25's "any shared term counts" behavior on
    this dialect too, without turning the primary query's phrase/negation/OR
    syntax off.
    """
    from tret.db.migrate import ensure_schema

    engine = create_async_engine(database)
    try:
        await ensure_schema(engine)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            workspace = Workspace(name="W")
            db.add(workspace)
            await db.flush()
            project = Project(workspace_id=workspace.id, name="P")
            db.add(project)
            await db.flush()

            doc = await _make_document(
                db,
                project.id,
                "policy.md",
                "# Overview\nThe committee approved the annual budget for solar panel "
                "installation across every regional office this quarter.",
            )
            await ensure_chunks(db, doc)

            # 5 terms; only "solar", "panel" and "budget" are actually present
            # in the chunk above — "hydrogen" and "turbine" are not.
            query = "solar panel budget hydrogen turbine"

            # The primary AND-only query alone must match nothing here — this
            # is the exact shape of the bug, and it's what proves the OR
            # fallback below, not the primary query, is carrying the result.
            direct = (
                await db.execute(
                    select(DocumentChunk).where(
                        DocumentChunk.document_id == doc.id,
                        DocumentChunk.search_vector.op("@@")(
                            func.websearch_to_tsquery("english", query)
                        ),
                    )
                )
            ).first()
            assert direct is None, "expected the AND-only primary query to match nothing"

            hits = await rank_chunks(db, [doc.id], query)
            assert hits, "the OR fallback should still find the chunk sharing 3 of 5 terms"
    finally:
        await engine.dispose()
