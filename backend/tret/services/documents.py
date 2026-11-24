"""Document text extraction (PDF/DOCX/XLSX/PPTX/CSV/MD/TXT/HTML/zip listings) and the shared
ingestion step — bounded extraction plus building the `Document` row — that
both `api/documents.py`'s upload endpoint and the connections import endpoint
(`POST /api/projects/{project_id}/documents/import`) call, so the two paths
can never diverge on how a document actually gets ingested once its bytes
are on disk. Only how the bytes arrived differs between them.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import csv
import io
import logging
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
import zlib
from pathlib import Path
from typing import Any, Iterator

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
# Captured annual reports run to tens of MB; on a loaded 1-vCPU machine 30 s
# left them with no text at all, so a caller may scale the budget with size.
EXTRACTION_SECONDS_PER_MIB = 4.0
EXTRACTION_MAX_SECONDS = 600.0
# One pdftotext run over a whole PDF. Poppler does a 117-page report in seconds
# where pypdf takes minutes on a 1-vCPU machine, so it is the preferred reader.
PDFTOTEXT_TIMEOUT_SECONDS = 120.0
# pypdf (page count, fallback) is only started when this much of the caller's
# bound is left: the larger of a fraction of it and a fixed floor.
PYPDF_HEADROOM_FRACTION = 0.2
PYPDF_HEADROOM_MIN_SECONDS = 10.0


def extraction_budget_seconds(byte_size: int) -> float:
    """The wall-clock extraction budget for a document of `byte_size` bytes:
    30 s plus 4 s per MiB, never below 30 s nor above EXTRACTION_MAX_SECONDS
    (600 s, reached at about 142 MiB; 30 MiB gets 150 s)."""
    scaled = EXTRACTION_TIMEOUT_SECONDS + byte_size / 2**20 * EXTRACTION_SECONDS_PER_MIB
    return min(EXTRACTION_MAX_SECONDS, max(EXTRACTION_TIMEOUT_SECONDS, scaled))


_clock = time.monotonic  # a seam for tests

# The wall-clock bound of whoever is waiting on `extract_text` (`extract_bounded`,
# the web snapshot). Poppler's own timeout is never longer than this, so a slow
# PDF ends as a recorded timeout rather than as a thread still running pypdf
# after the caller has given up. A ContextVar, because the call crosses
# `asyncio.to_thread` (which copies the context) and `extract_text`'s signature
# is shared with every other format.
_pdf_budget: contextvars.ContextVar[float | None] = contextvars.ContextVar("pdf_budget", default=None)


@contextlib.contextmanager
def pdf_budget(seconds: float) -> Iterator[None]:
    """Bound any PDF text extraction started inside this block to `seconds`."""
    token = _pdf_budget.set(seconds)
    try:
        yield
    finally:
        _pdf_budget.reset(token)

# .xlsx and .pptx are zip containers that openpyxl/python-pptx inflate while
# parsing. A small compressed file can declare an enormous uncompressed size
# per member (a zip bomb), so the total is summed straight from the zip's
# central directory — no inflation, no parsing — before either library ever
# touches the bytes.
#
# .xlsx keeps the full 200MB allowance: `_extract_xlsx` reads `read_only`,
# streaming row by row rather than building an in-memory object model, so a
# large-but-legitimate workbook's memory cost stays bounded regardless of
# how much the zip expands to. `_extract_pptx` has no such streaming path —
# `python-pptx`'s `Presentation()` parses the whole package eagerly into
# memory before extraction ever gets to bound anything — so a pptx gets a
# lower allowance instead of trusting the same 200MB ceiling to be safe for
# a parser that cannot stream. `_extract_docx` shares that lower allowance for
# the same reason: `python-docx`'s `Document()` is just as eager.
MAX_ZIP_UNCOMPRESSED_BYTES = 200 * 1024 * 1024
MAX_PPTX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024


def _refuse_zip_bombs(
    filename: str,
    data: bytes,
    max_uncompressed_bytes: int | None = None,
    *,
    verify_members: bool = True,
) -> None:
    """Refuse a zip whose members expand past `max_uncompressed_bytes`.

    The declared sizes in the central directory are summed first (cheap, no
    inflation). That alone is not enough for a file a library will parse:
    `file_size` is the writer's claim, and `zipfile.ZipFile.read()` (what
    python-docx, python-pptx and openpyxl call) inflates a whole deflate
    member before trimming it to that claim, so a ~1 MB member that declares
    4 KB can still expand to gigabytes in memory. With `verify_members`, every
    member is also inflated here in bounded chunks and refused the moment it
    produces more than it declared, so the parser that runs next can only
    ever see the declared, already-capped sizes.

    `verify_members=False` is for callers that never hand a member to an
    unbounded reader (the zip listing reads only the central directory, and
    `read_zip_member` reads one member with a bounded `read(n)`).
    """
    import zipfile

    if max_uncompressed_bytes is None:
        max_uncompressed_bytes = MAX_ZIP_UNCOMPRESSED_BYTES

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        infos = zf.infolist()
    total = sum(info.file_size for info in infos)
    if total > max_uncompressed_bytes:
        raise ValueError(
            f"{filename}: archive expands to {total} bytes, over the "
            f"{max_uncompressed_bytes} byte limit"
        )
    if verify_members:
        for info in infos:
            _refuse_member_past_declared_size(filename, data, info)


_ZIP_LOCAL_HEADER = struct.Struct("<4s2B4HL2L2H")  # zipfile's structFileHeader (30 bytes)
_ZIP_INFLATE_CHUNK = 1024 * 1024


def _refuse_member_past_declared_size(filename: str, data: bytes, info: Any) -> None:
    """Inflate one member in `_ZIP_INFLATE_CHUNK` steps, never holding more
    than one chunk, and raise ValueError if it produces more than its
    declared `file_size`. Reads the same compressed bytes `zipfile` would
    (local header at `header_offset`, then `compress_size` bytes)."""
    import zipfile

    if info.flag_bits & 0x1:
        raise ValueError(f"{filename}: member {info.filename!r} is encrypted")
    if info.compress_type == zipfile.ZIP_STORED:
        return  # stored bytes are not inflated: zipfile reads at most compress_size of them
    if info.compress_type != zipfile.ZIP_DEFLATED:
        # bzip2/lzma members inflate through the same unbounded read; Office
        # files never use them, so refusing costs nothing legitimate.
        raise ValueError(f"{filename}: member {info.filename!r} uses an unsupported compression method")
    start = info.header_offset
    header = data[start : start + _ZIP_LOCAL_HEADER.size]
    if len(header) != _ZIP_LOCAL_HEADER.size or header[:4] != b"PK\x03\x04":
        raise ValueError(f"{filename}: member {info.filename!r} has a corrupt local header")
    fields = _ZIP_LOCAL_HEADER.unpack(header)
    body = start + _ZIP_LOCAL_HEADER.size + fields[10] + fields[11]  # + name and extra lengths
    pending = memoryview(data)[body : body + info.compress_size]
    inflater = zlib.decompressobj(-zlib.MAX_WBITS)
    produced = 0
    try:
        while not inflater.eof:
            out = inflater.decompress(pending, _ZIP_INFLATE_CHUNK)
            produced += len(out)
            if produced > info.file_size:
                raise ValueError(
                    f"{filename}: member {info.filename!r} expands past its declared "
                    f"{info.file_size} bytes"
                )
            pending = inflater.unconsumed_tail
            if not out and not pending:
                break  # truncated stream: the parser reports that itself
    except zlib.error as e:
        raise ValueError(f"{filename}: member {info.filename!r} is corrupt ({e})") from None


# Text-like formats read as plain text. `.html`/`.htm` are reduced to visible text
# (`_extract_html`); `.svg`/`.xml`/`.css` are returned as raw source (R03).
RAW_TEXT_EXTENSIONS = (".html", ".htm", ".svg", ".xml", ".css")
# Every extension `extract_text` can read (a zip member is read through it by extension).
EXTRACTABLE_EXTENSIONS = (
    ".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".tsv", ".md", ".txt", ".json", ".yaml", ".yml",
    *RAW_TEXT_EXTENSIONS,
)
SUPPORTED_TYPES_LABEL = "pdf, docx, xlsx, pptx, csv, tsv, md, txt, json, yaml, html, htm, svg, xml, css, zip (listing)"

# Zip listing and member reads (R06).
MAX_ZIP_MEMBERS_LISTED = 2000
MAX_ZIP_MEMBER_BYTES = 50 * 1024 * 1024


def decode_text(data: bytes) -> str:
    """UTF-8 (a BOM is dropped), else Windows-1252 — what a hand-saved HTML or CSV
    file that is not UTF-8 almost always is."""
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


# Hostile or huge HTML is cut here before it is scanned (the scan is linear, so
# this bounds time and memory); the cut is noted in the document's meta.
MAX_HTML_EXTRACT_BYTES = 5 * 1024 * 1024


def _extract_html(data: bytes) -> tuple[str, dict]:
    from tret.engine.html_scan import visible_text

    meta: dict = {}
    if len(data) > MAX_HTML_EXTRACT_BYTES:
        cut = MAX_HTML_EXTRACT_BYTES
        while cut > 0 and (data[cut] & 0xC0) == 0x80:  # back to a UTF-8 character boundary
            cut -= 1
        meta = {"html_truncated": True, "html_bytes": len(data), "html_bytes_read": cut}
        data = data[:cut]
    return visible_text(decode_text(data)), meta


def zip_member_infos(data: bytes) -> list[dict]:
    """One dict per member of the zip: path, size (uncompressed), kind
    (`file`/`folder`/`zip`/ext), readable (bool) and why not. Refuses a zip
    bomb first (`_refuse_zip_bombs`). Nested zips are listed, never opened.
    At most MAX_ZIP_MEMBERS_LISTED entries (the caller notes the cut)."""
    import zipfile

    _refuse_zip_bombs("archive", data, verify_members=False)
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if len(out) >= MAX_ZIP_MEMBERS_LISTED:
                break
            name = info.filename
            lower = name.lower()
            if info.is_dir():
                continue
            ext = Path(lower).suffix
            reason = None
            if info.flag_bits & 0x1:
                reason = "encrypted"
            elif lower.endswith(".zip"):
                reason = "nested zip: not opened"
            elif ext not in EXTRACTABLE_EXTENSIONS:
                reason = f"unsupported type {ext or '(none)'}"
            elif info.file_size > MAX_ZIP_MEMBER_BYTES:
                reason = f"over the {MAX_ZIP_MEMBER_BYTES // (1024 * 1024)} MB member limit"
            out.append(
                {
                    "path": name,
                    "size": info.file_size,
                    "kind": ext.lstrip(".") or "file",
                    "readable": reason is None,
                    "reason": reason,
                }
            )
    return out


def _zip_total_members(data: bytes) -> int:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return sum(1 for i in zf.infolist() if not i.is_dir())


def _extract_zip_listing(filename: str, data: bytes) -> tuple[str, dict]:
    infos = zip_member_infos(data)
    total = _zip_total_members(data)
    lines = [f"Zip archive {filename}: {total} file(s)."]
    for m in infos:
        state = "readable" if m["readable"] else f"not readable: {m['reason']}"
        lines.append(f"- {m['path']} ({m['size']} bytes, {m['kind']}, {state})")
    if total > len(infos):
        lines.append(f"[Only the first {len(infos)} of {total} members are listed.]")
    lines.append(
        "To read one member, call read_connected_file again with member set to its path exactly "
        "as listed above."
    )
    return "\n".join(lines), {"members": total}


def read_zip_member(archive_name: str, data: bytes, member: str) -> tuple[str, dict]:
    """Text of one member of a zip, read through `extract_text` by its own
    extension. ValueError (message safe to show a model) when the member is
    missing, encrypted, nested, too large or an unsupported type."""
    import zipfile

    _refuse_zip_bombs(archive_name, data, verify_members=False)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        try:
            info = zf.getinfo(member)
        except KeyError:
            raise ValueError(
                f"{archive_name} has no member named {member!r}; use the path exactly as the listing shows it"
            ) from None
        if info.is_dir():
            raise ValueError(f"{member!r} in {archive_name} is a folder, not a file")
        if info.flag_bits & 0x1:
            raise ValueError(f"{member!r} in {archive_name} is encrypted and cannot be read")
        if member.lower().endswith(".zip"):
            raise ValueError(f"{member!r} in {archive_name} is a nested zip; nested archives are listed but not opened")
        if info.file_size > MAX_ZIP_MEMBER_BYTES:
            raise ValueError(
                f"{member!r} in {archive_name} is {info.file_size} bytes, over the "
                f"{MAX_ZIP_MEMBER_BYTES // (1024 * 1024)} MB per-member limit"
            )
        with zf.open(info) as fh:
            body = fh.read(MAX_ZIP_MEMBER_BYTES + 1)
    if len(body) > MAX_ZIP_MEMBER_BYTES:
        raise ValueError(f"{member!r} in {archive_name} expands past the per-member limit")
    return extract_text(member, body)


async def read_zip_member_bounded(
    archive_name: str, data: bytes, member: str, *, budget_seconds: float | None = None
) -> tuple[str, dict]:
    """`read_zip_member` under the same wall-time budget and off-loop thread as
    `extract_bounded`, so a hostile member cannot stall the run that reads it.
    A timeout is a `ValueError` (the message is safe to show a model)."""
    budget = EXTRACTION_TIMEOUT_SECONDS if budget_seconds is None else budget_seconds
    try:
        with pdf_budget(budget):
            return await asyncio.wait_for(
                asyncio.to_thread(read_zip_member, archive_name, data, member), budget
            )
    except (asyncio.TimeoutError, TimeoutError):
        raise ValueError(f"reading it timed out after {budget:.0f}s") from None


def extraction_problem(doc: Document) -> str | None:
    """Why `doc` has no usable text, or None when it has some (R03/R05). The
    reason is for a model: the file, its type, and what went wrong."""
    text = doc.extracted_text
    if text is not None and text.strip():
        return None
    ext = Path(doc.filename or "").suffix.lstrip(".").lower() or "unknown"
    meta = doc.meta or {}
    err = meta.get("error")
    if doc.extraction_status in ("failed", "skipped") or err:
        return f"could not extract text from {doc.filename} (type {ext}): {err or 'extraction failed'}"
    return (
        f"no text could be extracted from {doc.filename} (type {ext}): the file is empty, has no "
        "visible text (for HTML, content that only appears via scripts), or is image-only"
    )


def extract_text(filename: str, data: bytes, *, budget_seconds: float | None = None) -> tuple[str, dict]:
    """Returns (text, meta). Raises ValueError for unsupported types.

    `budget_seconds` bounds the Poppler run for a PDF (default: the `pdf_budget`
    in force, else PDFTOTEXT_TIMEOUT_SECONDS); it is ignored for other formats.
    """
    lower = filename.lower()
    if lower.endswith(".pdf"):
        return _extract_pdf(data, budget_seconds)
    if lower.endswith(".docx"):
        return _extract_docx(filename, data)
    if lower.endswith(".xlsx"):
        return _extract_xlsx(filename, data)
    if lower.endswith(".pptx"):
        return _extract_pptx(filename, data)
    if lower.endswith((".csv", ".tsv")):
        delim = "\t" if lower.endswith(".tsv") else ","
        text = data.decode("utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=delim))
        rendered = "\n".join(" | ".join(row) for row in rows)
        return rendered, {"rows": len(rows), "columns": rows[0] if rows else []}
    if lower.endswith((".md", ".txt", ".json", ".yaml", ".yml")):
        return data.decode("utf-8", errors="replace"), {}
    if lower.endswith((".html", ".htm")):
        return _extract_html(data)
    if lower.endswith((".svg", ".xml", ".css")):
        return decode_text(data), {}
    if lower.endswith(".zip"):
        return _extract_zip_listing(filename, data)
    raise ValueError(f"Unsupported file type: {filename} (supported: {SUPPORTED_TYPES_LABEL})")


def _page_blocks(pages: list[str]) -> str:
    # A `## Page N` marker ahead of every page — the same heading-marker
    # convention `_extract_xlsx`/`_extract_pptx` already use for sheets and
    # slides — is what gives `services/retrieval.py`'s chunker a page
    # locator to attach to prose that otherwise has no heading structure
    # of its own (both readers return flowing text, not a document tree).
    return "\n\n".join(f"## Page {i}\n{text}" for i, text in enumerate(pages, start=1))


class _PdftotextTimeout(Exception):
    """Poppler ran out of its budget. Not a reason to start the slower reader."""


def _pdftotext_pages(data: bytes, timeout: float) -> list[str] | None:
    """Per-page text from `pdftotext -layout`, or None when Poppler is missing or
    unusable (not found, would not start, non-zero exit) and the caller should use
    pypdf. Raises _PdftotextTimeout when it ran past `timeout`.
    """
    exe = shutil.which("pdftotext")
    if exe is None:
        return None
    # Poppler needs nothing from the environment but PATH; credentials in the
    # server's env have no business reaching a PDF parser.
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    try:
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(data)
            tmp.flush()
            run = subprocess.run(
                [exe, "-layout", "-enc", "UTF-8", tmp.name, "-"],
                capture_output=True,
                timeout=timeout,
                env=env,
            )
    except subprocess.TimeoutExpired:
        raise _PdftotextTimeout from None
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("pdftotext could not extract; falling back to pypdf: %s", e)
        return None
    if run.returncode != 0:
        log.warning("pdftotext exited %s; falling back to pypdf", run.returncode)
        return None
    pieces = run.stdout.decode("utf-8", errors="replace").split("\f")
    if pieces and not pieces[-1].strip():
        pieces.pop()
    return [piece.rstrip() for piece in pieces]


def _pdf_page_count(data: bytes) -> int | None:
    """Page count from the PDF's own page tree (a cheap open, no text), or None."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        return len(reader.pages)
    except Exception as e:  # damaged or password-protected: let the pypdf path say so
        log.warning("could not count PDF pages: %s", e)
        return None


