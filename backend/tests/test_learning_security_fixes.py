"""Regression tests for the Learning security batch: H2 (upload-time drive check),
H3 (import_table source kinds), M1 (linear HTML scan, extraction bounds), L1
(html_blocks follows the renderer), L3 (reserved `session-` slugs).

H1 (chat save derives its bytes from the message) lives in test_learning_files.py
beside the other R19 endpoint tests; the `_session_folders` strip in
test_run_lineage_api.py beside the other stripped-key tests."""
from __future__ import annotations

import time
import types
import uuid

import pydantic
import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.api.chat_preview import html_blocks
from tret.api.connections import WriteTargetEntry
from tret.db.models import Base, Document, Project, Workspace
from tret.engine import html_scan
from tret.engine.html_check import check_reply_html
from tret.engine.tools import RunContext, ToolError, import_table
from tret.services import connections as cs
from tret.services import documents as docs

pytestmark = pytest.mark.asyncio


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sec.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        ws = Workspace(name="W", settings={"edition": "cohort"})
        session.add(ws)
        await session.flush()
        session.add(Project(workspace_id=ws.id, name="P"))
        await session.commit()
        yield session
    await engine.dispose()


# ── H3: import_table accepts only member-supplied bytes ──────────────────────
async def _doc(db, tmp_path, kind: str) -> tuple[Document, Project]:
    project = (await db.execute(select(Project))).scalars().one()
    data = b"a,b\n1,2\n"
    path = tmp_path / f"{kind}.csv"
    path.write_bytes(data)
    doc = Document(
        project_id=project.id, filename=f"{kind}.csv", content_type="text/csv", byte_size=len(data),
        storage_path=str(path), sha256=uuid.uuid4().hex, extraction_status="done", source_kind=kind,
    )
    db.add(doc)
    await db.flush()
    return doc, project


def _ctx(db, project, doc) -> RunContext:
    return RunContext(
        db=db, run_id=uuid.uuid4(), project_id=project.id, pack_id=None, doctrine_sha=None,
        model_used=None, document_ids=[doc.id], output_schemas={}, workspace_id=project.workspace_id,
    )


@pytest.mark.parametrize("kind", ["generated", "web", "package", "batch_record", "shared"])
async def test_h3_import_table_refuses_non_member_sources(db, tmp_path, kind):
    doc, project = await _doc(db, tmp_path, kind)
    with pytest.raises(
        ToolError, match="written by Tret" if kind == "generated" else "not a member-supplied file"
    ):
        await import_table(_ctx(db, project, doc), name="t", document_id=str(doc.id))


@pytest.mark.parametrize("kind", ["upload", "connected"])
async def test_h3_import_table_accepts_upload_and_connected(db, tmp_path, kind):
    doc, project = await _doc(db, tmp_path, kind)
    out = await import_table(_ctx(db, project, doc), name=f"t_{kind}", document_id=str(doc.id))
    assert '"rows": 1' in out


# ── H2: an upload target must sit on one of the workspace's allowed drives ────
async def test_h2_upload_refuses_a_target_outside_the_allowed_sources(db, monkeypatch):
    ws = (await db.execute(select(Workspace))).scalars().one()
    conn = types.SimpleNamespace(granted_scopes=["Files.ReadWrite.All", "Sites.ReadWrite.All"])

    async def _usable(db, workspace_id, provider=None):
        return conn

    async def _sources(db, workspace_id):
        return [types.SimpleNamespace(drive_id="allowed-drive")]

    reached_network = []

    async def _token(*a, **k):
        reached_network.append(1)
        return "t"

    monkeypatch.setattr(cs, "ensure_connection_usable", _usable)
    monkeypatch.setattr(cs, "list_connected_sources", _sources)
    monkeypatch.setattr(cs, "get_access_token", _token)
    forged = cs.WriteTarget(
        slug="session-1", label="x", path="p", site_id=None, drive_id="someone-elses-drive",
        item_id="i", web_url=None,
    )
    with pytest.raises(cs.ConnectionWriteError, match="not an allowed output folder"):
        await cs.upload_connected_file(
            db, workspace_id=ws.id, target_slug="session-1", filename="a.html", data=b"x",
            content_type="text/html", target=forged,
        )
    assert reached_network == []


# ── L3: `session-` is reserved ───────────────────────────────────────────────
def _entry(slug):
    return WriteTargetEntry(slug=slug, label="l", path="p", drive_id="d", item_id="i")


async def test_l3_admin_slug_cannot_use_the_session_prefix():
    with pytest.raises(pydantic.ValidationError, match="session-"):
        _entry("session-1")
    assert _entry("sessions-notes").slug == "sessions-notes"
    assert _entry("docs").slug == "docs"


