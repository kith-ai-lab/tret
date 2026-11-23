"""The Stage 1 normaliser (`tret/services/normalise.py`): pass-1 text of every page, the
table-page rule, the docling chunks (mocked: docling is never needed here), the child
process guards, quarantine, ESEF tables, idempotency per version, the routes and the CLI.

Real Poppler is used where it is installed (the tests that need it skip otherwise); the
PDFs are built with WeasyPrint like `tests/test_document_extractors.py`.
"""
# ruff: noqa: F811 - the imported fixture is used by name as a test argument
from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.test_collections_api import world  # noqa: F401 - fixture
from tests.test_esef_filing import XHTML_HEAD, make_esef_zip
from tret.api import disclosure_reports as reports_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, DisclosureReport, Document, Run, Workspace
from tret.services import normalise as nz
from tret.storage import get_store, normalised_key, reset_store

needs_poppler = pytest.mark.skipif(shutil.which("pdftotext") is None, reason="pdftotext (Poppler) not installed")


# ── fixtures and builders ────────────────────────────────────────────────────
@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """The process settings with the normaliser on, docling off, storage in tmp."""
    s = get_settings()
    monkeypatch.setattr(s, "storage_dir", str(tmp_path / "storage"))
    monkeypatch.setattr(s, "disclosure_normaliser", "on")
    monkeypatch.setattr(s, "normaliser_docling", "off")
    reset_store()
    yield s
    reset_store()


def pdf_from_html(body: str) -> bytes:
    from weasyprint import HTML

    return HTML(string=f"<html><body>{body}</body></html>").write_pdf()


PROSE = "Our approach to sustainability and climate risk is described in this section of the report. " * 4


def prose_pdf(pages: int) -> bytes:
    body = "".join(
        f'<div style="page-break-before: always;"><p>Page body {i}. {PROSE}</p></div>' for i in range(1, pages + 1)
    )
    return pdf_from_html(body)


def table_html(rows: int = 12) -> str:
    cells = "".join(
        f"<tr><td>Scope {i} emissions</td><td>{1000 + i * 17}</td><td>{900 + i * 13}</td><td>{800 + i * 11}</td></tr>"
        for i in range(1, rows + 1)
    )
    return f'<table style="width:100%"><tr><td>Metric</td><td>2025</td><td>2024</td><td>2023</td></tr>{cells}</table>'


def table_pdf() -> bytes:
    """Page 1 prose, page 2 a table, page 3 prose."""
    return pdf_from_html(
        f"<p>{PROSE}</p>"
        f'<div style="page-break-before: always;">{table_html()}</div>'
        f'<div style="page-break-before: always;"><p>{PROSE}</p></div>'
    )


def numeric_layout_page() -> str:
    lines = ["Greenhouse gas emissions (tCO2e)    2025      2024      2023"]
    lines += [f"Scope {i} emissions             {1000 + i}     {900 + i}     {800 + i}" for i in range(1, 11)]
    return "\n".join(lines)


# ── the table-page rule ──────────────────────────────────────────────────────
def test_a_page_of_aligned_numeric_rows_is_a_table_page():
    page = numeric_layout_page()
    numeric, rows = nz.table_page_stats(page)
    assert numeric >= nz.TABLE_MIN_NUMERIC_TOKENS and rows >= nz.TABLE_MIN_ROW_LINES
    assert nz.is_table_page(page)


def test_prose_with_numbers_in_it_is_not_a_table_page():
    prose = "In 2025 we cut emissions by 12% across 40 sites, 3 of them in 2 countries.\n" * 30
    assert nz.table_page_stats(prose)[1] == 0
    assert not nz.is_table_page(prose)


def test_a_few_numeric_rows_are_not_enough_without_emissions_words():
    page = "Revenue    100    200\nCosts    50    60\n" + "x\n" * 10
    assert not nz.is_table_page(page)
    # numeric tokens below the floor, whatever the shape
    assert not nz.is_table_page("a    1    2\nb    3    4\nc    5    6\nd    7    8")


def test_an_emissions_page_needs_only_two_row_lines():
    page = (
        "Our greenhouse gas emissions are shown below. Scope 1 and scope 2 are reported in tCO2e.\n"
        + "Total emissions    8,186.0    7,500.5    7,010.2\nScope 1    1,200    1,100    1,000\n"
        + "Other figures: 2025 2024 2023 2022 2021 2020 2019 2018 2017 2016 2015 2014 2013 2012 2011\n"
    )
    assert nz.table_page_stats(page)[1] == 2 and nz.is_table_page(page)
    assert not nz.is_table_page(page.replace("emissions", "items").replace("Scope", "Part").replace("tCO2e", "t"))


def test_over_the_cap_emissions_pages_go_first_and_the_cut_is_reported():
    plain = numeric_layout_page().replace("emissions", "items").replace("Scope", "Part").replace("tCO2e", "t")
    plain = plain.replace("Greenhouse gas", "Stock")
    pages = [plain, plain, numeric_layout_page(), plain]
    chosen, candidates, capped = nz.select_table_pages(pages, 2)
    assert candidates == 4 and capped
    assert 3 in chosen and len(chosen) == 2 and chosen == sorted(chosen)
    assert nz.select_table_pages(pages, 10) == ([1, 2, 3, 4], 4, False)


@needs_poppler
def test_a_real_pdf_table_page_is_found_in_pdftotext_text(tmp_path):
    path = tmp_path / "t.pdf"
    path.write_bytes(table_pdf())
    pages, extractor = nz.read_pages(path)
    assert extractor == "pdftotext" and len(pages) == 3
    assert nz.select_table_pages(pages, 150)[0] == [2]


# ── pass 1: every page, no cap, physical page markers ────────────────────────
@needs_poppler
def test_every_page_is_marked_in_order_and_nothing_is_dropped(tmp_path, cfg):
    path = tmp_path / "long.pdf"
    path.write_bytes(prose_pdf(40))
    result = nz.normalise_pdf_file(path, cfg)
    assert result.status == nz.STATUS_NORMALISED and result.source_kind == "pdf"
    markers = [ln for ln in result.markdown.splitlines() if ln.startswith("## Page ")]
    assert markers == [f"## Page {i}" for i in range(1, 41)]
    for i in (1, 17, 40):
        assert f"Page body {i}." in result.markdown
    m = result.metrics
    assert m["pages"] == 40 and m["pages_with_text"] == 40 and m["empty_pages"] == 0
    assert m["text_extractor"] == "pdftotext" and m["docling"] == "off" and m["capped"] is False
    assert m["seconds_total"] >= 0 and "pdftotext_version" in m