def _extract_pdf(data: bytes, budget_seconds: float | None = None) -> tuple[str, dict]:
    bound = budget_seconds if budget_seconds is not None else _pdf_budget.get()
    # A caller's bound may exceed Poppler's default (a large captured report); it
    # is still never longer than the caller waits, nor than the overall ceiling.
    ceiling = max(PDFTOTEXT_TIMEOUT_SECONDS, EXTRACTION_MAX_SECONDS)
    timeout = PDFTOTEXT_TIMEOUT_SECONDS if bound is None else min(ceiling, bound)
    started = _clock()
    try:
        pages = _pdftotext_pages(data, timeout)
    except _PdftotextTimeout:
        # Recorded, not retried: pypdf is the slower reader, and the caller's own
        # bound is about to expire.
        return "", {"text_extractor": "pdftotext", "error": f"pdftotext timed out after {timeout:.0f}s"}
    if bound is not None:
        remaining = bound - (_clock() - started)
        if remaining < max(bound * PYPDF_HEADROOM_FRACTION, PYPDF_HEADROOM_MIN_SECONDS):
            # Poppler used most of the bound; pypdf started now would still be
            # running in this thread after the caller has given up waiting.
            if pages is None:
                return "", {
                    "text_extractor": "pdftotext",
                    "error": "pdftotext failed with too little time left for pypdf",
                    "fallback_skipped": "budget",
                }
            return _page_blocks(pages), {
                "pages": len(pages),
                "text_extractor": "pdftotext",
                "fallback_skipped": "budget",
            }
    if pages is not None:
        # Poppler ends every page with a form feed, so n segments is n pages, unless
        # a stray form feed inside the text has split one. Page numbers are what
        # retrieval cites, so a count that disagrees with the page tree is not used.
        count = _pdf_page_count(data)
        if count == len(pages):
            return _page_blocks(pages), {"pages": len(pages), "text_extractor": "pdftotext"}
        log.warning("pdftotext gave %d page segments for %s pages; using pypdf", len(pages), count)
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    pages = [page.extract_text() or "" for page in reader.pages]
    return _page_blocks(pages), {"pages": len(reader.pages), "text_extractor": "pypdf"}


