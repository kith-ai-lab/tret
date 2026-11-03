"""`capture_source`: collecting a public file whole, with provenance.

`fetch_url` reads pages; this tool collects originals, and the two differ in
exactly the ways that make a collected PDF worth something later:

1. **Never a truncated file.** A 15 MB issuer report cut at 2 MB is a PDF that
   opens, hashes, and is wrong. Over the limit is an error, and nothing is stored.
2. **The record travels with the bytes.** Hash, retrieval time and the response
   headers live in `Document.meta`, so a reviewer can tell later whether the
   source moved.
3. **`fetch_url` is untouched.** Same 2 MB cap, same content types, same counter.

Nothing here opens a socket: the tool tests stub `fetch_page`, and the
`fetch_page` tests run the real function over an in-memory httpx transport.
"""

# ruff: noqa: F811  (fixtures are imported, then named as test arguments)
from __future__ import annotations

import gzip
import hashlib
import io
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

# Reused, not copied: the sqlite tret, the run context and the mode switch are the
# same fixtures the sibling file's tests stand on.
from test_web_tools import _no_leaked_overrides, ctx, db, research  # noqa: F401

from tret.db.models import Document, EgressCall
from tret.engine import tools as tools_module
from tret.engine.tools import (
    ToolError,
    capture_source,
    fetch_url,
    read_document,
    get_builtin_tools,
    withheld_web_tools,
)
from tret.net.fetch import SOURCE_KIND_WEB, FetchedPage, FetchError, fetch_page
from tret.net.fetch import fetch as fetch_module
from tret.net.policy import MODE_OFF, MODE_ON, MODE_REPLAY

URL = "https://issuer.example.com/reports/2025-sustainability.pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HEADERS = (
    ("content-length", "123"),
    ("last-modified", "Tue, 01 Jul 2025 10:00:00 GMT"),
    ("etag", '"abc123"'),
    ("content-disposition", 'attachment; filename="report.pdf"'),
)


def _pdf_bytes(pad_to: int = 0) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buf = io.BytesIO()
    writer.write(buf)
    data = buf.getvalue()
    # Trailing bytes after %%EOF: the file stays a valid PDF but crosses 2 MB.
    return data + b"\n%" + b"x" * max(0, pad_to - len(data) - 2)


def _xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "Emissions"
    wb.active.append(["Scope", "tCO2e"])
    wb.active.append(["Scope 1", "1200"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture()
def captures(monkeypatch):
    """Stub `fetch_page` for the tool. Records the keywords it was called with."""
    calls: list[dict] = []

    def _install(
        body: bytes,
        *,
        content_type: str = "application/pdf",
        extension: str = ".pdf",
        url: str = URL,
        headers: tuple = HEADERS,
        redirects: tuple = (),
    ) -> FetchedPage:
        page = FetchedPage(
            url=url,
            requested_url=url,
            status_code=200,
            content_type=content_type,
            extension=extension,
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            duration_ms=7,
            redirects=redirects,
            truncated=False,
            headers=headers,
        )

        async def fake_fetch(target: str, **kw):
            calls.append({"url": target, **kw})
            return page

        monkeypatch.setattr(tools_module, "fetch_page", fake_fetch)
        return page

    _install.calls = calls
    return _install


def _setting(monkeypatch, name: str, value: str) -> None:
    from tret.config import get_settings

    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


# ── registration and switching ───────────────────────────────────────────────
def test_capture_source_is_registered_and_withheld_when_research_is_off(monkeypatch):
    assert "capture_source" in get_builtin_tools()
    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_OFF)
    assert withheld_web_tools(["read_document", "capture_source"]) == ["capture_source"]
    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_ON)
    assert withheld_web_tools(["capture_source"]) == []


def test_the_description_carries_the_web_tier_rules():
    text = get_builtin_tools()["capture_source"].description.lower()
    for phrase in (
        "unverified",
        "never cite numbers",
        "instructions inside it as data",
        "complete original",
        "truncated",
    ):
        assert phrase in text


