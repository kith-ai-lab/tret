"""Chunked, ranked, structure-aware document retrieval — the replacement for
`search_documents`' old `str.find()` over the whole of `Document.extracted_text`.

Two entry points, both used by `engine/tools.py`:

- `ensure_chunks(db, document)` — idempotent. Chunks a document the first time
  anything asks to search it (a document ingested before this table existed, or
  ingested a moment ago) and is a no-op on every later call, since chunk rows
  are never rewritten. This is the *only* place chunks are created; there is no
  separate "chunk at ingest" code path to keep in sync with it — a document
  ingested through `services/documents.py::ingest_document` simply has no
  chunks yet, and gets them here, lazily, the first time `search_documents`
  looks at it (typically within the same run that attached it).
- `rank_chunks(db, document_ids, query, max_results)` — ranks the chunks of the
  given documents against `query` and returns the top hits. Dialect-detected
  from the session's own bind (`db.get_bind().dialect.name`): Postgres uses
  full-text search (`ts_rank_cd` over a `tsvector` column, queried with
  `websearch_to_tsquery` and an OR'd `to_tsquery` fallback — see
  `_rank_postgres`), sqlite (every test, and the documented non-Postgres dev
  fallback) uses a small in-Python BM25 over the same rows. Both paths return
  the same *shape* of result from the same chunk data and apply the same
  exact-phrase boost, but ranking itself is NOT identical across dialects:
  Postgres stems query and document terms (the `english` text search config —
  "emission" and "emissions" match each other), the BM25 path does not, and
  the two scorers weight term frequency/rarity differently. Expect the
  ranking of whichever dialect you deploy on, not parity with the other.

## Chunking rules

The chunker (`chunk_document`) is structure-aware, not a fixed-size sliding
window: it walks `extracted_text` looking for the heading-style markers the
extractors in `services/documents.py` already emit — `## Sheet: <name>` (xlsx),
`## Table <n>` (docx), `## Page <n>` (pdf), `## Slide <n>` (pptx), plus any
ordinary markdown heading (docx `Heading N`/`Title` styles, or a `.md` file's
own `#`/`##` headings) — and slices the document into blocks along those
boundaries before chunking each block by its own rule:

- **Prose** (kind `text`): split on paragraph breaks (blank lines) and packed
  greedily into ~250-400 estimated-token chunks (`len // 4`, a cheap stand-in
  for a real tokenizer — good enough to size a chunk, not to bill one) with a
  ~40-token overlap carried from the tail of the previous chunk into the next,
  so a sentence spanning a chunk boundary is not orphaned on one side of it. A
  single paragraph with no internal breaks at all — the common shape of a
  `pypdf`-extracted page, which rarely has blank lines between paragraphs — is
  hard-split at the character budget instead of being left as one oversized
  chunk. Whatever prose was already pending is flushed as its own chunk
  *first*, so a short paragraph ahead of the oversize one (a title line, say)
  is never swallowed into it; the hard-split slices themselves carry the same
  ~40-token overlap as the paragraph-packing path, and prefer to end at a
  sentence or line boundary within the last ~200 characters of the budget
  rather than cutting mid-word. Each chunk's `locator` carries the heading
  path it fell under and the page it fell on, if either is known.
- **Tables** (kind `table`): kept whole in one chunk when the table fits under
  ~1,200 estimated tokens; a bigger table is split into row blocks with the
  header row (the table's first row) repeated at the top of every block, the
  same rule spreadsheets use below — a reader (model or human) should never
  see a data row without knowing what its columns mean.
- **Spreadsheet extracts** (kind `sheet_block`): one chunk per row block, sized
  the same way as an oversized table, with the sheet's header row repeated in
  every block and the sheet name + row range in `locator`. A `.csv`/`.tsv`
  upload is chunked the same way, as a single implicit sheet.

Chunking itself is cheap — on the order of a few milliseconds per MB of
extracted text, measured against this module's own chunker — which is why
`ensure_chunks` can afford to run it synchronously on the first search that
touches a document. What is not free is the row count: a document at the
2,000,000-character extraction cap (`services/documents.py::
MAX_EXTRACTED_CHARS`) produces on the order of 1,250 chunks at the
~1,600-character (400-token) target size, all inserted in one commit;
`MAX_CHUNKS_PER_DOCUMENT` (2,000) is the hard backstop above that, and a
document that hits it gets one extra marker chunk saying so — see
`chunk_document`.

Every chunk's `context` is a one-line contextual prefix — document title +
section/sheet/page — prepended ahead of ranking per Anthropic's
contextual-retrieval pattern (https://www.anthropic.com/news/contextual-retrieval):
short-text search (BM25, and lexical FTS generally) matches surface wording,
and a chunk that says "revenue grew 12%" with no indication of *which* quarter
or *whose* revenue ranks no differently from one that does — the prefix is
what lets a bare mention of a number or term still be found via the section
that gives it meaning.

## No embeddings in this pass

No embedding provider is configured in this deployment, so ranking is lexical
only (FTS/BM25 plus an exact-phrase boost — see `_apply_phrase_boost`). The
seam for a later embeddings/reranker pass is `rerank_seam` at the bottom of
this file: `rank_chunks` already calls it (an identity function today) on its
final hit list, so a future pass slots in there — widen the over-fetch in
`_rank_postgres`/`_rank_bm25`, re-score the wider candidate set by similarity
or a cross-encoder inside `rerank_seam`, and truncate to `max_results` there.
"""