_HEADING_STYLE_RE = re.compile(r"^Heading (\d+)$")


def _docx_heading_level(style_name: str) -> int:
    """0 for a body-text style, 1-6 for a heading style ("Title" counts as a
    top-level heading same as "Heading 1"; "Heading 7"+ clamps to 6, matching
    the deepest markdown heading `services/retrieval.py`'s chunker recognises).
    """
    if style_name == "Title":
        return 1
    m = _HEADING_STYLE_RE.match(style_name)
    return min(6, int(m.group(1))) if m else 0


def _extract_docx(filename: str, data: bytes) -> tuple[str, dict]:
    """Paragraphs and tables, in document order, with headings and tables
    marked inline (`# heading text` / `## Table N`) rather than dumped as flat
    text and every table appended after every paragraph the way python-docx's
    `.paragraphs`/`.tables` accessors would — document order and heading
    structure are what let `services/retrieval.py`'s chunker build heading
    paths and keep tables whole instead of treating the whole file as one
    undifferentiated block of prose.

    Blocks (one per paragraph, and one per table — heading plus every row) are
    joined with a blank line between them, not a single newline: a docx has no
    blank-line convention of its own (each paragraph is already its own XML
    element), but `services/retrieval.py`'s prose chunker finds paragraph
    breaks by splitting on blank lines, same as it does for a `.md` upload. A
    single-newline join would hand it one run-on paragraph per section and
    default straight to the hard-split path instead of packing paragraphs to
    the ~250-400 token target. A table's own rows stay single-newline-joined
    *within* the table's one block, exactly like every other extractor's
    table/sheet rows — only the blank line between blocks is new.
    """
    import docx
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    # Summed from the central directory before python-docx inflates anything;
    # without it one small upload can expand to gigabytes in the shared
    # process (the extraction timeout abandons the thread but cannot stop it).
    _refuse_zip_bombs(filename, data, MAX_PPTX_UNCOMPRESSED_BYTES)
    document = docx.Document(io.BytesIO(data))
    blocks: list[str] = []
    table_count = 0
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            paragraph = Paragraph(child, document)
            text = paragraph.text
            if not text.strip():
                continue
            level = _docx_heading_level((paragraph.style.name if paragraph.style else "") or "")
            blocks.append(f"{'#' * level} {text}" if level else text)
        elif isinstance(child, CT_Tbl):
            table_count += 1
            table = Table(child, document)
            rows = [" | ".join(cell.text for cell in row.cells) for row in table.rows]
            blocks.append("\n".join([f"## Table {table_count}", *rows]))
    return "\n\n".join(blocks), {"paragraphs": len(document.paragraphs), "tables": table_count}