def test_the_text_is_not_cut_at_the_extracted_text_cap(tmp_path, cfg, monkeypatch):
    """`Document.extracted_text` stops at 2,000,000 characters; the normaliser must not."""
    from tret.services.documents import MAX_EXTRACTED_CHARS

    big = ("A long paragraph of report text about climate targets. " * 600).strip()  # ~31 kB
    pages = [f"{big}\nend of page {i}" for i in range(1, 91)]  # ~2.8 M characters
    assert sum(len(p) for p in pages) > MAX_EXTRACTED_CHARS
    monkeypatch.setattr(nz, "read_pages", lambda path, settings=None: (pages, "pdftotext"))
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    assert len(result.markdown) > MAX_EXTRACTED_CHARS
    assert "end of page 90" in result.markdown and result.markdown.count("## Page ") == 90


def test_prose_pages_lose_layout_whitespace_and_hash_lines_cannot_forge_a_marker():
    pages = ["      Indented    heading\nsome    text here\n## Page 99\n# Title"]
    md = nz.assemble_pdf_markdown(pages, {}, set())
    assert "Indented heading" in md and "some text here" in md
    assert md.splitlines().count("## Page 1") == 1 and "\\## Page 99" in md and "\\# Title" in md
    assert "## Page 99" not in [ln for ln in md.splitlines()]


def test_a_table_page_that_was_not_converted_keeps_its_layout_and_says_so():
    page = numeric_layout_page()
    md = nz.assemble_pdf_markdown([page], {}, {1})
    assert nz.UNSTRUCTURED_NOTE in md
    assert "Scope 3 emissions             1003     903     803" in md  # alignment kept


# ── pass 2: docling chunks (mocked) ──────────────────────────────────────────
def three_table_pages(monkeypatch, *, pages=8, table=(2, 3, 5, 6, 8)):
    texts = [
        numeric_layout_page() if i in table else f"Prose page {i}. {PROSE}" for i in range(1, pages + 1)
    ]
    monkeypatch.setattr(nz, "read_pages", lambda path, settings=None: (texts, "pdftotext"))
    return texts


def docling_on(monkeypatch, cfg, chunk_pages=2):
    monkeypatch.setattr(cfg, "normaliser_docling", "auto")
    monkeypatch.setattr(cfg, "normaliser_chunk_pages", chunk_pages)
    monkeypatch.setattr(nz, "docling_version", lambda: "2.99.0")


def md_table_for(page: int) -> str:
    """What docling would return for a `numeric_layout_page()`: every number kept, plus a
    row naming the page so a test can tell the pages apart."""
    rows = "\n".join(f"| Scope {i} | {1000 + i} | {900 + i} | {800 + i} |" for i in range(1, 11))
    return (
        "## Emissions\n\n| Metric | 2025 | 2024 | 2023 |\n| --- | --- | --- | --- |\n"
        f"{rows}\n| Scope 1 | {1000 + page} |\n<!-- image -->"
    )


def test_table_pages_go_to_docling_in_chunks_and_land_on_their_physical_pages(tmp_path, cfg, monkeypatch):
    three_table_pages(monkeypatch)
    docling_on(monkeypatch, cfg, chunk_pages=2)
    calls: list[list[int]] = []

    def fake(path, pages, settings):
        calls.append(list(pages))
        return nz.ChunkResult(
            pages={p: md_table_for(p) for p in pages}, ok=True, seconds=1.5, peak_rss_mb=1700.4,
            docling_version="2.99.0",
        )

    monkeypatch.setattr(nz, "convert_chunk", fake)
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    assert calls == [[2, 3], [5, 6], [8]]
    md = result.markdown
    assert "| Scope 1 | 1005 |" in md and "<!-- image -->" not in md
    assert md.index("## Page 5\n") < md.index("| Scope 1 | 1005 |") < md.index("## Page 6\n")
    # docling's `## Emissions` is pushed below the page marker's level
    assert "#### Emissions" in md and md.count("## Page ") == 8
    m = result.metrics
    assert (m["table_pages_selected"], m["table_pages_converted"], m["table_pages_fallback"]) == (5, 5, 0)
    assert m["tables_found"] == 5 and m["docling"] == "used" and m["docling_version"] == "2.99.0"
    assert m["peak_rss_mb"] == 1700 and m["seconds_docling"] == 4.5 and len(m["chunks"]) == 3
    assert m["table_numeric_recall_min"] is not None


@pytest.mark.parametrize("error", ["killed: timed out after 300s", "killed: memory 3600 MB over the 3200 MB limit"])
def test_a_failed_chunk_falls_back_to_pdftotext_for_its_pages_only(tmp_path, cfg, monkeypatch, error):
    texts = three_table_pages(monkeypatch)
    docling_on(monkeypatch, cfg, chunk_pages=2)

    def fake(path, pages, settings):
        if pages == [5, 6]:
            return nz.ChunkResult(error=error, seconds=300.0, peak_rss_mb=3600.0)
        return nz.ChunkResult(pages={p: md_table_for(p) for p in pages}, ok=True)

    monkeypatch.setattr(nz, "convert_chunk", fake)
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    m = result.metrics
    assert (m["table_pages_converted"], m["table_pages_fallback"]) == (3, 2)
    assert [c["ok"] for c in m["chunks"]] == [True, False, True]
    assert m["chunks"][1]["error"] == error and m["peak_rss_mb"] == 3600
    md = result.markdown
    assert md.count(nz.UNSTRUCTURED_NOTE) == 2
    assert texts[4].splitlines()[1] in md  # page 5's pass-1 text is there
    assert "| Scope 1 | 1005 |" not in md and "| Scope 1 | 1002 |" in md


