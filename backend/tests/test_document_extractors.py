"""`.xlsx`/`.pptx`/`.docx`/`.pdf` text extraction in `tret.services.documents`.

Every fixture here is built in-memory with openpyxl/python-pptx/python-docx
(and, for the two-page PDF, WeasyPrint — already a project dependency) — no
binary files live in the repo. The extractor is a third-party parser fed
third-party bytes, so what matters is: the happy path renders sheets/slides/
paragraphs/pages into readable text with the right shape recorded in `meta`
and in document order, a zip crafted to inflate far past what it claims on
disk is refused before either library ever inflates it, and legacy Office
formats (`.xls`/`.ppt`/`.doc`) are still unsupported.
"""
from __future__ import annotations

import io
import time
import zipfile

import pytest

from tret.services import documents as documents_service
from tret.services.documents import (
    MAX_EXTRACTED_CHARS,
    MAX_PPTX_UNCOMPRESSED_BYTES,
    MAX_ZIP_UNCOMPRESSED_BYTES,
    extract_text,
)


# ── fixtures, built in-memory ─────────────────────────────────────────────
def make_xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    sheet1 = wb.active
    sheet1.title = "Sheet1"
    sheet1.append(["name", "value"])
    sheet1.append(["a", 1])
    sheet1.append([None, None])  # fully empty row: must be skipped
    sheet1.append(["b", 2])

    sheet2 = wb.create_sheet("Sheet2")
    sheet2.append(["x", "y"])
    sheet2.append([1, 2])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_large_xlsx_bytes(rows: int, cols: int = 8) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "Big"
    row = [f"value-{i}" for i in range(cols)]
    for _ in range(rows):
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_pptx_bytes() -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    blank = prs.slide_layouts[6]

    slide1 = prs.slides.add_slide(blank)
    box1 = slide1.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box1.text_frame.text = "Hello slide one"
    slide1.notes_slide.notes_text_frame.text = "Speaker notes for slide one"

    slide2 = prs.slides.add_slide(blank)
    box2 = slide2.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box2.text_frame.text = "Hello slide two"
    table_shape = slide2.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(3), Inches(1))
    table = table_shape.table
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    table.cell(1, 0).text = "1"
    table.cell(1, 1).text = "2"

    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def make_docx_bytes() -> bytes:
    import docx

    document = docx.Document()
    document.add_heading("Report Title", level=1)
    document.add_paragraph("An introductory paragraph about the report's purpose and scope.")
    document.add_heading("Findings", level=2)
    document.add_paragraph("The first finding paragraph, describing what was observed.")
    document.add_paragraph("A second finding paragraph, immediately after the first one.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "Metric"
    table.cell(0, 1).text = "Value"
    table.cell(1, 0).text = "Revenue"
    table.cell(1, 1).text = "42"

    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


def make_two_page_pdf_bytes() -> bytes:
    """A real two-page PDF with actual selectable text on each page, built via
    WeasyPrint (already a project dependency — `tret/services/export.py` uses
    it for PDF export) rather than a hand-built PDF byte stream. reportlab is
    not installed in this venv; WeasyPrint fills the same "already available,
    produces real extractable text" role `extract_text` itself needs pypdf to
    read back.
    """
    from weasyprint import HTML

    html = (
        "<html><body>"
        "<p>First page body text about the annual filing.</p>"
        '<div style="page-break-before: always;">'
        "<p>Second page body text about the same filing.</p>"
        "</div></body></html>"
    )
    return HTML(string=html).write_pdf()


def make_zip_bomb_bytes() -> bytes:
    """A member that decompresses to well over the 200MB wall — cheap to
    build because all-zero bytes compress to almost nothing with DEFLATE.
    """
    return make_zip_bomb_bytes_at(MAX_ZIP_UNCOMPRESSED_BYTES + 1024 * 1024)


def make_zip_bomb_bytes_at(uncompressed_size: int) -> bytes:
    """A single-member zip whose declared (central-directory) uncompressed
    size is exactly `uncompressed_size` — cheap to build because all-zero
    bytes compress to almost nothing with DEFLATE."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("bomb.bin", b"\x00" * uncompressed_size)
    return buf.getvalue()


# ── .xlsx ──────────────────────────────────────────────────────────────────
def test_xlsx_renders_sheet_headings_tab_separated_rows_and_skips_empty_rows():
    text, meta = extract_text("book.xlsx", make_xlsx_bytes())

    assert "## Sheet: Sheet1" in text
    assert "## Sheet: Sheet2" in text
    assert "name\tvalue" in text
    assert "a\t1" in text
    assert "b\t2" in text
    assert "x\ty" in text
    assert "1\t2" in text
    assert meta["sheets"] == 2
    # Sheet1: header + "a" row + "b" row (the None/None row is skipped).
    # Sheet2: two non-empty rows.
    assert meta["rows"] == 5
    assert "truncated" not in meta


def test_xlsx_stops_at_the_char_cap_and_records_truncation():
    data = make_large_xlsx_bytes(rows=40_000)

    started = time.monotonic()
    text, meta = extract_text("big.xlsx", data)
    elapsed = time.monotonic() - started

    assert meta["sheets"] == 1
    assert meta["truncated"] is True
    # The char cap must stop iteration well short of all 40,000 rows —
    # otherwise "read_only iteration and the char cap" bought nothing.
    assert meta["rows"] < 40_000
    assert len(text) < MAX_EXTRACTED_CHARS + 1000  # a little slack for the last line
    assert elapsed < 15, f"extraction took {elapsed:.1f}s — the early stop did not fire"


# ── .pptx ──────────────────────────────────────────────────────────────────
def test_pptx_renders_slide_headings_shape_text_tables_and_notes():
    text, meta = extract_text("deck.pptx", make_pptx_bytes())

    assert "## Slide 1" in text
    assert "## Slide 2" in text
    assert "Hello slide one" in text
    assert "Hello slide two" in text
    assert "Notes:" in text
    assert "Speaker notes for slide one" in text
    assert "A\tB" in text
    assert "1\t2" in text
    assert meta["slides"] == 2
    assert "truncated" not in meta

    # Slide 1 has no notes text and the Notes: heading for it must not appear
    # right after "Hello slide one" with nothing behind it — slide order is
    # preserved (slide 1's block before slide 2's).
    assert text.index("## Slide 1") < text.index("Hello slide one") < text.index(
        "Notes:"
    ) < text.index("## Slide 2")


# ── .docx ──────────────────────────────────────────────────────────────────
def test_docx_renders_headings_paragraphs_and_a_table_in_document_order():
    text, meta = extract_text("report.docx", make_docx_bytes())

    assert "# Report Title" in text
    assert "## Findings" in text
    assert "## Table 1" in text
    assert "An introductory paragraph about the report's purpose and scope." in text
    assert "Metric | Value" in text
    assert "Revenue | 42" in text
    # meta counts: python-docx's own `.paragraphs`/table-count, unaffected by
    # the join-character change below.
    assert meta["paragraphs"] == 5
    assert meta["tables"] == 1

    # Document order: title, then intro paragraph, then the Findings heading,
    # then its two paragraphs, then the table — never tables-after-paragraphs
    # the way python-docx's flat `.paragraphs`/`.tables` accessors would give.
    for earlier, later in [
        ("# Report Title", "An introductory paragraph"),
        ("An introductory paragraph", "## Findings"),
        ("## Findings", "The first finding paragraph"),
        ("The first finding paragraph", "A second finding paragraph"),
        ("A second finding paragraph", "## Table 1"),
    ]:
        assert text.index(earlier) < text.index(later), f"{earlier!r} should precede {later!r}"


def test_docx_paragraphs_are_blank_line_separated_so_the_chunker_can_pack_them():
    # A docx has no blank-line convention of its own — every paragraph is
    # already its own XML element — so `_extract_docx` must insert one
    # explicitly, or `services/retrieval.py`'s prose chunker (which finds
    # paragraph breaks by splitting on blank lines) sees one giant run-on
    # paragraph per section instead of two separate ones.
    text, _ = extract_text("report.docx", make_docx_bytes())

    first = "The first finding paragraph, describing what was observed."
    second = "A second finding paragraph, immediately after the first one."
    between = text[text.index(first) + len(first) : text.index(second)]
    assert "\n\n" in between, f"expected a blank line between paragraphs, got {between!r}"

    # The fix must not touch table rows: they stay single-newline-joined
    # within the table's own block, same as every other extractor's tables.
    table_block = text[text.index("## Table 1") :]
    assert "Metric | Value\nRevenue | 42" in table_block


# ── .pdf ───────────────────────────────────────────────────────────────────
def test_pdf_pages_get_page_markers_in_document_order():
    text, meta = extract_text("filing.pdf", make_two_page_pdf_bytes())

    assert "## Page 1" in text
    assert "## Page 2" in text
    assert "First page body text about the annual filing." in text
    assert "Second page body text about the same filing." in text
    assert text.index("## Page 1") < text.index("First page body text")
    assert text.index("First page body text") < text.index("## Page 2")
    assert text.index("## Page 2") < text.index("Second page body text")
    assert meta["pages"] == 2


# ── zip-bomb guard ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("filename", ["bomb.xlsx", "bomb.pptx"])
def test_a_zip_bomb_is_refused_before_either_library_parses_it(filename):
    data = make_zip_bomb_bytes()
    with pytest.raises(ValueError, match="archive expands to"):
        extract_text(filename, data)


async def test_the_zip_bomb_refusal_reaches_the_ordinary_failure_path():
    data = make_zip_bomb_bytes()
    text, meta, status = await documents_service.extract_bounded("bomb.xlsx", data)
    assert status == "failed"
    assert text == ""
    assert "archive expands to" in meta["error"]


def test_pptx_zip_guard_uses_a_lower_ceiling_than_xlsx():
    """`_extract_pptx` cannot stream the way `_extract_xlsx` does —
    `python-pptx` parses the whole package eagerly — so it must be refused
    at a lower uncompressed-size ceiling than xlsx's 200MB, not trust the
    same wall a streaming parser can afford."""
    # Between the two ceilings: over pptx's 50MB, comfortably under xlsx's
    # 200MB default.
    data = make_zip_bomb_bytes_at(MAX_PPTX_UNCOMPRESSED_BYTES + 1024 * 1024)

    with pytest.raises(ValueError, match="archive expands to"):
        extract_text("deck.pptx", data)

    # The exact same bytes, read as if they were an xlsx's zip container,
    # pass the guard (still well under the 200MB xlsx ceiling) — proving
    # the pptx refusal above came from a lower, format-specific cap, not
    # from the shared default.
    documents_service._refuse_zip_bombs("book.xlsx", data)


# ── unsupported types ────────────────────────────────────────────────────────
def test_unsupported_type_message_now_lists_xlsx_and_pptx():
    with pytest.raises(ValueError) as exc:
        extract_text("photo.jpeg", b"not an office file")
    assert "xlsx" in str(exc.value)
    assert "pptx" in str(exc.value)


@pytest.mark.parametrize("filename", ["report.xls", "deck.ppt", "letter.doc"])
def test_legacy_office_formats_stay_unsupported(filename):
    with pytest.raises(ValueError, match="Unsupported file type"):
        extract_text(filename, b"whatever bytes")