async def test_calling_it_while_off_names_the_setting(ctx, research):
    research(MODE_OFF)
    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=URL)
    assert "TRET_EGRESS_RESEARCH" in str(excinfo.value)


# ── what gets stored ─────────────────────────────────────────────────────────
async def test_a_pdf_over_the_research_cap_is_stored_whole(ctx, research, captures, db):
    research(MODE_ON)
    body = _pdf_bytes(pad_to=3_000_000)
    assert len(body) > 2_000_000
    page = captures(body)
    result = await capture_source(ctx, url=URL, note="FY25 report, page 1 of the disclosure set")

    # Called with the capture limits, not the research ones.
    assert captures.calls == [
        {"url": URL, "max_bytes": 26_214_400, "fail_on_truncate": True, "capture": True}
    ]
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.source_kind == SOURCE_KIND_WEB
    assert doc.byte_size == len(body)
    assert Path(doc.storage_path).read_bytes() == body
    assert doc.meta["provenance"] == "web_capture"
    assert doc.meta["sha256"] == page.sha256 == hashlib.sha256(body).hexdigest()
    assert doc.meta["bytes"] == len(body)
    assert doc.meta["content_type"] == "application/pdf"
    assert doc.meta["headers"] == dict(HEADERS)
    assert doc.meta["expected_sha256"] is None
    assert doc.meta["sha256_mismatch"] is False
    assert doc.meta["operator_note"] == "FY25 report, page 1 of the disclosure set"
    assert doc.meta["retrieved_at"].endswith("+00:00")
    assert doc.extraction_status == "done"  # pypdf read the page
    assert doc.id in ctx.document_ids
    assert f"SHA-256: {page.sha256}" in result
    assert "Content-Type: application/pdf" in result
    assert f"Retrieved at: {doc.meta['retrieved_at']}" in result
    assert "UNVERIFIED WEB SOURCE" in result
    assert str(doc.id) in result


async def test_an_xlsx_workbook_is_stored_and_extracted(ctx, research, captures, db):
    research(MODE_ON)
    captures(
        _xlsx_bytes(),
        content_type=XLSX,
        extension=".xlsx",
        url="https://issuer.example.com/data.xlsx",
    )
    result = await capture_source(ctx, url="https://issuer.example.com/data.xlsx")
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.filename.endswith(".xlsx")
    assert doc.extraction_status == "done"
    assert "Scope 1" in doc.extracted_text
    assert "Scope 1" in result


async def test_a_zip_is_stored_as_the_original_and_not_extracted(ctx, research, captures, db):
    research(MODE_ON)
    body = b"PK\x03\x04" + b"\0" * 200
    captures(
        body,
        content_type="application/zip",
        extension=".zip",
        url="https://issuer.example.com/esef.zip",
    )
    result = await capture_source(ctx, url="https://issuer.example.com/esef.zip")
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.extraction_status == "skipped"
    assert doc.meta["error"] == "archive stored as original; not extracted"
    assert doc.extracted_text == ""
    assert Path(doc.storage_path).read_bytes() == body
    assert "extraction was skipped" in result.lower()


async def test_xhtml_keeps_its_extension_and_is_read_as_html(ctx, research, captures, db):
    research(MODE_ON)
    body = b"<html><body><p>Scope 1 emissions were reported.</p></body></html>"
    captures(
        body,
        content_type="application/xhtml+xml",
        extension=".xhtml",
        url="https://issuer.example.com/esef",
    )
    await capture_source(ctx, url="https://issuer.example.com/esef")
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.filename.endswith(".xhtml")
    assert doc.extraction_status == "done"
    assert "Scope 1 emissions were reported." in doc.extracted_text