def test_a_page_docling_returns_nothing_for_keeps_its_pdftotext_text(tmp_path, cfg, monkeypatch):
    three_table_pages(monkeypatch, table=(2, 3))
    docling_on(monkeypatch, cfg, chunk_pages=5)
    monkeypatch.setattr(nz, "convert_chunk", lambda path, pages, s: nz.ChunkResult(pages={2: md_table_for(2), 3: "  "}, ok=True))
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    assert (result.metrics["table_pages_converted"], result.metrics["table_pages_fallback"]) == (1, 1)


def test_docling_missing_or_off_means_unstructured_tables_not_an_error(tmp_path, cfg, monkeypatch):
    three_table_pages(monkeypatch)
    monkeypatch.setattr(cfg, "normaliser_docling", "auto")
    monkeypatch.setattr(nz, "docling_version", lambda: None)
    boom = lambda *a, **k: pytest.fail("docling must not be started")  # noqa: E731
    monkeypatch.setattr(nz, "convert_chunk", boom)
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    assert result.metrics["docling"] == "unavailable" and result.metrics["table_pages_fallback"] == 5
    assert result.markdown.count(nz.UNSTRUCTURED_NOTE) == 5 and result.status == nz.STATUS_NORMALISED
    monkeypatch.setattr(nz, "docling_version", lambda: "2.99.0")
    monkeypatch.setattr(cfg, "normaliser_docling", "off")
    assert nz.normalise_pdf_file(tmp_path / "x.pdf", cfg).metrics["docling"] == "off"


def test_the_table_page_cap_is_applied_and_recorded(tmp_path, cfg, monkeypatch):
    three_table_pages(monkeypatch)
    monkeypatch.setattr(cfg, "normaliser_max_table_pages", 2)
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    m = result.metrics
    assert m["capped"] is True and m["table_pages_candidates"] == 5 and m["table_pages_selected"] == 2


# ── the child process guards ─────────────────────────────────────────────────
def stub_worker(monkeypatch, code: str):
    monkeypatch.setattr(nz, "_worker_command", lambda mode_args, out: [sys.executable, "-c", code, out])


def test_a_child_that_writes_its_result_is_read_back(monkeypatch, cfg):
    stub_worker(monkeypatch, "import json,sys; json.dump({'pages': {'7': '| a |'}, 'docling_version': '9'}, open(sys.argv[1], 'w'))")
    res = nz.convert_chunk(Path("x.pdf"), [7], cfg)
    assert res.ok and res.pages == {7: "| a |"} and res.docling_version == "9" and res.error is None


