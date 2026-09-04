"""Document text extraction (PDF/DOCX/CSV/MD/TXT) and the shared ingestion
step — bounded extraction plus building the `Document` row — that both
`api/documents.py`'s upload endpoint and the connections import endpoint
(`POST /api/projects/{project_id}/documents/import`) call, so the two paths
can never diverge on how a document actually gets ingested once its bytes
are on disk. Only how the bytes arrived differs between them.
"""
from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from pathlib import Path

from tret.db.models import Document

log = logging.getLogger("tret.documents")

# The one size cap for every path that ends up in `ingest_document` — a
# manual upload (`api/documents.py::upload_document`, which streams the body
# and enforces this mid-stream) and a provider import (`api/documents.py::
# _import_one`, via `services/connections.py::IMPORT_MAX_BYTES`, which holds
# the whole downloaded file in memory before it ever reaches here). 25MB is
# sized to the import path's memory cost, not the upload path's — streaming
# could afford a much larger cap, but importing cannot, and both paths must
# share one number rather than silently diverge on what "too large" means.
MAX_DOCUMENT_BYTES = 25 * 1024 * 1024

# Extraction bounds. A 25MB CSV of one-character rows, or a PDF crafted to
# expand, must not turn into an unbounded string in a JSONB column or an
# unbounded stretch of CPU inside a request.
MAX_EXTRACTED_CHARS = 2_000_000
EXTRACTION_TIMEOUT_SECONDS = 30.0


def extract_text(filename: str, data: bytes) -> tuple[str, dict]:
    """Returns (text, meta). Raises ValueError for unsupported types."""
    lower = filename.lower()
    if lower.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
        return "\n\n".join(pages), {"pages": len(pages)}
    if lower.endswith(".docx"):
        import docx

        document = docx.Document(io.BytesIO(data))
        parts = [p.text for p in document.paragraphs]
        for table in document.tables:
            for row in table.rows:
                parts.append(" | ".join(cell.text for cell in row.cells))
        return "\n".join(parts), {"paragraphs": len(document.paragraphs)}
    if lower.endswith((".csv", ".tsv")):
        delim = "\t" if lower.endswith(".tsv") else ","
        text = data.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        rendered = "\n".join(" | ".join(row) for row in rows)
        return rendered, {"rows": len(rows), "columns": rows[0] if rows else []}
    if lower.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
        return data.decode("utf-8", errors="replace"), {}
    raise ValueError(f"Unsupported file type: {filename} (supported: pdf, docx, csv, tsv, md, txt)")


async def extract_bounded(filename: str, data: bytes) -> tuple[str, dict, str]:
    """(text, meta, status). Bounded in wall time and in characters returned.

    Extraction is third-party parsing of third-party bytes: it runs off the event
    loop so one slow document cannot stall every other request, it is abandoned
    after EXTRACTION_TIMEOUT_SECONDS, its output is truncated, and *any* parser
    failure is recorded on the row rather than raised — a corrupt PDF is a
    document with no text, not a 500 on upload.

    "Abandoned" is precise: Python cannot cancel a running thread, so a pathological
    parser finishes in the background while the request moves on. What is bounded is
    the request, and the memory that thread holds (one document, at most the size cap
    its caller enforced before getting here).
    """
    try:
        text, meta = await asyncio.wait_for(
            asyncio.to_thread(extract_text, filename, data), EXTRACTION_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        return "", {"error": f"text extraction timed out after {EXTRACTION_TIMEOUT_SECONDS:.0f}s"}, "failed"
    except ValueError as e:  # unsupported type: the message is for the user
        return "", {"error": str(e)}, "failed"
    except Exception as e:  # a parser blowing up on hostile bytes
        log.warning("text extraction failed for %r: %s", filename, e)
        return "", {"error": f"text extraction failed: {type(e).__name__}"}, "failed"
    if len(text) > MAX_EXTRACTED_CHARS:
        meta = {**meta, "truncated": True, "extracted_chars": MAX_EXTRACTED_CHARS}
        text = text[:MAX_EXTRACTED_CHARS]
    return text, meta, "done"


async def ingest_document(
    *,
    project_id: uuid.UUID,
    filename: str,
    content_type: str,
    storage_path: Path,
    sha256: str,
    byte_size: int,
    data: bytes,
    uploaded_by: uuid.UUID | None,
    source_kind: str = "upload",
    meta_extra: dict | None = None,
) -> Document:
    """Extract `data`'s text (bounded) and build the `Document` row for it.

    `data` is already written to `storage_path` by the caller — an upload
    streams it there while consuming an untrusted multipart body, an import
    downloads it whole from a provider — so this is purely "bytes in, a row
    ready to add to the session out". Not committed and not added to the
    session: the caller owns the transaction (the upload endpoint commits
    once; the import endpoint commits per item so one bad file in a batch
    cannot roll back the others).
    """
    text, meta, status = await extract_bounded(filename, data)
    if meta_extra:
        meta = {**meta, **meta_extra}
    return Document(
        project_id=project_id,
        filename=filename,
        content_type=content_type,
        byte_size=byte_size,
        storage_path=str(storage_path),
        extracted_text=text or None,  # None = nothing was extracted
        extraction_status=status,
        meta=meta,
        sha256=sha256,
        source_kind=source_kind,
        uploaded_by=uploaded_by,
    )