def _extract_xlsx(filename: str, data: bytes) -> tuple[str, dict]:
    """Row-major text of every worksheet, via `read_only` iteration so a 25MB
    workbook with millions of cells is streamed rather than built into an
    in-memory object model. Formulas are never evaluated: `data_only=True`
    reads each cell's last cached value (what Excel wrote when it last saved
    the file), not a live recomputation.
    """
    import openpyxl

    _refuse_zip_bombs(filename, data)
    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        sheet_count = len(workbook.sheetnames)
        parts: list[str] = []
        chars = 0
        total_rows = 0
        truncated = False
        for sheet in workbook.worksheets:
            if truncated:
                break
            heading = f"## Sheet: {sheet.title}"
            parts.append(heading)
            chars += len(heading) + 1
            # The declared `<dimension>` pads every row, gap rows included, to
            # its width: a sheet claiming A1:XFD1048576 with two real rows
            # would yield a million 16,384-wide empty rows. Without it, a gap
            # row is empty and a row is as wide as its own last cell.
            sheet.reset_dimensions()
            for row in sheet.iter_rows(values_only=True):
                if not row or row.count(None) == len(row):
                    continue
                line = "\t".join("" if value is None else str(value) for value in row)
                parts.append(line)
                chars += len(line) + 1
                total_rows += 1
                if chars >= MAX_EXTRACTED_CHARS:
                    truncated = True
                    break
    finally:
        workbook.close()
    meta: dict = {"sheets": sheet_count, "rows": total_rows}
    if truncated:
        meta["truncated"] = True
    return "\n".join(parts), meta