async def test_l3_session_target_is_never_replaced_by_a_colliding_workspace_entry(db, monkeypatch, caplog):
    async def _usable(db, workspace_id, provider=None):
        return types.SimpleNamespace()

    ws_target = cs.WriteTarget(
        slug="session-1", label="legacy", path="p", site_id=None, drive_id="d9", item_id="i9", web_url=None
    )
    other = cs.WriteTarget(slug="docs", label="Docs", path="p", site_id=None, drive_id="d9", item_id="i8", web_url=None)

    async def _list(db, workspace_id):
        return [ws_target, other]

    monkeypatch.setattr(cs, "ensure_connection_usable", _usable)
    monkeypatch.setattr(cs, "list_write_targets", _list)
    folders = [{"drive_id": "d1", "item_id": "i1", "name": "Week 3", "path": "Docs/Week 3"}]
    with caplog.at_level("WARNING", logger="tret.connections"):
        out = await cs.resolve_write_targets(
            db, uuid.uuid4(), session_folders=folders, include_session=True
        )
    assert [(t.slug, kind, t.drive_id) for t, kind in out] == [
        ("session-1", "session", "d1"),
        ("docs", "workspace", "d9"),
    ]
    assert "collides with a session target" in caplog.text


# ── L1: html_blocks follows MarkdownDoc.tsx's fence rules ────────────────────
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # info string must be ONE word: "```html title=x" is not a fence (a paragraph line)
        ("```html title=x\n<p>a</p>\n```", []),
        # ...and its stray closer then opens a fence that swallows the next block
        ("```html title=x\n<p>a</p>\n```\n```html\n<p>b</p>\n```", []),
        # any 3+ backtick line closes, whatever length opened the block
        ("````html\n<p>a</p>\n```\ninner\n```\n````", ["<p>a</p>"]),
        # a shorter... or longer closer both close
        ("```html\n<p>a</p>\n`````", ["<p>a</p>"]),
        # indentation is unlimited on open and close; body lines are kept raw
        ("      ```html\n  <p>a</p>\n    ```", ["  <p>a</p>"]),
        # unclosed final fence runs to the end
        ("```html\n<p>a</p>\n<p>b", ["<p>a</p>\n<p>b"]),
        # case-insensitive language, htm alias, other languages skipped
        ("```HTML\n<i>a</i>\n```\n```css\nx{}\n```\n```htm\n<b>b</b>\n```", ["<i>a</i>", "<b>b</b>"]),
        # a language fence inside a block does not close it (only a bare fence does)
        ("```html\n```python\n<p>a</p>\n```", ["```python\n<p>a</p>"]),
        # CRLF input
        ("```html\r\n<p>a</p>\r\n```\r\n", ["<p>a</p>"]),
        # trailing spaces on fence lines
        ("```html   \n<p>a</p>\n```   ", ["<p>a</p>"]),
    ],
)
async def test_l1_html_blocks_matches_the_renderer(text, expected):
    assert html_blocks(text) == expected


async def test_l1_whole_document_detection_matches_isFullHtmlDocument():
    # frontend: /^\s*(<!doctype\s+html[^>]*>|<html[\s>])/i
    assert html_blocks("  <!DOCTYPE html>\n<p>x</p>") == ["<!DOCTYPE html>\n<p>x</p>"]
    assert html_blocks("<html>x</html>") == ["<html>x</html>"]
    assert html_blocks("<html lang=en>x") == ["<html lang=en>x"]
    # not a document by the renderer's rule, so it is fence-scanned (none here)
    assert html_blocks("<!doctype html") == []
    assert html_blocks("<htmlx>y</htmlx>") == []


# ── M1: linear HTML scanning ─────────────────────────────────────────────────
MB = 1024 * 1024