from __future__ import annotations

import logging
import math
import re
import uuid
from collections import Counter
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import Document, DocumentChunk

log = logging.getLogger("tret.retrieval")

# ── sizing ─────────────────────────────────────────────────────────────────
CHARS_PER_TOKEN = 4  # a cheap stand-in for a real tokenizer; see module docstring
TARGET_TOKENS_MIN = 250
TARGET_TOKENS_MAX = 400
OVERLAP_TOKENS = 40
TARGET_CHARS = TARGET_TOKENS_MAX * CHARS_PER_TOKEN
MIN_CHARS = TARGET_TOKENS_MIN * CHARS_PER_TOKEN
OVERLAP_CHARS = OVERLAP_TOKENS * CHARS_PER_TOKEN
# A table/sheet row block this big (~1,200 est. tokens) before it is split,
# header repeated in each resulting block.
ROWS_CHAR_CEILING = TARGET_CHARS * 3

# A pathological document (a spreadsheet with a hundred thousand rows) must not
# turn into a hundred thousand row-block inserts — bounded and best-effort,
# same philosophy as the extraction caps in services/documents.py. Applied
# exactly once, over the whole document's drafts, in `chunk_document` — never
# inside a per-block helper like `_chunk_rows`, which has no way to know how
# many chunks other blocks in the same document have already produced.
MAX_CHUNKS_PER_DOCUMENT = 2000

# Appended as one extra chunk when a document is truncated at the cap above,
# so a search that only ever sees the top MAX_CHUNKS_PER_DOCUMENT chunks knows
# there was more it never got to look at, instead of silently treating the cut
# as the end of the document.
TRUNCATION_MARKER_TEMPLATE = (
    "[tret: document truncated at {n} chunks; later content not indexed]"
)

SNIPPET_CHARS = 600
PHRASE_BOOST_MULTIPLIER = 2.0

# How much of the tail of a hard-split slice to search for a sentence/line
# boundary to split on, rather than cutting mid-word at the raw char budget.
HARD_SPLIT_BOUNDARY_WINDOW = 200

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
SHEET_RE = re.compile(r"^Sheet:\s*(.+)$")
TABLE_RE = re.compile(r"^Table\s+(\d+)$")
PAGE_RE = re.compile(r"^Page\s+(\d+)$")
SENTENCE_END_RE = re.compile(r"[.!?]\s|\n")