def _extract_pptx(filename: str, data: bytes) -> tuple[str, dict]:
    """Per-slide text: every shape's text frame (paragraph per line), table
    cells tab-separated, and speaker notes under a `Notes:` heading.
    """
    from pptx import Presentation

    _refuse_zip_bombs(filename, data, MAX_PPTX_UNCOMPRESSED_BYTES)
    presentation = Presentation(io.BytesIO(data))
    parts: list[str] = []
    chars = 0
    truncated = False
    for index, slide in enumerate(presentation.slides, start=1):
        if truncated:
            break
        heading = f"## Slide {index}"
        parts.append(heading)
        chars += len(heading) + 1
        for shape in slide.shapes:
            if truncated:
                break
            if shape.has_text_frame:
                for paragraph in shape.text_frame.paragraphs:
                    if not paragraph.text:
                        continue
                    parts.append(paragraph.text)
                    chars += len(paragraph.text) + 1
                    if chars >= MAX_EXTRACTED_CHARS:
                        truncated = True
                        break
            if truncated:
                break
            if shape.has_table:
                for row in shape.table.rows:
                    line = "\t".join(cell.text for cell in row.cells)
                    parts.append(line)
                    chars += len(line) + 1
                    if chars >= MAX_EXTRACTED_CHARS:
                        truncated = True
                        break
        if truncated:
            break
        if slide.has_notes_slide:
            notes_frame = slide.notes_slide.notes_text_frame
            notes_text = notes_frame.text if notes_frame else ""
            if notes_text.strip():
                parts.append("Notes:")
                parts.append(notes_text)
                chars += len(notes_text) + 8
                if chars >= MAX_EXTRACTED_CHARS:
                    truncated = True
    meta: dict = {"slides": len(presentation.slides)}
    if truncated:
        meta["truncated"] = True
    return "\n".join(parts), meta