@pytest.mark.parametrize("unit", [b"<a ", b"<!--", b"<script>", b"<a b='", b"</", b"<!", b"<p><"])
async def test_m1_hostile_html_extracts_in_linear_time(unit):
    data = unit * (MB // len(unit))
    t = time.perf_counter()
    docs._extract_html(data)
    assert time.perf_counter() - t < 2.0
    t = time.perf_counter()
    check_reply_html("```html\n" + data.decode() + "\n```")
    assert time.perf_counter() - t < 2.0


async def test_m1_deeply_nested_and_end_tag_storms_stay_fast():
    t = time.perf_counter()
    out = check_reply_html("```html\n<body>" + "<div>" * 200_000 + "</x>" * 200_000 + "\n```")
    assert out["status"] == "issues"
    assert time.perf_counter() - t < 2.0


async def test_m1_extraction_still_reads_a_normal_page():
    page = (
        b"<html><head><title>T</title><style>p{}</style></head><body><h1>Head</h1>"
        b"<script>var x='<b>no</b>';</script><p>One &amp; two</p><ul><li>a</li><li>b</li></ul>"
        b"<table><tr><th>H1</th><th>H2</th></tr><tr><td>1</td><td>2</td></tr></table>"
        b"<!-- hidden --><noscript>nope</noscript><template><p>tpl</p></template><br/>end</body></html>"
    )
    text, meta = docs._extract_html(page)
    assert meta == {}
    assert "# Head" in text and "One & two" in text and "- a" in text and "- b" in text
    assert "H1 | H2" in text and "1 | 2" in text
    for hidden in ("hidden", "nope", "tpl", "var x", "p{}"):
        assert hidden not in text


async def test_m1_tokens_carry_lines_and_colspan():
    toks = list(html_scan.tokenize('<table>\n<tr><td colspan="2">x</td>\n<td ROWSPAN=3>y</td></tr></table>'))
    starts = [(v, line, a) for k, v, line, a in toks if k == html_scan.START]
    assert starts == [
        ("table", 1, None), ("tr", 2, None), ("td", 2, {"colspan": "2"}), ("td", 3, {"rowspan": "3"}),
    ]


async def test_m1_html_check_reports_what_it_did():
    reply = "```html\n<body><table>\n<tr><td>a</td><td>b</td></tr>\n<tr><td>c</td></tr>\n</table>\n<div>\n```"
    kinds = {i["kind"]: i["line"] for i in check_reply_html(reply)["issues"]}
    assert kinds["row_cell_count"] == 4 and kinds["unclosed_tag"] == 6
    cut = check_reply_html("```html\n<html><body>\n<p>x</p>\n```")
    assert [i["kind"] for i in cut["issues"]] == ["missing_end_html"]


async def test_m1_input_is_capped_at_5mb_and_the_cut_is_noted():
    chunk = b"<p>" + b"x" * 1000 + b"</p>"
    data = chunk * (6 * MB // len(chunk))
    text, meta = docs._extract_html(data)
    assert meta["html_truncated"] is True and meta["html_bytes"] == len(data)
    assert meta["html_bytes_read"] <= docs.MAX_HTML_EXTRACT_BYTES
    assert len(text) < len(data)
    # at or under the cap: no note
    assert docs._extract_html(b"<p>hi</p>")[1] == {}


async def test_m1_cut_lands_on_a_character_boundary():
    data = b"<p>" + "é".encode() * (docs.MAX_HTML_EXTRACT_BYTES // 2 + 10) + b"</p>"
    text, meta = docs._extract_html(data)
    assert meta["html_truncated"] and "�" not in text and "Ã" not in text


async def test_m1_a_timed_out_document_is_not_extracted_again(monkeypatch):
    async def _boom(*a, **k):
        raise AssertionError("extraction must not be retried after a timeout")

    monkeypatch.setattr(cs, "extract_bounded", _boom)
    doc = Document(
        project_id=uuid.uuid4(), filename="x.pdf", content_type="application/pdf", byte_size=1,
        storage_path="x", sha256="s", extracted_text="", meta={"error": "text extraction timed out after 30s"},
    )
    await cs._retry_failed_extraction(doc)  # returns before touching storage


async def test_m1_other_failures_are_still_retried(monkeypatch, tmp_path):
    calls = []

    async def _extract(filename, data, **k):
        calls.append(filename)
        return "recovered", {}, "done"

    async def _bytes(doc, settings=None):
        return b"x"

    import tret.storage as storage

    monkeypatch.setattr(cs, "extract_bounded", _extract)
    monkeypatch.setattr(storage, "document_bytes", _bytes)
    doc = Document(
        project_id=uuid.uuid4(), filename="x.html", content_type="text/html", byte_size=1,
        storage_path="x", sha256="s", extracted_text="", meta={"error": "unsupported type"},
    )
    await cs._retry_failed_extraction(doc)
    assert calls == ["x.html"] and doc.extracted_text == "recovered"


async def test_m1_zip_member_reads_are_bounded_by_the_extraction_budget(monkeypatch):
    def _slow(archive, data, member):
        time.sleep(1.0)
        return "late", {}

    monkeypatch.setattr(docs, "read_zip_member", _slow)
    with pytest.raises(ValueError, match="timed out"):
        await docs.read_zip_member_bounded("a.zip", b"", "m.txt", budget_seconds=0.1)

    monkeypatch.setattr(docs, "read_zip_member", lambda a, d, m: ("fast", {}))
    assert (await docs.read_zip_member_bounded("a.zip", b"", "m.txt"))[0] == "fast"