def _token_est(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN)


# ── chunk drafts: pure, no DB ────────────────────────────────────────────────
@dataclass
class ChunkDraft:
    kind: str  # 'text' | 'table' | 'sheet_block'
    locator: dict
    context: str
    body: str
    token_est: int
    ordinal: int = -1


@dataclass
class _Block:
    kind: str  # 'prose' | 'table' | 'sheet'
    heading_path: list[str]
    page: int | None
    sheet: str | None
    table_index: int | None
    lines: list[str] = field(default_factory=list)


def _parse_blocks(text: str) -> list[_Block]:
    """Split `text` into `_Block`s along the heading-style markers described
    in the module docstring, tracking heading nesting, the active page (pdf),
    and the active sheet/table (xlsx/docx) as side-channel state rather than
    heading-path entries — a table or a sheet is a different *kind* of chunk,
    not another section of prose.
    """
    blocks: list[_Block] = []
    stack: list[tuple[int, str]] = []
    page: int | None = None
    sheet: str | None = None
    table_index: int | None = None

    def new_block(kind: str) -> _Block:
        return _Block(
            kind=kind,
            heading_path=[title for _, title in stack],
            page=page,
            sheet=sheet,
            table_index=table_index,
        )

    def flush(blk: _Block) -> None:
        if any(line.strip() for line in blk.lines):
            blocks.append(blk)

    current = new_block("prose")
    for raw_line in text.splitlines():
        m = HEADING_RE.match(raw_line.rstrip())
        if not m:
            current.lines.append(raw_line)
            continue
        title = m.group(2).strip()
        flush(current)
        sheet_m = SHEET_RE.match(title)
        table_m = TABLE_RE.match(title)
        page_m = PAGE_RE.match(title)
        if sheet_m:
            sheet = sheet_m.group(1).strip()
            table_index = None
            stack = []
            current = new_block("sheet")
        elif table_m:
            table_index = int(table_m.group(1))
            current = new_block("table")
        elif page_m:
            page = int(page_m.group(1))
            sheet = None
            table_index = None
            stack = []
            current = new_block("prose")
        else:
            # An ordinary heading (docx Heading N/Title styles, a pptx "Slide
            # N" marker, or a .md file's own headings) — closes any open
            # table/sheet block and nests into the generic heading path.
            sheet = None
            table_index = None
            level = len(m.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current = new_block("prose")
    flush(current)
    return blocks


def _prose_context(title: str, block: _Block) -> str:
    parts = [title]
    if block.heading_path:
        parts.append(" > ".join(block.heading_path))
    if block.page is not None:
        parts.append(f"page {block.page}")
    return " — ".join(parts)


def _find_split_point(text: str, target: int) -> int:
    """Where to end a hard-split slice of `text` that starts at index 0 and is
    aiming for roughly `target` chars: prefer the last sentence end (`. `,
    `! `, `? `) or line break within `HARD_SPLIT_BOUNDARY_WINDOW` chars before
    `target`, so a slice doesn't cut mid-sentence when a nearby boundary is
    available. Falls back to the last space before `target` (never mid-word),
    and only cuts at the raw char budget if there is no space either.
    """
    if target >= len(text):
        return len(text)
    window_start = max(0, target - HARD_SPLIT_BOUNDARY_WINDOW)
    best = -1
    for m in SENTENCE_END_RE.finditer(text, window_start, target):
        best = m.end()
    if best != -1:
        return best
    space = text.rfind(" ", window_start, target)
    if space != -1:
        return space + 1
    return target


def _hard_split(text: str) -> list[str]:
    """Split one oversize paragraph into TARGET_CHARS-ish slices, carrying
    the same `OVERLAP_CHARS` from the tail of each slice into the next as the
    paragraph-packing path above, and preferring a sentence/line boundary
    near the budget (`_find_split_point`) over cutting mid-word."""
    if len(text) <= TARGET_CHARS:
        return [text]
    slices: list[str] = []
    start = 0
    while start < len(text):
        target_end = start + TARGET_CHARS
        end = _find_split_point(text, target_end) if target_end < len(text) else len(text)
        if end <= start:
            end = min(len(text), target_end)
        slices.append(text[start:end].strip())
        if end >= len(text):
            break
        # Guarantee forward progress even if a boundary landed unusually
        # close to `start` — the next slice starts no earlier than start + 1.
        start = max(start + 1, end - OVERLAP_CHARS)
    return slices


def _chunk_prose_block(title: str, block: _Block) -> list[ChunkDraft]:
    text = "\n".join(block.lines).strip("\n")
    if not text.strip():
        return []
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text.strip()]

    locator: dict = {"heading_path": block.heading_path}
    if block.page is not None:
        locator["page"] = block.page
    context = _prose_context(title, block)

    def make(body: str) -> ChunkDraft:
        return ChunkDraft(
            kind="text",
            locator=dict(locator),
            context=context,
            body=body,
            token_est=_token_est(body),
        )

    drafts: list[ChunkDraft] = []
    current_paras: list[str] = []
    current_len = 0
    i = 0
    while i < len(paragraphs):
        p = paragraphs[i]
        if len(p) > int(TARGET_CHARS * 1.5):
            # One giant paragraph with no internal break (typical of a
            # pypdf-extracted page) — hard-split at the character budget so
            # it does not become one oversized chunk. Flush whatever prose is
            # already pending FIRST: checking this regardless of
            # `current_paras` (rather than only when it's empty) is what
            # keeps a short paragraph ahead of this one — a page title, say —
            # from being swallowed into the oversize paragraph instead of
            # becoming its own chunk.
            if current_paras:
                drafts.append(make("\n\n".join(current_paras)))
                current_paras = []
                current_len = 0
            drafts.extend(make(piece) for piece in _hard_split(p))
            i += 1
            continue
        if current_paras and current_len + len(p) + 2 > TARGET_CHARS and current_len >= MIN_CHARS:
            drafts.append(make("\n\n".join(current_paras)))
            overlap = current_paras[-1][-OVERLAP_CHARS:]
            current_paras = [overlap] if overlap.strip() else []
            current_len = len(current_paras[0]) if current_paras else 0
            continue
        current_paras.append(p)
        current_len += len(p) + 2
        i += 1
    if current_paras:
        drafts.append(make("\n\n".join(current_paras)))
    return drafts