# ── expected_sha256 ──────────────────────────────────────────────────────────
async def test_a_changed_source_is_stored_and_flagged_with_both_hashes(ctx, research, captures, db):
    research(MODE_ON)
    page = captures(_pdf_bytes())
    old = "ab" * 32
    result = await capture_source(ctx, url=URL, expected_sha256=old.upper())

    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.meta["sha256_mismatch"] is True
    assert doc.meta["expected_sha256"] == old  # normalised to lower case
    assert "SOURCE CHANGED" in result
    assert old in result and page.sha256 in result
    assert "re-review" in result


async def test_a_matching_expected_hash_is_not_flagged(ctx, research, captures, db):
    research(MODE_ON)
    page = captures(_pdf_bytes())
    result = await capture_source(ctx, url=URL, expected_sha256=page.sha256)
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.meta["sha256_mismatch"] is False
    assert "SOURCE CHANGED" not in result


@pytest.mark.parametrize("bad", ["abc", "z" * 64, "ab" * 33, "sha256:" + "ab" * 32])
async def test_a_malformed_expected_hash_is_a_tool_error(ctx, research, captures, db, bad):
    research(MODE_ON)
    captures(_pdf_bytes())
    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=URL, expected_sha256=bad)
    assert "expected_sha256" in str(excinfo.value)
    assert (await db.execute(select(Document))).scalars().all() == []
    assert captures.calls == []  # refused before any request


async def test_input_validation_covers_the_url_and_the_note(ctx, research, captures):
    research(MODE_ON)
    captures(_pdf_bytes())
    with pytest.raises(ToolError):
        await capture_source(ctx, url="http://issuer.example.com/a.pdf")
    with pytest.raises(ToolError):
        await capture_source(ctx, url="/relative/a.pdf")
    with pytest.raises(ToolError):
        await capture_source(ctx, url=URL, note="n" * 501)


# ── refusing instead of truncating ───────────────────────────────────────────
async def test_an_oversize_file_is_refused_and_nothing_is_stored(ctx, research, monkeypatch, db):
    research(MODE_ON)

    async def too_big(url, **kw):
        raise FetchError(
            f"{url} is larger than the {kw['max_bytes']}-byte limit; refusing to store a truncated copy"
        )

    monkeypatch.setattr(tools_module, "fetch_page", too_big)
    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=URL)
    assert "26214400-byte limit" in str(excinfo.value)

    await db.rollback()  # the engine rolls a failed tool call back; the audit row must survive
    assert (await db.execute(select(Document))).scalars().all() == []
    call = (await db.execute(select(EgressCall))).scalars().one()
    assert call.decision == "denied" and call.reason.startswith("fetch_failed")
    # The attempt spent a slot; no bytes were stored.
    assert ctx.web_captures == 1 and ctx.web_bytes == 0


# ── budgets are separate ─────────────────────────────────────────────────────
async def test_captures_have_their_own_budget_and_leave_the_fetch_counter_alone(
    ctx, research, captures, monkeypatch
):
    research(MODE_ON)
    page = captures(_pdf_bytes())
    _setting(monkeypatch, "TRET_EGRESS_CAPTURE_MAX_PER_RUN", "1")

    await capture_source(ctx, url=URL)
    assert ctx.web_captures == 1
    assert ctx.web_fetches == 0
    assert ctx.web_bytes == len(page.body)

    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=URL)
    assert "TRET_EGRESS_CAPTURE_MAX_PER_RUN=1" in str(excinfo.value)
    assert len(captures.calls) == 1  # the second never reached the network


async def test_a_spent_fetch_budget_does_not_block_a_capture(ctx, research, captures, monkeypatch):
    research(MODE_ON)
    captures(_pdf_bytes())
    ctx.web_fetches = 10_000
    await capture_source(ctx, url=URL)
    assert ctx.web_captures == 1


