"""A fetched page becomes a Document — the whole reason `fetch_url` exists.

The tempting implementation returns the page text straight to the model. It is
also the one that quietly breaks this product: prose in a tool result has no
provenance, cannot be re-read by a reviewer, and disappears the moment the
transcript is trimmed. Someone auditing a deliverable six months from now needs
to see *what the page said when the run read it*, not what it says today.

So the bytes are written to the storage directory, hashed, and recorded as a
`Document` with `source_kind='web'` and the fetch provenance in `meta`. The model
reads it with `read_document`, the same tool it uses for an uploaded PDF, and
every read is in the audit trail for free. The snapshot is also what makes
`BENCH_EGRESS_RESEARCH=replay` possible: an audit can re-run the reasoning
without re-running the internet.

A web Document is emphatically not an upload:

* `source_kind` says so, on the row, queryable;
* nothing it contains is registered in `ctx.retrieved_values`, so the
  cited-values check in `engine/validation.py` still refuses any number that
  came from it. Web pages remain unable to back a verdict, exactly as before this
  module existed. Promoting one into a dataset is an operator's reviewed act.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.config import get_settings
from bench.db.models import Document
from bench.net.fetch.extract import html_to_text
from bench.net.fetch.fetch import FetchedPage
from bench.services.documents import extract_text

log = logging.getLogger("bench.net")

SOURCE_KIND_WEB = "web"
SOURCE_KIND_UPLOAD = "upload"

# Matches the upload path's ceiling (`api/documents.py::MAX_EXTRACTED_CHARS`), for
# the same reason: extracted text lands in a column and then in a prompt.
MAX_EXTRACTED_CHARS = 2_000_000
EXTRACTION_TIMEOUT_SECONDS = 30.0

_UNSAFE = re.compile(r"[^a-z0-9._-]+")


def snapshot_filename(url: str, extension: str) -> str:
    """A legible filename for a fetched page: `host-path-slug.ext`.

    Not the upload sanitiser (`api/documents.py::safe_filename`) and not a
    substitute for it — that one defends against a filename a client chose, this
    one builds a name from a URL bench already checked. Both end up under the
    storage directory prefixed by a content hash, so neither can name a path.
    """
    parts = urlsplit(url)
    stem = f"{parts.hostname or 'page'}{parts.path or ''}".lower().rstrip("/")
    stem = _UNSAFE.sub("-", stem).strip("-.") or "page"
    return f"{stem[:100]}{extension}"


async def _extract(page: FetchedPage, filename: str) -> tuple[str, dict, str]:
    """(text, meta, status), bounded in wall time exactly as uploads are."""
    if page.extension == ".html":
        html = page.body.decode("utf-8", errors="replace")
        text, title = await asyncio.to_thread(html_to_text, html)
        return text, {"title": title}, "done"
    try:
        text, meta = await asyncio.wait_for(
            asyncio.to_thread(extract_text, filename, page.body), EXTRACTION_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        return "", {"error": f"text extraction timed out after {EXTRACTION_TIMEOUT_SECONDS:.0f}s"}, "failed"
    except Exception as e:  # a parser blowing up on hostile bytes is not a run failure
        log.warning("web extraction failed for %r: %s", page.url, e)
        return "", {"error": f"text extraction failed: {type(e).__name__}"}, "failed"
    return text, meta, "done"


async def store_snapshot(
    db: AsyncSession, page: FetchedPage, project_id: uuid.UUID, run_id: uuid.UUID | None
) -> Document:
    """Write the bytes, extract the text, and add the Document row. Caller commits."""
    storage_dir = Path(get_settings().storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    filename = snapshot_filename(page.url, page.extension)
    storage_path = (storage_dir / f"{page.sha256}-{filename}").resolve()
    if storage_path.parent != storage_dir:  # belt and braces over snapshot_filename
        raise ValueError("refusing to write a snapshot outside the storage directory")
    await asyncio.to_thread(storage_path.write_bytes, page.body)

    text, meta, status = await _extract(page, filename)
    truncated_text = len(text) > MAX_EXTRACTED_CHARS
    if truncated_text:
        text = text[:MAX_EXTRACTED_CHARS]

    doc = Document(
        project_id=project_id,
        filename=filename,
        content_type=page.content_type,
        byte_size=len(page.body),
        storage_path=str(storage_path),
        extracted_text=text,
        extraction_status=status,
        sha256=page.sha256,
        source_kind=SOURCE_KIND_WEB,
        # Everything a reviewer needs to judge the source without leaving the row:
        # what was asked for, what actually answered, when, and whether what the
        # model read was the whole of it.
        meta={
            **meta,
            "source": SOURCE_KIND_WEB,
            "url": page.url,
            "requested_url": page.requested_url,
            "redirects": list(page.redirects),
            "http_status": page.status_code,
            "fetched_by_run": str(run_id) if run_id else None,
            "body_truncated": page.truncated,
            "text_truncated": truncated_text,
        },
        uploaded_by=None,  # no human put this here; that is the point of source_kind
    )
    db.add(doc)
    await db.flush()
    return doc


# How far back `replay` looks for a snapshot of a URL. Bounded for the same
# reason `api/analytics.py` bounds its transcript scan: the filter is a JSON
# comparison done in Python (no dialect-specific JSONB predicates), so it reads a
# window rather than the table.
REPLAY_SCAN_LIMIT = 2000


async def find_snapshot(db: AsyncSession, project_id: uuid.UUID, url: str) -> Document | None:
    """The most recent snapshot of `url` in this project, if one was ever taken.

    Matches the URL that was *requested* as well as the one finally fetched, so a
    replay of a run that followed a redirect finds the page under the name the
    model asked for.
    """
    rows = (
        await db.execute(
            select(Document)
            .where(Document.project_id == project_id, Document.source_kind == SOURCE_KIND_WEB)
            .order_by(Document.created_at.desc())
            .limit(REPLAY_SCAN_LIMIT)
        )
    ).scalars().all()
    for doc in rows:
        meta = doc.meta or {}
        if url in (meta.get("url"), meta.get("requested_url")):
            return doc
    return None