def _chunk_rows(
    *, title: str, rows: list[str], kind: str, locator_base: dict, context_label: str
) -> list[ChunkDraft]:
    """Shared row-block chunker for both oversized tables and every
    spreadsheet extract: `rows[0]` is the header, repeated verbatim atop every
    block, and the rest are packed under `ROWS_CHAR_CEILING`. No
    `MAX_CHUNKS_PER_DOCUMENT` check here — this may be called once per
    table/sheet in a document with several of them, and only the caller
    (`chunk_document`) sees every block's drafts together, so only it can cap
    the document as a whole and say so with a truncation marker.
    """
    if not rows:
        return []
    header = rows[0]
    data_rows = rows[1:]
    if not data_rows:
        locator = {**locator_base, "row_start": 1, "row_end": 1}
        return [
            ChunkDraft(
                kind=kind,
                locator=locator,
                context=context_label,
                body=header,
                token_est=_token_est(header),
            )
        ]
    drafts: list[ChunkDraft] = []
    i = 0
    while i < len(data_rows):
        block_rows = [header]
        chars = len(header)
        start_i = i
        while i < len(data_rows) and chars + len(data_rows[i]) + 1 <= ROWS_CHAR_CEILING:
            block_rows.append(data_rows[i])
            chars += len(data_rows[i]) + 1
            i += 1
        if i == start_i:  # a single row bigger than the ceiling: take it anyway
            block_rows.append(data_rows[i])
            i += 1
        row_start, row_end = start_i + 2, i + 1  # 1-based; row 1 is the header
        body = "\n".join(block_rows)
        locator = {**locator_base, "row_start": row_start, "row_end": row_end}
        context = f"{context_label}, rows {row_start}-{row_end}"
        drafts.append(
            ChunkDraft(
                kind=kind, locator=locator, context=context, body=body, token_est=_token_est(body)
            )
        )
    return drafts