async def test_replay_serves_a_captured_snapshot_without_a_request(
    ctx, research, captures, monkeypatch, db
):
    research(MODE_ON)
    page = captures(_pdf_bytes())
    await capture_source(ctx, url=URL)

    async def explode(url, **kw):
        raise AssertionError("replay must not fetch")

    monkeypatch.setattr(tools_module, "fetch_page", explode)
    research(MODE_REPLAY)
    ctx.document_ids.clear()
    result = await capture_source(ctx, url=URL, expected_sha256="cd" * 32)
    assert "REPLAYED" in result and page.sha256 in result
    assert "SOURCE CHANGED" in result
    assert len(ctx.document_ids) == 1


async def test_a_failed_capture_still_spends_a_slot(ctx, research, monkeypatch, captures, db):
    research(MODE_ON)
    _setting(monkeypatch, "TRET_EGRESS_CAPTURE_MAX_PER_RUN", "1")

    async def refused(url, **kw):
        raise FetchError("too big")

    monkeypatch.setattr(tools_module, "fetch_page", refused)
    with pytest.raises(ToolError) as first:
        await capture_source(ctx, url=URL)
    assert "too big" in str(first.value)
    assert ctx.web_captures == 1

    captures(_pdf_bytes())  # a retry that would succeed never gets to run
    with pytest.raises(ToolError) as second:
        await capture_source(ctx, url=URL)
    assert "TRET_EGRESS_CAPTURE_MAX_PER_RUN=1" in str(second.value)
    assert captures.calls == []


async def test_replay_returns_the_capture_not_a_newer_truncated_fetch_url_copy(
    ctx, research, wire, monkeypatch, db
):
    research(MODE_ON)
    body = b"a" * 3_000_000
    wire(lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=body))
    url = "https://issuer.example.com/report.txt"
    await capture_source(ctx, url=url)
    await fetch_url(ctx, url=url)  # 2 MB truncated, and the newer of the two

    docs = {d.meta["provenance"]: d for d in (await db.execute(select(Document))).scalars()}
    assert docs["web"].byte_size == 2_000_000 and docs["web"].meta["body_truncated"] is True
    docs["web"].created_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    await db.flush()

    async def explode(url, **kw):
        raise AssertionError("replay must not fetch")

    monkeypatch.setattr(tools_module, "fetch_page", explode)
    research(MODE_REPLAY)
    ctx.document_ids.clear()
    result = await capture_source(ctx, url=url)
    assert ctx.document_ids == [docs["web_capture"].id]
    assert docs["web_capture"].sha256 in result and docs["web"].sha256 not in result
    assert "3000000 bytes" in result
    # fetch_url replay is unchanged: it still takes the newest copy of any kind.
    ctx.document_ids.clear()
    await fetch_url(ctx, url=url)
    assert ctx.document_ids == [docs["web"].id]


async def test_replay_refuses_a_page_that_was_only_read_with_fetch_url(
    ctx, research, wire, monkeypatch
):
    research(MODE_ON)
    url = "https://issuer.example.com/page.txt"
    wire(lambda req: httpx.Response(200, headers={"content-type": "text/plain"}, content=b"hello"))
    await fetch_url(ctx, url=url)
    research(MODE_REPLAY)
    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=url)
    assert "not a capture" in str(excinfo.value)


async def test_replay_refuses_a_truncated_capture_record(ctx, research, captures, db):
    research(MODE_ON)
    captures(_pdf_bytes())
    await capture_source(ctx, url=URL)
    doc = (await db.execute(select(Document))).scalars().one()
    doc.meta = {**doc.meta, "body_truncated": True}
    await db.flush()
    research(MODE_REPLAY)
    with pytest.raises(ToolError) as excinfo:
        await capture_source(ctx, url=URL)
    assert "no untruncated capture" in str(excinfo.value)


async def test_read_document_banner_carries_the_hash_and_the_changed_source_warning(
    ctx, research, captures, db
):
    """The capture_source result is elidable; the banner on every read is not."""
    research(MODE_ON)
    page = captures(_pdf_bytes())
    await capture_source(ctx, url=URL, expected_sha256="ab" * 32)
    doc = (await db.execute(select(Document))).scalars().one()
    text = await read_document(ctx, document_id=str(doc.id))
    assert f"SHA-256: {page.sha256}" in text
    assert "SOURCE CHANGED since the expected hash" in text
    assert "need re-review" in text