async def extract_bounded(
    filename: str, data: bytes, *, budget_seconds: float | None = None
) -> tuple[str, dict, str]:
    """(text, meta, status). Bounded in wall time and in characters returned.

    Extraction is third-party parsing of third-party bytes: it runs off the event
    loop so one slow document cannot stall every other request, it is abandoned
    after `budget_seconds` (default EXTRACTION_TIMEOUT_SECONDS; see
    `extraction_budget_seconds`), its output is truncated, and *any* parser
    failure is recorded on the row rather than raised — a corrupt PDF is a
    document with no text, not a 500 on upload.

    "Abandoned" is precise: Python cannot cancel a running thread, so a pathological
    parser finishes in the background while the request moves on. What is bounded is
    the request, and the memory that thread holds (one document, at most the size cap
    its caller enforced before getting here).
    """
    budget = EXTRACTION_TIMEOUT_SECONDS if budget_seconds is None else budget_seconds
    try:
        with pdf_budget(budget):
            text, meta = await asyncio.wait_for(asyncio.to_thread(extract_text, filename, data), budget)
    except (asyncio.TimeoutError, TimeoutError):
        return "", {"error": f"text extraction timed out after {budget:.0f}s"}, "failed"
    except ValueError as e:  # unsupported type: the message is for the user
        return "", {"error": str(e)}, "failed"
    except Exception as e:  # a parser blowing up on hostile bytes
        log.warning("text extraction failed for %r: %s", filename, e)
        detail = type(e).__name__
        if filename.lower().endswith(".pptx"):
            # python-pptx's own exceptions (a missing package member, a
            # malformed part) carry a first line worth surfacing alongside
            # the class name — the other extractors' exceptions rarely do.
            first_line = next((line for line in str(e).splitlines() if line.strip()), "")
            if first_line:
                detail = f"{detail}: {first_line}"
        return "", {"error": f"text extraction failed: {detail}"}, "failed"
    if len(text) > MAX_EXTRACTED_CHARS:
        meta = {**meta, "truncated": True, "extracted_chars": MAX_EXTRACTED_CHARS}
        text = text[:MAX_EXTRACTED_CHARS]
    # A recorded extractor error with no text (Poppler timed out) is a failure.
    return text, meta, "failed" if meta.get("error") and not text else "done"


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
    storage_key: str | None = None,
    extract: bool = True,
) -> Document:
    """Extract `data`'s text (bounded) and build the `Document` row for it.

    `data` is already written to `storage_path` by the caller — an upload
    streams it there while consuming an untrusted multipart body, an import
    downloads it whole from a provider — so this is purely "bytes in, a row
    ready to add to the session out". Not committed and not added to the
    session: the caller owns the transaction (the upload endpoint commits
    once; the import endpoint commits per item so one bad file in a batch
    cannot roll back the others).

    `storage_key` is the BlobStore key the bytes were stored under (tret/storage);
    `storage_path` stays the local file for readers that need one.
    """
    if extract:
        text, meta, status = await extract_bounded(filename, data)
    else:  # stored as a file; its text lives in a sibling document (e.g. a filing package's .layout.md)
        text, meta, status = "", {"error": "stored as a file; not text-extracted"}, "skipped"
    if meta_extra:
        meta = {**meta, **meta_extra}
    return Document(
        project_id=project_id,
        filename=filename,
        content_type=content_type,
        byte_size=byte_size,
        storage_path=str(storage_path),
        storage_key=storage_key,
        extracted_text=text or None,  # None = nothing was extracted
        extraction_status=status,
        meta=meta,
        sha256=sha256,
        source_kind=source_kind,
        uploaded_by=uploaded_by,
    )