def _chunk_table_block(title: str, block: _Block) -> list[ChunkDraft]:
    rows = [line for line in block.lines if line.strip()]
    if not rows:
        return []
    locator_base: dict = {"table_index": block.table_index}
    if block.page is not None:
        locator_base["page"] = block.page
    label = f"{title} — Table {block.table_index}"
    if block.page is not None:
        label += f", page {block.page}"
    whole = "\n".join(rows)
    if len(whole) <= ROWS_CHAR_CEILING or len(rows) == 1:
        # Kept whole: it fits, or there is nothing but a header row to split.
        locator = {**locator_base, "row_start": 1, "row_end": len(rows)}
        return [
            ChunkDraft(
                kind="table",
                locator=locator,
                context=label,
                body=whole,
                token_est=_token_est(whole),
            )
        ]
    return _chunk_rows(
        title=title, rows=rows, kind="table", locator_base=locator_base, context_label=label
    )


def _chunk_sheet_block(title: str, block: _Block) -> list[ChunkDraft]:
    rows = [line for line in block.lines if line.strip()]
    if not rows:
        return []
    locator_base = {"sheet": block.sheet}
    label = f"{title} — Sheet '{block.sheet}'" if block.sheet else f"{title} — Sheet"
    return _chunk_rows(
        title=title, rows=rows, kind="sheet_block", locator_base=locator_base, context_label=label
    )


def chunk_document(filename: str, text: str, meta: dict | None = None) -> list[ChunkDraft]:
    """Pure: `filename` + `extracted_text` (+ its extraction `meta`, unused
    today but threaded through for a future chunker that wants it) in, an
    ordered list of `ChunkDraft`s out. No DB, no document id — `ensure_chunks`
    is what turns these into `DocumentChunk` rows.

    `MAX_CHUNKS_PER_DOCUMENT` is applied exactly once here, over every draft
    the whole document produced (never inside a per-block helper — see
    `_chunk_rows`), and a truncated document gets one extra marker chunk
    appended so a search over it can tell the cut happened instead of
    silently treating the last kept chunk as the end of the document.
    """
    if not text or not text.strip():
        return []
    title = filename
    if filename.lower().endswith((".csv", ".tsv")):
        block = _Block(
            kind="sheet",
            heading_path=[],
            page=None,
            sheet=None,
            table_index=None,
            lines=text.splitlines(),
        )
        drafts = _chunk_sheet_block(title, block)
    else:
        drafts = []
        for block in _parse_blocks(text):
            if block.kind == "sheet":
                drafts.extend(_chunk_sheet_block(title, block))
            elif block.kind == "table":
                drafts.extend(_chunk_table_block(title, block))
            else:
                drafts.extend(_chunk_prose_block(title, block))
    truncated = len(drafts) > MAX_CHUNKS_PER_DOCUMENT
    drafts = drafts[:MAX_CHUNKS_PER_DOCUMENT]
    if truncated:
        marker = TRUNCATION_MARKER_TEMPLATE.format(n=MAX_CHUNKS_PER_DOCUMENT)
        drafts.append(
            ChunkDraft(
                kind="text",
                locator={},
                context=title,
                body=marker,
                token_est=_token_est(marker),
            )
        )
    for ordinal, draft in enumerate(drafts):
        draft.ordinal = ordinal
    return drafts