async def test_read_document_banner_for_a_clean_capture_has_the_hash_and_no_warning(
    ctx, research, captures, db
):
    research(MODE_ON)
    page = captures(_pdf_bytes())
    await capture_source(ctx, url=URL, expected_sha256=page.sha256)
    doc = (await db.execute(select(Document))).scalars().one()
    text = await read_document(ctx, document_id=str(doc.id))
    assert f"SHA-256: {page.sha256}" in text
    assert "SOURCE CHANGED" not in text


async def test_the_result_shows_the_content_type_as_served(ctx, research, captures):
    """A login wall stored at a .pdf URL must look like what it is."""
    research(MODE_ON)
    captures(
        b"<html><body>Please sign in</body></html>",
        content_type="text/html",
        extension=".html",
        url="https://issuer.example.com/report.pdf",
    )
    result = await capture_source(ctx, url="https://issuer.example.com/report.pdf")
    assert "Content-Type: text/html" in result


# ── fetch_url did not change ─────────────────────────────────────────────────
@pytest.fixture()
def wire(monkeypatch):
    """Run the real `fetch_page` over an in-memory transport: no socket, no DNS."""

    def _install(handler):
        @asynccontextmanager
        async def fake_open_client(
            egress_class, *, timeout=None, headers=None, follow_redirects=False
        ):
            async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler), headers=headers, timeout=timeout
            ) as client:
                yield client

        async def allow(url, egress_class, policy=None):
            return None

        monkeypatch.setattr(fetch_module, "open_client", fake_open_client)
        monkeypatch.setattr(fetch_module, "check_url", allow)

    return _install


async def test_fetch_url_still_truncates_at_the_research_cap_and_stores(ctx, research, wire, db):
    research(MODE_ON)
    wire(
        lambda req: httpx.Response(
            200, headers={"content-type": "text/plain"}, content=b"a" * 3_000_000
        )
    )
    result = await fetch_url(ctx, url="https://issuer.example.com/big.txt")
    doc = (await db.execute(select(Document))).scalars().one()
    assert doc.byte_size == 2_000_000
    assert doc.meta["body_truncated"] is True
    assert doc.meta["provenance"] == "web"
    assert "Recorded as document" in result
    assert ctx.web_fetches == 1 and ctx.web_captures == 0


async def test_fetch_url_still_refuses_what_only_capture_accepts(ctx, research, wire, db):
    research(MODE_ON)
    wire(
        lambda req: httpx.Response(
            200, headers={"content-type": "application/zip"}, content=b"PK\x03\x04"
        )
    )
    with pytest.raises(ToolError) as excinfo:
        await fetch_url(ctx, url="https://issuer.example.com/a.zip")
    assert "application/zip" in str(excinfo.value)
    assert "application/zip" not in fetch_module.READABLE_TYPES


# ── fetch_page: limits and sniffing ──────────────────────────────────────────
def _respond(content_type: str, body: bytes, **headers):
    def handler(req):
        return httpx.Response(200, headers={"content-type": content_type, **headers}, content=body)

    return handler


async def test_capture_refuses_a_body_over_the_limit_by_its_streamed_size(wire):
    # No content-length (a generator body is chunked), so only the streaming check can catch it.
    def handler(req):
        async def body():
            for _ in range(10):
                yield b"x" * 1000

        return httpx.Response(200, headers={"content-type": "text/plain"}, content=body())

    wire(handler)
    with pytest.raises(FetchError) as excinfo:
        await fetch_page("https://issuer.example.com/a.txt", max_bytes=5000, fail_on_truncate=True)
    assert "5000-byte limit" in str(excinfo.value)