def test_a_child_over_the_wall_clock_is_killed(monkeypatch, cfg):
    stub_worker(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setattr(cfg, "normaliser_chunk_timeout_seconds", 1)
    res = nz.convert_chunk(Path("x.pdf"), [1], cfg)
    assert not res.ok and "timed out" in res.error and res.seconds < 20


def test_a_child_over_the_rss_guard_is_killed(monkeypatch, cfg):
    stub_worker(monkeypatch, "import time; time.sleep(60)")
    monkeypatch.setattr(nz, "rss_mb", lambda pid: 4200.0)
    res = nz.convert_chunk(Path("x.pdf"), [1], cfg)
    assert not res.ok and "memory 4200 MB over the 3200 MB limit" in res.error and res.peak_rss_mb == 4200.0
    assert res.seconds < 20


def test_a_child_that_crashes_reports_its_stderr(monkeypatch, cfg):
    stub_worker(monkeypatch, "import sys; sys.stderr.write('docling exploded'); sys.exit(3)")
    res = nz.convert_chunk(Path("x.pdf"), [1], cfg)
    assert not res.ok and "exited 3" in res.error and "docling exploded" in res.error


def test_the_child_environment_carries_no_secrets(monkeypatch):
    monkeypatch.setenv("TRET_SECRET_KEY", "hunter2")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-x")
    env = nz._child_env()
    assert "TRET_SECRET_KEY" not in env and "ANTHROPIC_API_KEY" not in env and env["OMP_NUM_THREADS"] == "2"


def test_pages_are_grouped_into_chunks():
    assert nz.chunk_pages([1, 2, 3, 4, 5, 6, 7], 3) == [[1, 2, 3], [4, 5, 6], [7]]
    assert nz.chunk_pages([], 5) == [] and nz.chunk_pages([1, 2], 0) == [[1], [2]]


# ── quarantine ───────────────────────────────────────────────────────────────
def metrics_of(pages: list[str]) -> dict:
    return nz.text_metrics(pages)


def test_quarantine_rules_in_order():
    ok = ["x" * 500] * 4
    assert nz.quarantine_reason(metrics_of(ok), "pdf") is None
    assert nz.quarantine_reason(metrics_of([]), "pdf") == nz.QUARANTINE_EMPTY
    assert nz.quarantine_reason(metrics_of(["", "  "]), "pdf") == nz.QUARANTINE_EMPTY
    assert nz.quarantine_reason(metrics_of(["x" * 150] * 3 + ["x" * 5000]), "pdf") == nz.QUARANTINE_NO_TEXT_LAYER
    garbled = ["a" * 450 + "�" * 50] * 3
    assert nz.quarantine_reason(metrics_of(garbled), "pdf") == nz.QUARANTINE_GARBLED
    assert nz.text_metrics(garbled)["fffd_rate"] == 0.1
    # exactly 1% is not over the limit
    edge = ["a" * 495 + "�" * 5] * 3
    assert nz.quarantine_reason(metrics_of(edge), "pdf") is None
    # the median rule does not apply to an ESEF filing read as sections
    assert nz.quarantine_reason(metrics_of(["x" * 50] * 3), "esef_reflow") is None
    assert nz.quarantine_reason(metrics_of(["x" * 50] * 3), "esef_paged") == nz.QUARANTINE_NO_TEXT_LAYER


def test_a_report_with_no_text_layer_is_quarantined_but_its_markdown_is_kept(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(nz, "read_pages", lambda path, settings=None: (["Cover", "", "Contents", ""], "pdftotext"))
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    assert result.status == nz.STATUS_QUARANTINED and result.quarantine_reason == nz.QUARANTINE_NO_TEXT_LAYER
    assert "## Page 4" in result.markdown and "Contents" in result.markdown
    assert result.metrics["empty_pages"] == 2 and result.metrics["pages_with_text"] == 2


@needs_poppler
def test_a_pdf_with_blank_pages_only_is_empty(tmp_path, cfg):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    path = tmp_path / "blank.pdf"
    with open(path, "wb") as fh:
        writer.write(fh)
    result = nz.normalise_pdf_file(path, cfg)
    assert result.status == nz.STATUS_QUARANTINED and result.quarantine_reason == nz.QUARANTINE_EMPTY
    assert result.metrics["pages"] == 3 and result.markdown.count("## Page ") == 3


def test_replacement_characters_quarantine_a_report_as_garbled(tmp_path, cfg, monkeypatch):
    monkeypatch.setattr(nz, "read_pages", lambda path, settings=None: (["�" * 80 + "a" * 400] * 3, "pdftotext"))
    assert nz.normalise_pdf_file(tmp_path / "x.pdf", cfg).quarantine_reason == nz.QUARANTINE_GARBLED


def test_an_unreadable_pdf_is_a_failure_not_a_crash(tmp_path, cfg):
    path = tmp_path / "bad.pdf"
    path.write_bytes(b"this is not a pdf")
    result = nz.normalise_pdf_file(path, cfg)
    assert result.status == nz.STATUS_FAILED and result.error and result.markdown == ""


# ── ESEF ─────────────────────────────────────────────────────────────────────
def esef_report(pages: int = 6) -> bytes:
    body = "".join(
        f'<div class="page"><p>Page {i} narrative. {PROSE}</p>'
        f"<table><tr><td>Metric</td><td>2025</td><td>2024</td></tr>"
        f"<tr><td>Scope 1 | direct</td><td>{i * 100}</td><td>{i * 90}</td></tr>"
        f"<tr><td>Scope 2</td><td>{i * 10}</td><td>{i * 9}</td></tr></table>"
        f"<p>Closing line {i}.</p><p>{i}</p></div>"
        for i in range(1, pages + 1)
    )
    return (XHTML_HEAD + body + "</body></html>").encode()


def sections_report() -> bytes:
    body = "".join(
        f"<h2>Chapter {i}</h2><p>Text of chapter {i}.</p><table><tr><td>Item</td><td>Value</td></tr>"
        f"<tr><td>A</td><td>{i}</td></tr></table>"
        for i in range(1, 6)
    )
    return (XHTML_HEAD + body + "</body></html>").encode()


def test_esef_rows_become_markdown_tables_between_text(tmp_path, cfg):
    path = tmp_path / "filing.zip"
    path.write_bytes(make_esef_zip(esef_report()))
    result = nz.normalise_esef_file(path, cfg)
    assert result.source_kind == "esef_paged" and result.status == nz.STATUS_NORMALISED, result.error
    md = result.markdown
    assert [ln for ln in md.splitlines() if ln.startswith("## ")] == [f"## Page {i}" for i in range(1, 7)]
    assert "| Metric | 2025 | 2024 |\n| --- | --- | --- |\n| Scope 1 \\| direct | 100 | 90 |\n| Scope 2 | 10 | 9 |" in md
    assert md.index("Page 2 narrative") < md.index("| Scope 1 \\| direct | 200 | 180 |") < md.index("Closing line 2.")
    m = result.metrics
    assert m["pages"] == 6 and m["tables_found"] == 6 and m["layout"] == "paged" and m["capped"] is False


def test_an_esef_filing_read_as_sections_is_esef_reflow_with_section_markers(tmp_path, cfg):
    path = tmp_path / "filing.zip"
    path.write_bytes(make_esef_zip(sections_report()))
    result = nz.normalise_esef_file(path, cfg)
    assert result.source_kind == "esef_reflow" and result.status == nz.STATUS_NORMALISED, result.error
    assert "## Section 1: Chapter 1" in result.markdown and "| Item | Value |" in result.markdown


def test_a_hostile_esef_archive_is_a_failure(tmp_path, cfg):
    path = tmp_path / "bad.zip"
    path.write_bytes(b"PK not really a zip")
    result = nz.normalise_esef_file(path, cfg)
    assert result.status == nz.STATUS_FAILED and result.error


def test_markdown_table_pads_ragged_rows_and_flattens_newlines():
    assert nz.markdown_table([("a", "b"), ("c",), ("d\ne", "f|g", "h")]) == [
        "| a | b |  |",
        "| --- | --- | --- |",
        "| c |  |  |",
        "| d e | f\\|g | h |",
    ]


# ── the database side ────────────────────────────────────────────────────────
def run_db(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


def add_pdf_document(
    world, cfg, name: str, data: bytes, *, run_id=None, source_kind="web", provenance=None,
    content_type="application/pdf",
):
    sha = hashlib.sha256(data).hexdigest()
    path = Path(cfg.storage_dir) / f"{sha}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    meta = {"fetched_by_run": str(run_id) if run_id else None}
    if provenance:
        meta["provenance"] = provenance
    doc = Document(
        id=uuid.uuid4(), project_id=world.project.id, filename=name, content_type=content_type,
        byte_size=len(data), storage_path=str(path), storage_key=f"{sha}-{name}", sha256=sha,
        extracted_text="x", extraction_status="done", meta=meta, source_kind=source_kind,
    )

    async def go(db):
        db.add(doc)
        await db.commit()

    run_db(world, go)
    return doc


def fake_converter(monkeypatch, *, chars_by_name: dict[str, int] | None = None):
    """Replace the PDF converter with a counter; the markdown size is chosen by file name."""
    calls: list[str] = []

    def fake(path, settings):
        calls.append(path.name.split("-", 1)[-1])  # the stored name is `<sha256>-<filename>`
        size = next((n for key, n in (chars_by_name or {}).items() if key in path.name), 1000)
        return nz.NormaliseResult(
            source_kind="pdf", status=nz.STATUS_NORMALISED, markdown="## Page 1\n" + "x" * size,
            metrics={"pages": 1, "tables_found": 0, "seconds_total": 0.1},
        )

    monkeypatch.setattr(nz, "normalise_pdf_file", fake)
    return calls


def test_normalising_twice_at_one_version_does_nothing_the_second_time(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    calls = fake_converter(monkeypatch)

    async def twice(db):
        first = await nz.normalise_document(db, doc)
        await db.commit()
        second = await nz.normalise_document(db, doc)
        await db.commit()
        return first.id, second.id

    first, second = run_db(world, twice)
    assert first == second and len(calls) == 1

    async def rows(db):
        from sqlalchemy import select

        return (await db.execute(select(DisclosureReport))).scalars().all()

    (report,) = run_db(world, rows)
    key = normalised_key(doc.id, nz.NORMALISER_VERSION)
    assert report.markdown_key == key and report.status == "normalised" and report.source_kind == "pdf"
    stored = asyncio.run(get_store(cfg).get(key))
    assert stored.decode() == "## Page 1\n" + "x" * 1000
    assert report.markdown_sha256 == hashlib.sha256(stored).hexdigest() and report.markdown_chars == len(stored)
    assert report.project_id == world.project.id and report.document_id == doc.id


def test_a_new_normaliser_version_makes_a_second_row_and_force_redoes_one(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    calls = fake_converter(monkeypatch)

    async def go(db):
        a = await nz.normalise_document(db, doc)
        monkeypatch.setattr(nz, "NORMALISER_VERSION", "2")
        b = await nz.normalise_document(db, doc)
        again = await nz.normalise_document(db, doc, force=True)
        await db.commit()
        return a.id, b.id, again.id, a.markdown_key, b.markdown_key

    a, b, again, key_a, key_b = run_db(world, go)
    assert a != b and b == again and len(calls) == 3
    assert key_a.endswith("/1.md") and key_b.endswith("/2.md")


def test_a_failed_report_is_retried_not_skipped(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    outcomes = iter([nz.NormaliseResult("pdf", nz.STATUS_FAILED, "", {}, error="pdftotext timed out"),
                     nz.NormaliseResult("pdf", nz.STATUS_NORMALISED, "## Page 1\nok\n", {"pages": 1})])
    monkeypatch.setattr(nz, "normalise_pdf_file", lambda path, s: next(outcomes))

    async def go(db):
        first = await nz.normalise_document(db, doc)
        snapshot = (first.status, first.error, first.markdown_key)
        second = await nz.normalise_document(db, doc)
        return snapshot, (second.id == first.id, second.status, second.error, second.markdown_key is not None)

    snapshot, after = run_db(world, go)
    assert snapshot == ("failed", "pdftotext timed out", None)
    assert after == (True, "normalised", None, True)


def test_the_service_refuses_to_run_while_the_setting_is_off(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    monkeypatch.setattr(cfg, "disclosure_normaliser", "off")
    with pytest.raises(nz.NormaliserDisabledError):
        run_db(world, lambda db: nz.normalise_document(db, doc))


def test_files_the_normaliser_does_not_read_are_skipped(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "notes.txt", b"hello", content_type="text/plain")
    calls = fake_converter(monkeypatch)
    assert run_db(world, lambda db: nz.normalise_document(db, doc)) is None and calls == []


def seed_target(world, cfg, monkeypatch):
    """A batch with one target whose run fetched three PDFs (one big, one small, one of
    another run) and a package derived from them."""
    run_id, other_run = uuid.uuid4(), uuid.uuid4()

    async def setup(db):
        for rid in (run_id, other_run):
            db.add(Run(id=rid, project_id=world.project.id, harness_id=world.harness.id,
                       task_type="freeform", task_input={}, document_ids=[], status="completed"))
        await db.flush()
        batch = Batch(id=uuid.uuid4(), workspace_id=world.ws.id, project_id=world.project.id, name="B",
                      run_slug="b-1", status="review", profile="disclosure")
        db.add(batch)
        await db.flush()
        target = BatchTarget(id=uuid.uuid4(), collection_id=batch.id, position=1, slug="acme", company="Acme",
                             market="EU", label="Acme", status="review", run_id=run_id)
        db.add(target)
        await db.commit()
        return batch.id, target.id

    batch_id, target_id = run_db(world, setup)
    big = add_pdf_document(world, cfg, "big.pdf", b"%PDF big", run_id=run_id)
    small = add_pdf_document(world, cfg, "small.pdf", b"%PDF small", run_id=run_id)
    add_pdf_document(world, cfg, "elsewhere.pdf", b"%PDF other", run_id=other_run)
    add_pdf_document(world, cfg, "pkg.pdf", b"%PDF pkg", run_id=run_id, source_kind="package")
    calls = fake_converter(monkeypatch, chars_by_name={"big": 5000, "small": 100})
    return SimpleNamespace(batch_id=batch_id, target_id=target_id, big=big, small=small, calls=calls, run_id=run_id)


def test_normalise_target_reads_only_its_runs_web_pdfs_and_marks_the_biggest_primary(world, cfg, monkeypatch):
    n = seed_target(world, cfg, monkeypatch)

    async def go(db):
        target = await db.get(BatchTarget, n.target_id)
        reports = await nz.normalise_target(db, target)
        await db.commit()
        return {r.document_id: (r.is_primary, r.batch_target_id, r.run_id) for r in reports}

    got = run_db(world, go)
    assert sorted(n.calls) == ["big.pdf", "small.pdf"]
    assert got == {n.big.id: (True, n.target_id, n.run_id), n.small.id: (False, n.target_id, n.run_id)}


def test_normalise_batch_filters_by_status_slug_and_limit(world, cfg, monkeypatch):
    n = seed_target(world, cfg, monkeypatch)
    seen = []

    def go(**kw):
        return asyncio.run(nz.normalise_batch(world.factory, n.batch_id, progress=seen.append, **kw))

    assert go(only_status=["gap"]) == []
    assert go(target_slug="nope") == []
    out = go(only_status=["review"], limit=1)
    assert [s["slug"] for s in out] == ["acme"] and len(out[0]["reports"]) == 2
    assert {r["filename"]: r["is_primary"] for r in out[0]["reports"]} == {"big.pdf": True, "small.pdf": False}
    assert seen and len(n.calls) == 2
    go(force=True)
    assert len(n.calls) == 4


# ── the routes ───────────────────────────────────────────────────────────────
@pytest.fixture
def api(world, cfg, monkeypatch):
    n = seed_target(world, cfg, monkeypatch)
    asyncio.run(nz.normalise_batch(world.factory, n.batch_id))
    acting = SimpleNamespace(ws=world.ws)
    app = FastAPI()
    app.include_router(reports_api.router)

    async def _db():
        async with world.factory() as session:
            yield session

    async def _user():
        return world.user

    async def _ws():
        return WorkspaceContext(Workspace(id=acting.ws.id, name=acting.ws.name, kind="team"), "analyst")

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    with TestClient(app) as client:
        n.client, n.acting, n.world, n.cfg = client, acting, world, cfg
        yield n


def test_every_route_is_a_404_while_the_setting_is_off(api, monkeypatch):
    monkeypatch.setattr(api.cfg, "disclosure_normaliser", "off")
    rid = uuid.uuid4()
    for url in ("/api/disclosure-reports", f"/api/disclosure-reports?batch_id={api.batch_id}",
                f"/api/disclosure-reports/{rid}", f"/api/disclosure-reports/{rid}/markdown"):
        assert api.client.get(url).status_code == 404, url


def test_the_list_carries_metrics_but_never_the_markdown(api):
    r = api.client.get("/api/disclosure-reports", params={"batch_id": str(api.batch_id)})
    assert r.status_code == 200
    reports = r.json()["reports"]
    assert {x["is_primary"] for x in reports} == {True, False} and len(reports) == 2
    first = reports[0]
    assert first["target_slug"] == "acme" and first["status"] == "normalised" and first["metrics"]["pages"] == 1
    assert first["has_markdown"] is True and first["normaliser_version"] == nz.NORMALISER_VERSION
    assert "markdown" not in first and "markdown_key" not in first and "x" * 100 not in r.text
    assert len(api.client.get("/api/disclosure-reports").json()["reports"]) == 2


def test_one_report_and_its_markdown(api):
    rid = api.client.get("/api/disclosure-reports", params={"batch_id": str(api.batch_id)}).json()["reports"][0]["id"]
    one = api.client.get(f"/api/disclosure-reports/{rid}")
    assert one.status_code == 200 and one.json()["id"] == rid and one.json()["target_label"] == "Acme"
    md = api.client.get(f"/api/disclosure-reports/{rid}/markdown")
    assert md.status_code == 200 and md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("## Page 1\n") and hashlib.sha256(md.content).hexdigest() == one.json()["markdown_sha256"]
    assert api.client.get(f"/api/disclosure-reports/{uuid.uuid4()}").status_code == 404


def test_another_workspaces_reports_are_a_404(api):
    rid = api.client.get("/api/disclosure-reports", params={"batch_id": str(api.batch_id)}).json()["reports"][0]["id"]
    api.acting.ws = api.world.other_ws
    assert api.client.get(f"/api/disclosure-reports/{rid}").status_code == 404
    assert api.client.get(f"/api/disclosure-reports/{rid}/markdown").status_code == 404
    assert api.client.get("/api/disclosure-reports", params={"batch_id": str(api.batch_id)}).status_code == 404
    assert api.client.get("/api/disclosure-reports").json() == {"reports": []}


def test_a_missing_blob_is_a_404_not_a_500(api):
    rid = api.client.get("/api/disclosure-reports", params={"batch_id": str(api.batch_id)}).json()["reports"][0]["id"]
    for p in Path(api.cfg.storage_dir, "normalised").rglob("*.md"):
        p.unlink()
    assert api.client.get(f"/api/disclosure-reports/{rid}/markdown").status_code == 404


# ── the CLI ──────────────────────────────────────────────────────────────────
def run_cli(monkeypatch, *argv: str) -> int:
    from tret import cli

    monkeypatch.setattr(sys, "argv", ["tret", *argv])
    try:
        cli.main()
    except SystemExit as e:
        return int(e.code or 0)
    return 0


@needs_poppler
def test_normalise_file_writes_markdown_and_metrics_without_a_database(tmp_path, cfg, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "disclosure_normaliser", "off")  # the manual check needs no setting
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: pytest.fail("no database"))
    src = tmp_path / "in" / "Report 2025.pdf"
    src.parent.mkdir()
    src.write_bytes(table_pdf())
    out = tmp_path / "out"
    assert run_cli(monkeypatch, "disclosure", "normalise-file", str(src), "--out", str(out), "--no-docling") == 0
    md = (out / "Report 2025.md").read_text()
    metrics = json.loads((out / "Report 2025.metrics.json").read_text())
    assert [ln for ln in md.splitlines() if ln.startswith("## Page ")] == ["## Page 1", "## Page 2", "## Page 3"]
    assert metrics["file"] == "Report 2025.pdf" and metrics["metrics"]["pages"] == 3
    assert metrics["metrics"]["table_pages_selected"] == 1 and metrics["metrics"]["docling"] == "off"
    assert metrics["status"] == "quarantined" or metrics["status"] == "normalised"
    assert "Report 2025.pdf:" in capsys.readouterr().out


def test_normalise_file_reports_a_bad_file(tmp_path, cfg, monkeypatch, capsys):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert run_cli(monkeypatch, "disclosure", "normalise-file", str(bad), "--out", str(tmp_path / "o")) == 1
    assert run_cli(monkeypatch, "disclosure", "normalise-file", str(tmp_path / "missing.pdf"), "--out", str(tmp_path / "o")) == 2
    assert "error" in capsys.readouterr().err


def test_normalise_needs_the_setting_and_a_valid_batch_id(cfg, monkeypatch, capsys):
    monkeypatch.setattr(cfg, "disclosure_normaliser", "off")
    assert run_cli(monkeypatch, "disclosure", "normalise", "--batch", str(uuid.uuid4())) == 2
    assert "TRET_DISCLOSURE_NORMALISER=on" in capsys.readouterr().err
    monkeypatch.setattr(cfg, "disclosure_normaliser", "on")
    assert run_cli(monkeypatch, "disclosure", "normalise", "--batch", "not-a-uuid") == 2


def test_normalise_runs_a_batch_from_the_cli(world, cfg, monkeypatch, capsys):
    n = seed_target(world, cfg, monkeypatch)
    monkeypatch.setattr("tret.db.engine.get_session_factory", lambda: world.factory)
    assert run_cli(monkeypatch, "disclosure", "normalise", "--batch", str(n.batch_id), "--only-status", "review") == 0
    out = capsys.readouterr().out
    assert "acme: 2 report(s)" in out and "[primary] big.pdf" in out
    assert "1 target(s), 2 report(s): 2 normalised, 0 quarantined, 0 failed" in out
    assert run_cli(monkeypatch, "disclosure", "normalise", "--batch", str(uuid.uuid4())) == 1


# ── wiring ───────────────────────────────────────────────────────────────────
def test_the_normaliser_is_off_by_default_with_the_documented_defaults():
    from tret.config import Settings

    s = Settings(_env_file=None)
    assert (s.disclosure_normaliser, s.normaliser_docling) == ("off", "auto")
    assert (s.normaliser_max_rss_mb, s.normaliser_chunk_pages) == (3200, 3)
    assert (s.normaliser_chunk_timeout_seconds, s.normaliser_max_table_pages) == (300, 150)


def test_the_blob_key_is_flat_and_validated():
    assert normalised_key("abc", "1") == "normalised/abc/1.md"
    with pytest.raises(ValueError):
        normalised_key("abc", "../1")


def test_docling_is_an_optional_extra_only():
    import tomllib

    pyproject = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())
    extras = pyproject["project"]["optional-dependencies"]
    assert extras["normalise"] == ["docling>=2"]
    flat = " ".join(pyproject["project"]["dependencies"]) + " ".join(extras["server"]) + " ".join(extras["dev"])
    assert "docling" not in flat and "pymupdf" not in flat.lower()


# ── review round: docling output is judged after cleaning ────────────────────
def test_an_image_only_docling_page_keeps_its_pdftotext_text(tmp_path, cfg, monkeypatch):
    texts = three_table_pages(monkeypatch, table=(2, 3))
    docling_on(monkeypatch, cfg, chunk_pages=5)
    monkeypatch.setattr(
        nz, "convert_chunk",
        lambda path, pages, s: nz.ChunkResult(pages={2: "<!-- image -->", 3: md_table_for(3)}, ok=True),
    )
    result = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg)
    m = result.metrics
    assert (m["table_pages_converted"], m["table_pages_fallback"], m["table_pages_rejected"]) == (1, 1, [2])
    assert m["chunks"][0]["rejected"] == [2]
    assert texts[1].splitlines()[1] in result.markdown and nz.UNSTRUCTURED_NOTE in result.markdown


def test_a_docling_page_that_lost_most_of_the_numbers_is_rejected(tmp_path, cfg, monkeypatch):
    three_table_pages(monkeypatch, table=(2, 3))
    docling_on(monkeypatch, cfg, chunk_pages=5)
    thin = "| Metric | 2025 |\n| --- | --- |\n| Scope 1 | 1001 |\n| Scope 2 | 1002 |"  # a few of ~34 numbers
    monkeypatch.setattr(
        nz, "convert_chunk", lambda path, pages, s: nz.ChunkResult(pages={2: thin, 3: md_table_for(3)}, ok=True)
    )
    m = nz.normalise_pdf_file(tmp_path / "x.pdf", cfg).metrics
    assert m["table_pages_rejected"] == [2] and m["table_pages_converted"] == 1
    assert m["table_numeric_recall_min"] >= nz.MIN_DOCLING_RECALL


# ── review round: the child never outlives its parent's exception ────────────
def pid_alive(pid: int) -> bool:
    import os

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_an_exception_while_waiting_kills_the_child(monkeypatch, tmp_path):
    pidfile = tmp_path / "pid"
    stub_worker(monkeypatch, f"import os,time; open({str(pidfile)!r},'w').write(str(os.getpid())); time.sleep(60)")

    def interrupted(pid):
        raise KeyboardInterrupt

    monkeypatch.setattr(nz, "rss_mb", interrupted)
    with pytest.raises(KeyboardInterrupt):
        nz.run_worker(["x"], timeout_seconds=60, max_rss_mb=1e9, poll_seconds=1.0)
    assert not pid_alive(int(pidfile.read_text()))


def test_a_killed_child_leaves_no_temp_files_behind(monkeypatch, tmp_path):
    marker = tmp_path / "where"
    stub_worker(
        monkeypatch,
        "import tempfile,time; d=tempfile.mkdtemp(prefix='tret-chunk-'); open(d+'/chunk.pdf','w').write('x');"
        f"open({str(marker)!r},'w').write(d); time.sleep(60)",
    )
    monkeypatch.setattr(nz, "rss_mb", lambda pid: 9999.0)
    run = nz.run_worker(["x"], timeout_seconds=60, max_rss_mb=100, poll_seconds=0.5)
    assert run.error and "memory" in run.error
    assert marker.exists() and not Path(marker.read_text()).exists()


def test_the_esef_child_uses_the_configured_memory_guard(tmp_path, cfg, monkeypatch):
    seen = {}

    def fake_run(args, *, timeout_seconds, max_rss_mb, poll_seconds=0.25):
        seen["rss"] = max_rss_mb
        return nz.WorkerRun(None, "stop here", 0.0, 0.0)

    monkeypatch.setattr(nz, "run_worker", fake_run)
    monkeypatch.setattr(cfg, "normaliser_max_rss_mb", 1234)
    nz.normalise_esef_file(tmp_path / "f.zip", cfg)
    assert seen["rss"] == 1234.0


# ── review round: page counting and the pypdf fallback are guarded ───────────
@needs_poppler
def test_pdfinfo_gives_the_page_count_and_garbage_gives_none(tmp_path):
    path = tmp_path / "t.pdf"
    path.write_bytes(prose_pdf(3))
    assert nz.pdfinfo_pages(path) == 3
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    assert nz.pdfinfo_pages(bad) is None


@needs_poppler
@pytest.mark.parametrize("how", ["poppler_missing", "page_count_disagrees"])
def test_pypdf_fallback_runs_in_the_guarded_child(tmp_path, cfg, monkeypatch, how):
    path = tmp_path / "t.pdf"
    path.write_bytes(prose_pdf(3))
    if how == "poppler_missing":
        monkeypatch.setattr(nz, "pdftotext_pages", lambda p: None)
    else:
        monkeypatch.setattr(nz, "pdfinfo_pages", lambda p: 99)
    monkeypatch.setattr("pypdf.PdfReader", lambda *a, **k: pytest.fail("pypdf must not open the PDF in the parent"))
    pages, extractor = nz.read_pages(path, cfg)
    assert extractor == "pypdf" and len(pages) == 3 and "Page body 2." in pages[1]


def test_the_pypdf_child_failing_is_an_error_not_a_hang(tmp_path, cfg, monkeypatch):
    path = tmp_path / "t.pdf"
    path.write_bytes(b"%PDF")
    stub_worker(monkeypatch, "import sys; sys.exit(2)")
    with pytest.raises(RuntimeError, match="pypdf could not read"):
        nz.pypdf_pages_guarded(path, cfg)


# ── review round: the worker's page_no mapping ───────────────────────────────
def test_the_worker_maps_chunk_pages_back_to_physical_pages():
    from tret.services import normalise_worker as w

    class FakeDoc:
        def export_to_markdown(self, page_no):
            return f"md of chunk page {page_no}"

    assert w.export_pages(FakeDoc(), [40, 41, 90]) == {
        "40": "md of chunk page 1", "41": "md of chunk page 2", "90": "md of chunk page 3",
    }


@needs_poppler
def test_the_real_docling_worker_converts_a_table_page(tmp_path):
    pytest.importorskip("docling")
    path = tmp_path / "t.pdf"
    path.write_bytes(table_pdf())
    out = nz.run_worker(["docling", "--pdf", str(path), "--pages", "2"], timeout_seconds=600, max_rss_mb=6000)
    assert out.error is None and "|" in out.payload["pages"]["2"]


# ── review round: reused documents, duplicates, failures and races ───────────
def test_a_document_a_run_reused_is_found_through_the_runs_document_ids(world, cfg, monkeypatch):
    """`capture_filing` reusing a stored package adds the old document to the run's
    `document_ids` without `fetched_by_run` naming this run."""
    n = seed_target(world, cfg, monkeypatch)
    old_run = uuid.uuid4()
    reused = add_pdf_document(world, cfg, "reused.pdf", b"%PDF reused", run_id=old_run)

    async def attach(db):
        run = await db.get(Run, n.run_id)
        run.document_ids = [*(run.document_ids or []), reused.id]
        await db.commit()

    run_db(world, attach)

    async def go(db):
        target = await db.get(BatchTarget, n.target_id)
        return sorted(d.filename for d in await nz.target_documents(db, target))

    assert run_db(world, go) == ["big.pdf", "reused.pdf", "small.pdf"]


def test_a_document_two_targets_share_keeps_its_first_target_and_records_the_other(world, cfg, monkeypatch):
    n = seed_target(world, cfg, monkeypatch)
    shared = n.big
    second = uuid.uuid4()

    async def go(db):
        first_target = await db.get(BatchTarget, n.target_id)
        db.add(Run(id=(rid := uuid.uuid4()), project_id=world.project.id, harness_id=world.harness.id,
                   task_type="freeform", task_input={}, document_ids=[shared.id], status="completed"))
        await db.flush()
        db.add(BatchTarget(id=second, collection_id=first_target.collection_id, position=2, slug="globex",
                           company="Globex", market="EU", label="Globex", status="review", run_id=rid))
        await db.flush()
        await nz.normalise_target(db, first_target)
        other = await db.get(BatchTarget, second)
        reports = await nz.normalise_target(db, other)
        await db.commit()
        (r,) = reports
        return r.batch_target_id, r.is_primary, r.metrics.get("also_linked_targets")

    owner, primary, also = run_db(world, go)
    assert owner == n.target_id and primary is True and also == [str(second)]


def test_a_failed_forced_rerun_keeps_the_good_report(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    fake_converter(monkeypatch)

    async def go(db):
        good = await nz.normalise_document(db, doc)
        key, digest = good.markdown_key, good.markdown_sha256
        monkeypatch.setattr(nz, "normalise_pdf_file", lambda path, s: nz.NormaliseResult(
            "pdf", nz.STATUS_FAILED, "", {}, error="pdftotext timed out"))
        again = await nz.normalise_document(db, doc, force=True)
        return again.status, again.markdown_key == key, again.markdown_sha256 == digest, again.error, again.metrics

    status, same_key, same_sha, error, metrics = run_db(world, go)
    assert (status, same_key, same_sha, error) == ("normalised", True, True, None)
    assert metrics["last_error"] == "pdftotext timed out" and metrics["pages"] == 1


def test_an_error_on_one_document_marks_it_failed_and_the_batch_goes_on(world, cfg, monkeypatch):
    n = seed_target(world, cfg, monkeypatch)
    import tret.storage as storage

    real = storage.document_local_path

    async def flaky(doc, settings=None):
        if doc.filename == "big.pdf":
            raise PermissionError("disk unplugged")
        return await real(doc, settings)

    monkeypatch.setattr(storage, "document_local_path", flaky)
    out = asyncio.run(nz.normalise_batch(world.factory, n.batch_id))
    got = {r["filename"]: (r["status"], r["error"]) for r in out[0]["reports"]}
    assert got["small.pdf"] == ("normalised", None)
    assert got["big.pdf"][0] == "failed" and "PermissionError: disk unplugged" in got["big.pdf"][1]


def test_a_storage_failure_while_saving_the_markdown_marks_the_report_failed(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    fake_converter(monkeypatch)

    async def boom(self, *a, **k):
        raise OSError("bucket gone")

    monkeypatch.setattr(type(get_store(cfg)), "put", boom)

    async def go(db):
        r = await nz.normalise_document(db, doc)
        return r.status, r.error, r.markdown_key

    status, error, key = run_db(world, go)
    assert status == "failed" and "could not store the Markdown" in error and key is None


def test_two_processes_creating_the_same_report_end_on_one_row(world, cfg, monkeypatch):
    doc = add_pdf_document(world, cfg, "a.pdf", b"%PDF-1.4 a")
    fake_converter(monkeypatch)
    real = nz._existing_report
    stale = iter([True])

    async def racing(session, document_id):
        # the first look says "no row yet", as it would for the loser of a race
        return None if next(stale, False) else await real(session, document_id)

    async def go(db):
        from sqlalchemy import select

        a = await nz.normalise_document(db, doc)
        await db.commit()
        monkeypatch.setattr(nz, "_existing_report", racing)
        b = await nz.normalise_document(db, doc, force=True)
        await db.commit()
        rows = (await db.execute(select(DisclosureReport))).scalars().all()
        return a.id, b.id, len(rows)

    first, second, count = run_db(world, go)
    assert first == second and count == 1