# ── persistence: chunk at first use, exactly once ────────────────────────────
async def ensure_chunks(db: AsyncSession, document: Document) -> list[DocumentChunk]:
    """Chunk `document` if it has no chunks yet, and persist them (committed —
    see `engine/tools.py::run_harness_task` for the same pattern: a tool that
    needs durable state now, not just at the end of the run, commits it
    itself). Returns the newly created rows, or `[]` when the document was
    already chunked, had no text, or chunking/persisting failed — this is a
    best-effort backfill, not a step a search may fail on.
    """
    already = (
        await db.execute(
            select(DocumentChunk.id).where(DocumentChunk.document_id == document.id).limit(1)
        )
    ).first()
    if already is not None or not document.extracted_text:
        return []
    try:
        drafts = chunk_document(document.filename, document.extracted_text, document.meta)
    except Exception:
        log.warning("chunking failed for document %s", document.id, exc_info=True)
        return []
    if not drafts:
        return []

    dialect = db.get_bind().dialect.name
    rows: list[DocumentChunk] = []
    for draft in drafts:
        row = DocumentChunk(
            document_id=document.id,
            ordinal=draft.ordinal,
            kind=draft.kind,
            locator=draft.locator,
            context=draft.context,
            body=draft.body,
            token_est=draft.token_est,
        )
        if dialect == "postgresql":
            # Set once, at insert, rather than a DDL GENERATED column — see the
            # long comment on DocumentChunk.search_vector for why: chunk rows
            # are write-once, so this can never go stale the way a hand-set
            # column on a mutable row could.
            row.search_vector = func.to_tsvector("english", f"{draft.context} {draft.body}")
        db.add(row)
        rows.append(row)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        log.warning("failed to persist chunks for document %s", document.id, exc_info=True)
        return []
    return rows


# ── ranking ───────────────────────────────────────────────────────────────────
@dataclass
class SearchHit:
    document_id: uuid.UUID
    filename: str
    kind: str
    locator: dict
    context: str
    snippet: str
    score: float
    # The chunk's ordinal within its document — printed in the hit line
    # (`engine/tools.py::search_documents`) so `read_document(chunk_ordinal=…)`
    # can jump straight to it; without this a search result names a chunk's
    # location but not the one number that makes it addressable.
    ordinal: int