async def test_capture_refuses_early_on_a_declared_length_over_the_limit(wire):
    wire(_respond("application/pdf", _pdf_bytes(pad_to=20_000)))
    with pytest.raises(FetchError) as excinfo:
        await fetch_page(
            "https://issuer.example.com/a.pdf",
            max_bytes=10_000,
            fail_on_truncate=True,
            capture=True,
        )
    assert "10000-byte limit" in str(excinfo.value)


async def test_a_body_exactly_at_the_limit_is_accepted_whole(wire):
    wire(_respond("text/plain", b"y" * 5000))
    page = await fetch_page(
        "https://issuer.example.com/a.txt", max_bytes=5000, fail_on_truncate=True
    )
    assert len(page.body) == 5000 and page.truncated is False


async def test_octet_stream_with_pdf_magic_is_a_pdf_only_in_capture_mode(wire):
    body = _pdf_bytes()
    wire(_respond("application/octet-stream", body))
    page = await fetch_page("https://issuer.example.com/download?id=7", capture=True)
    assert page.extension == ".pdf" and page.body == body
    assert page.content_type == "application/octet-stream"
    with pytest.raises(FetchError) as excinfo:
        await fetch_page("https://issuer.example.com/download?id=7")
    assert "octet-stream" in str(excinfo.value)


async def test_octet_stream_that_is_not_a_pdf_is_refused(wire):
    wire(_respond("application/octet-stream", b"MZ\x90\x00" + b"\0" * 100))
    with pytest.raises(FetchError) as excinfo:
        await fetch_page("https://issuer.example.com/setup.exe", capture=True)
    assert "not a PDF" in str(excinfo.value) or "%PDF-" in str(excinfo.value)


async def test_a_short_octet_stream_body_is_checked_too(wire):
    wire(_respond("application/octet-stream", b"%P"))
    with pytest.raises(FetchError):
        await fetch_page("https://issuer.example.com/tiny", capture=True)


async def test_application_pdf_with_an_html_body_is_refused_in_capture_mode(wire):
    wire(_respond("application/pdf", b"<html><body>404 Not Found</body></html>"))
    with pytest.raises(FetchError) as excinfo:
        await fetch_page("https://issuer.example.com/a.pdf", capture=True)
    assert "%PDF-" in str(excinfo.value)


async def test_capture_mode_widens_the_types_and_normal_mode_does_not(wire):
    wire(_respond(XLSX, _xlsx_bytes()))
    page = await fetch_page("https://issuer.example.com/a.xlsx", capture=True)
    assert page.extension == ".xlsx"
    with pytest.raises(FetchError):
        await fetch_page("https://issuer.example.com/a.xlsx")

    wire(_respond("application/xhtml+xml", b"<html/>"))
    assert (await fetch_page("https://issuer.example.com/e", capture=True)).extension == ".xhtml"
    assert (await fetch_page("https://issuer.example.com/e")).extension == ".html"


async def test_only_the_provenance_headers_are_kept_lower_cased(wire):
    packed = gzip.compress(b"hi" * 100)
    wire(
        _respond(
            "text/plain",
            packed,
            **{
                "ETag": '"v1"',
                "Last-Modified": "Tue, 01 Jul 2025 10:00:00 GMT",
                "Content-Encoding": "gzip",
                "Set-Cookie": "sid=secret",
            },
        )
    )
    page = await fetch_page("https://issuer.example.com/a.txt")
    names = dict(page.headers)
    assert names["etag"] == '"v1"'
    assert names["last-modified"] == "Tue, 01 Jul 2025 10:00:00 GMT"
    # Recorded so a compressed content-length is not mistaken for truncation: the
    # stored body is the decoded one, longer than the length the server declared.
    assert names["content-encoding"] == "gzip"
    assert names["content-length"] == str(len(packed))
    assert page.body == b"hi" * 100 and len(page.body) > len(packed)
    assert "set-cookie" not in names
    assert set(names) <= {
        "content-length",
        "content-encoding",
        "last-modified",
        "etag",
        "content-disposition",
    }