def _tokenize(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _bm25_scores(query: str, corpus: list[str], *, k1: float = 1.5, b: float = 0.75) -> list[float]:
    """A minimal Okapi BM25 over `corpus` (already-lowercased-and-tokenized
    internally), scored against `query`. No new dependency: this is the whole
    algorithm, not a wrapper around one. Scores are always >= 0; a document
    sharing no query term with any other document in the corpus scores 0
    exactly (never negative — the classic BM25 idf can go negative for a term
    every document contains, `idf` below floors it at a small positive value
    instead, which only ever matters for a corpus of a handful of chunks)."""
    # OR semantics, explicitly: a chunk sharing even one query term with the
    # corpus gets a (possibly small) positive score below — there is no
    # requirement that every term be present, unlike Postgres's
    # `websearch_to_tsquery` primary path in `_rank_postgres`, which ANDs
    # bare terms. `_rank_postgres` compensates for that gap with its own OR
    # fallback; this path never needed one; it ORs by construction.
    query_terms = _tokenize(query)
    if not query_terms or not corpus:
        return [0.0] * len(corpus)
    docs_tokens = [_tokenize(doc) for doc in corpus]
    doc_lens = [len(toks) for toks in docs_tokens]
    n_docs = len(corpus)
    avgdl = (sum(doc_lens) / n_docs) if n_docs else 0.0

    doc_freq: Counter[str] = Counter()
    for toks in docs_tokens:
        doc_freq.update(set(toks))

    idf: dict[str, float] = {}
    for term in set(query_terms):
        n_qi = doc_freq.get(term, 0)
        idf[term] = max(1e-6, math.log(1 + (n_docs - n_qi + 0.5) / (n_qi + 0.5)))

    scores: list[float] = []
    for toks, dl in zip(docs_tokens, doc_lens):
        tf = Counter(toks)
        score = 0.0
        for term in query_terms:
            freq = tf.get(term, 0)
            if not freq:
                continue
            denom = freq + k1 * (1 - b + b * (dl / avgdl if avgdl else 1.0))
            score += idf[term] * (freq * (k1 + 1)) / denom
        scores.append(score)
    return scores


def _apply_phrase_boost(score: float, text: str, query: str) -> float:
    """The lexical boost both ranking paths share: a chunk containing the
    query as one contiguous phrase outranks one that merely contains the same
    words scattered across it, by the same multiplier regardless of dialect."""
    phrase = " ".join(query.lower().split())
    haystack = " ".join(text.lower().split())
    if phrase and phrase in haystack:
        return score * PHRASE_BOOST_MULTIPLIER
    return score


def _make_snippet(body: str, query: str) -> str:
    terms = _tokenize(query)
    low = body.lower()
    pos = None
    for term in terms:
        i = low.find(term)
        if i != -1 and (pos is None or i < pos):
            pos = i
    if pos is None:
        snippet = body[:SNIPPET_CHARS]
        return snippet + ("..." if len(body) > SNIPPET_CHARS else "")
    half = SNIPPET_CHARS // 2
    start = max(0, pos - half)
    end = min(len(body), start + SNIPPET_CHARS)
    return ("..." if start > 0 else "") + body[start:end] + ("..." if end < len(body) else "")


def _to_hit(chunk: DocumentChunk, filename: str, score: float, query: str) -> SearchHit:
    return SearchHit(
        document_id=chunk.document_id,
        filename=filename,
        kind=chunk.kind,
        locator=chunk.locator,
        context=chunk.context,
        snippet=_make_snippet(chunk.body, query),
        score=round(float(score), 4),
        ordinal=chunk.ordinal,
    )


def _or_tsquery_expr(query: str) -> str | None:
    """Build a `to_tsquery`-ready OR expression (`'term1' | 'term2' | …`) out
    of `query`'s tokens, for `_rank_postgres`'s fallback below. Each term is
    single-quoted as a tsquery-syntax literal — belt-and-braces safety since
    `_tokenize` already only ever emits `[a-z0-9]+` tokens with no operator or
    quote characters to escape, but any embedded `'` is still doubled the way
    a SQL string literal would be, so this can never be read as tsquery
    operator syntax regardless of what `_tokenize` does in the future. Quoting
    a single term this way does not disable stemming — a single-quoted token
    is still normalized through the `english` dictionary, just like a bare
    one. Returns None for a query with no tokenizable terms at all.
    """
    terms = list(dict.fromkeys(_tokenize(query)))  # de-duplicated, order preserved
    if not terms:
        return None
    return " | ".join("'" + t.replace("'", "''") + "'" for t in terms)


async def _rank_postgres(
    db: AsyncSession, document_ids: list[uuid.UUID], query: str, max_results: int
) -> list[SearchHit]:
    """Postgres ranking. `websearch_to_tsquery` is the primary query — it
    accepts the same free-text a person would type (quoted phrases, `-word`
    exclusion, a literal `OR`) and, unlike `plainto_tsquery`, never raises on
    unbalanced input, but bare words are still ANDed together same as
    `plainto_tsquery`: a 5-term query only matches a chunk containing all 5.
    That is a real gap against the sqlite BM25 path below, which scores by
    term overlap and never requires every term — so when the primary query
    returns nothing, we retry with an OR of the same tokens (`_or_tsquery_expr`)
    before giving up, matching BM25's "any shared term counts" behavior. This
    changes *recall*, not ranking parity: see the module docstring for why
    Postgres and BM25 still don't score alike even when both return a hit.
    """

    async def _search(tsquery) -> list:
        rank = func.ts_rank_cd(DocumentChunk.search_vector, tsquery)
        stmt = (
            select(DocumentChunk, Document.filename, rank.label("rank"))
            .join(Document, Document.id == DocumentChunk.document_id)
            .where(DocumentChunk.document_id.in_(document_ids))
            .where(DocumentChunk.search_vector.op("@@")(tsquery))
            .order_by(rank.desc())
            .limit(max(max_results * 4, max_results))
        )
        return (await db.execute(stmt)).all()

    rows = await _search(func.websearch_to_tsquery("english", query))
    if not rows:
        or_expr = _or_tsquery_expr(query)
        if or_expr is not None:
            rows = await _search(func.to_tsquery("english", or_expr))

    scored = [
        (
            _apply_phrase_boost(float(rank_value or 0.0), f"{chunk.context} {chunk.body}", query),
            chunk,
            filename,
        )
        for chunk, filename, rank_value in rows
    ]
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        _to_hit(chunk, filename, score, query) for score, chunk, filename in scored[:max_results]
    ]


async def _rank_bm25(
    db: AsyncSession, document_ids: list[uuid.UUID], query: str, max_results: int
) -> list[SearchHit]:
    stmt = (
        select(DocumentChunk, Document.filename)
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(DocumentChunk.document_id.in_(document_ids))
    )
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []
    corpus = [f"{chunk.context} {chunk.body}" for chunk, _ in rows]
    raw_scores = _bm25_scores(query, corpus)
    scored = []
    for (chunk, filename), text, raw in zip(rows, corpus, raw_scores):
        if raw <= 0:
            continue
        scored.append((_apply_phrase_boost(raw, text, query), chunk, filename))
    scored.sort(key=lambda t: t[0], reverse=True)
    return [
        _to_hit(chunk, filename, score, query) for score, chunk, filename in scored[:max_results]
    ]


async def rank_chunks(
    db: AsyncSession, document_ids: list[uuid.UUID], query: str, max_results: int = 8
) -> list[SearchHit]:
    """Rank the chunks belonging to `document_ids` (an explicit allowlist —
    the caller's project/run scoping, never widened here) against `query`.
    Dialect-detected per call rather than cached, since the same code may run
    against a real Postgres deployment and a sqlite test in the same process.
    """
    document_ids = list(document_ids)
    query = (query or "").strip()
    if not document_ids or not query:
        return []
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        hits = await _rank_postgres(db, document_ids, query, max_results)
    else:
        hits = await _rank_bm25(db, document_ids, query, max_results)
    return await rerank_seam(hits, query)


def describe_locator(kind: str, locator: dict) -> str:
    """A human/model-readable rendering of a chunk's `locator`, for the tool
    result text — see `engine/tools.py::search_documents`."""
    if kind == "sheet_block":
        return f"sheet '{locator.get('sheet')}' rows {locator.get('row_start')}-{locator.get('row_end')}"
    if kind == "table":
        parts = [f"table {locator.get('table_index')}"]
        if locator.get("row_start") is not None:
            parts.append(f"rows {locator.get('row_start')}-{locator.get('row_end')}")
        if locator.get("page") is not None:
            parts.append(f"page {locator.get('page')}")
        return ", ".join(parts)
    parts = []
    if locator.get("page") is not None:
        parts.append(f"page {locator.get('page')}")
    heading_path = locator.get("heading_path") or []
    if heading_path:
        parts.append(" > ".join(heading_path))
    return ", ".join(parts) if parts else "document body"


# ── seam for a future reranker / embeddings pass ─────────────────────────────
async def rerank_seam(hits: list[SearchHit], query: str) -> list[SearchHit]:
    """Identity today — see the "No embeddings in this pass" section of the
    module docstring. `rank_chunks` already routes its final hit list through
    this function, so wiring in a real reranker later is a one-function change:
    widen the over-fetch in `_rank_postgres`/`_rank_bm25`, then re-score and
    truncate to the caller's `max_results` here instead of there.
    """
    return hits
