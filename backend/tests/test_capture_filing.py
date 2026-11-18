"""`capture_filing` (engine/filing_tools.py): the ESEF filing fallback's collection tool.

`fetch_page` is stubbed on the tool's module; nothing opens a socket.
"""
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from sqlalchemy import select

from test_web_tools import _no_leaked_overrides, ctx, db, research  # noqa: F401
from tests.test_esef_filing import REPORT, PKG, make_esef_zip, make_paged_report
from tret.db.models import Collection, CollectionTarget, Document, Harness, Run
from tret.engine import filing_tools
from tret.engine import tools as tools_module
from tret.engine.filing_tools import capture_filing
from tret.engine.tools import ToolError, get_builtin_tools, withheld_web_tools
from tret.net.fetch import FetchedPage, FetchError
from tret.net.policy import MODE_OFF, MODE_ON, filing_policy
from tret.services.workspace import COLLECTION_HARNESS_TOOLS

from tests.evals.golden_world import install_sqlite_type_shims

install_sqlite_type_shims()

LEI = "529900T8BM49AURSDO55"


@pytest.fixture()
async def target(ctx):
    db = ctx.db
    from tret.db.models import Project

    project = (await db.execute(select(Project))).scalars().one()
    ctx.workspace_id = project.workspace_id
    h = Harness(id=uuid.uuid4(), workspace_id=project.workspace_id, name="H")
    db.add(h)
    await db.flush()
    c = Collection(id=uuid.uuid4(), workspace_id=project.workspace_id, project_id=project.id, name="C",
                   run_slug="r", status="running", settings={}, warnings=[])
    db.add(c)
    await db.flush()
    run = Run(id=ctx.run_id, project_id=project.id, harness_id=h.id, task_type="freeform",
              task_input={}, document_ids=[])
    db.add(run)
    await db.flush()
    t = CollectionTarget(id=uuid.uuid4(), collection_id=c.id, position=1, slug="acme", company="Acme",
                         market="EU", fiscal_year=2025, run_id=run.id, known_urls=[], finding_ids=[],
                         written_files=[], identifiers={"lei": LEI, "source_filing_url": REPORT})
    db.add(t)
    run.task_input = {"_collection_target_id": str(t.id), "_collection_id": str(c.id)}
    await db.commit()
    return t, run


REPORT_PATH = urlsplit(REPORT).path
API = "https://filings.xbrl.org/api/filings"
OTHER_PKG_PATH = "/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme-annual-report.zip"
OTHER_PKG = "https://filings.xbrl.org" + OTHER_PKG_PATH


def api_page(package_path=None, *, url=API, redirects=(), entries=None):
    """A filings-API answer: one filing carrying `package_path`, `entries` (attribute dicts), or no match."""
    if entries is None:
        entries = [{"package_url": package_path}] if package_path else []
    body = json.dumps({"data": [{"type": "filing", "attributes": e} for e in entries]}).encode()
    return FetchedPage(url=url, requested_url=url, status_code=200, content_type="application/vnd.api+json",
                       extension=".json", body=body, sha256=hashlib.sha256(body).hexdigest(),
                       duration_ms=1, redirects=redirects, truncated=False)


@pytest.fixture()
def serve(monkeypatch):
    calls = []
    api_calls = []
    api_script = []  # one entry per API request, in order: a FetchedPage or an exception; empty = fails
    fail = {}  # package URL -> exception to raise instead of serving the zip

    def _install(body=None, *, url=PKG, extension=".zip", redirects=()):
        body = body if body is not None else make_esef_zip(make_paged_report(6))
        page = FetchedPage(url=url, requested_url=PKG, status_code=200, content_type="application/zip",
                           extension=extension, body=body, sha256=hashlib.sha256(body).hexdigest(),
                           duration_ms=3, redirects=redirects, truncated=False,
                           headers=(("etag", '"x"'),))

        async def fake(target_url, **kw):
            if target_url.startswith(API + "?"):
                api_calls.append({"url": target_url, **kw})
                item = api_script.pop(0) if api_script else FetchError("filings API unavailable")
                if isinstance(item, Exception):
                    raise item
                return item
            calls.append({"url": target_url, **kw})
            if target_url in fail:
                raise fail[target_url]
            return page

        monkeypatch.setattr(filing_tools, "fetch_page", fake)
        return page

    _install.calls = calls
    _install.api_calls = api_calls
    _install.api = api_script
    _install.fail = fail
    return _install


def test_registered_withheld_and_on_the_collector():
    assert "capture_filing" in get_builtin_tools()
    assert "capture_filing" in COLLECTION_HARNESS_TOOLS
    assert "capture_filing" in tools_module.WEB_TOOL_NAMES
    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(tools_module, "_research_mode", lambda: MODE_OFF)
    try:
        assert withheld_web_tools(["capture_filing"]) == ["capture_filing"]
    finally:
        mp.undo()


def test_policy_is_pinned_to_the_filings_host():
    pol = filing_policy()
    assert pol.allow_hosts == frozenset({"filings.xbrl.org"})
    assert pol.max_bytes == 200 * 1024 * 1024 and pol.allow_http is False


async def test_refuses_outside_a_batch_run(ctx, research, serve):
    research(MODE_ON)
    with pytest.raises(ToolError, match="not part of a collection"):
        await capture_filing(ctx)


async def test_refuses_without_identifiers(ctx, research, serve, target):
    research(MODE_ON)
    t, _ = target
    t.identifiers = {"lei": LEI}
    await ctx.db.commit()
    with pytest.raises(ToolError, match="no source_filing_url"):
        await capture_filing(ctx)
    t.identifiers = {"source_filing_url": REPORT}
    await ctx.db.commit()
    with pytest.raises(ToolError, match="no LEI"):
        await capture_filing(ctx)
    assert serve.calls == []


async def test_refuses_a_wrong_host_and_a_wrong_lei(ctx, research, serve, target):
    research(MODE_ON)
    t, _ = target
    t.identifiers = {"lei": LEI, "source_filing_url": REPORT.replace("filings.xbrl.org", "evil.example")}
    await ctx.db.commit()
    with pytest.raises(ToolError, match="cannot be used"):
        await capture_filing(ctx)
    t.identifiers = {"lei": "5299000000000000000X", "source_filing_url": REPORT}
    await ctx.db.commit()
    with pytest.raises(ToolError, match="different LEI"):
        await capture_filing(ctx)
    assert serve.calls == []


async def test_a_redirect_off_host_is_refused(ctx, research, serve, target):
    research(MODE_ON)
    serve(url="https://cdn.example.com/x.zip", redirects=(PKG,))
    with pytest.raises(ToolError, match="cdn.example.com"):
        await capture_filing(ctx)
    assert (await ctx.db.execute(select(Document))).scalars().all() == []


async def test_stores_the_zip_with_meta_and_text_and_counts_a_capture(ctx, research, serve, target):
    research(MODE_ON)
    page = serve()
    out = await capture_filing(ctx, note="  fallback  ")
    call = serve.calls[0]
    assert call["url"] == PKG and call["capture"] and call["fail_on_truncate"]
    assert call["max_bytes"] == 200 * 1024 * 1024 and call["policy"].allow_hosts == frozenset({"filings.xbrl.org"})
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.sha256 == page.sha256 and doc.filename.endswith(".zip") and doc.id in ctx.document_ids
    assert doc.extraction_status == "done" and doc.extracted_text.startswith("## Page 1\n")
    assert "## Page 6\n" in doc.extracted_text and "SECRET-FACT" not in doc.extracted_text
    m = doc.meta
    assert m["provenance"] == "esef_filing" and m["lei"] == LEI and m["filing_url"] == REPORT
    assert m["package_url"] == PKG and m["report_member"] == "acme-2025/reports/acme-2025.xhtml"
    assert m["report_member_sha256"] == hashlib.sha256(make_paged_report(6)).hexdigest()
    assert m["layout"] == "paged" and m["unit"] == "filing_page" and m["unit_count"] == 6
    assert m["folios"] == {str(n): n for n in range(1, 7)} and m["text_truncated"] is False
    assert m["operator_note"] == "fallback"
    assert ctx.web_captures == 1 and ctx.web_bytes == len(page.body)
    assert "regulated filing" in out and "not the issuer's own PDF" in out and "6 pages" in out


async def test_the_capture_cap_applies(ctx, research, serve, target, monkeypatch):
    research(MODE_ON)
    serve()
    ctx.web_captures = 40
    with pytest.raises(ToolError, match="per-run limit"):
        await capture_filing(ctx)
    assert serve.calls == []


async def test_zip_bombs_and_bad_archives_store_nothing(ctx, research, serve, target, monkeypatch):
    research(MODE_ON)
    serve()
    monkeypatch.setattr(filing_tools, "MAX_ZIP_UNCOMPRESSED_BYTES", 10)
    with pytest.raises(ToolError, match="expands to"):
        await capture_filing(ctx)
    serve(b"PK\x03\x04 not really a zip")
    monkeypatch.setattr(filing_tools, "MAX_ZIP_UNCOMPRESSED_BYTES", 200 * 1024 * 1024)
    with pytest.raises(ToolError, match="not a readable ZIP"):
        await capture_filing(ctx)
    assert (await ctx.db.execute(select(Document))).scalars().all() == []


async def test_off_mode_refuses(ctx, research, target):
    research(MODE_OFF)
    with pytest.raises(ToolError, match="switched off"):
        await capture_filing(ctx)


# ── allowlist narrows, never overrides ─────────────────────────────────────────
def _allow(monkeypatch, hosts: str):
    from tret.config import get_settings

    monkeypatch.setenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS", hosts)
    get_settings.cache_clear()


async def test_a_research_allowlist_without_the_filing_host_refuses(ctx, research, serve, target, monkeypatch):
    research(MODE_ON)
    _allow(monkeypatch, "issuer.example.com")
    try:
        with pytest.raises(ToolError, match="filings.xbrl.org"):
            await capture_filing(ctx)
        assert serve.calls == [] and ctx.web_captures == 0
        from tret.net.policy import egress_status

        assert egress_status()["classes"]["research"]["filing_host"] == {
            "host": "filings.xbrl.org", "permitted": False}
    finally:
        monkeypatch.delenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS")
        from tret.config import get_settings

        get_settings.cache_clear()


async def test_an_allowlist_that_includes_the_host_or_none_works(ctx, research, serve, target, monkeypatch):
    research(MODE_ON)
    _allow(monkeypatch, "filings.xbrl.org")
    serve()
    try:
        assert "regulated filing" in await capture_filing(ctx)
        from tret.net.policy import egress_status

        assert egress_status()["classes"]["research"]["filing_host"]["permitted"] is True
    finally:
        monkeypatch.delenv("TRET_EGRESS_RESEARCH_ALLOW_HOSTS")
        from tret.config import get_settings

        get_settings.cache_clear()


# ── audit and counting, reuse, replay ────────────────────────────────────────────
async def test_a_completed_download_is_audited_even_when_it_is_not_a_zip(ctx, research, serve, target):
    from tret.db.models import EgressCall

    research(MODE_ON)
    page = serve(b"<html>login</html>", extension=".html")
    with pytest.raises(ToolError, match="did not return an archive"):
        await capture_filing(ctx)
    rows = [r for r in (await ctx.db.execute(select(EgressCall))).scalars().all() if r.path != "/api/filings"]
    assert [r.decision for r in rows] == ["allowed"] and rows[0].byte_count == len(page.body)
    assert ctx.web_bytes == len(page.body)


async def test_the_same_package_is_reused_not_stored_twice(ctx, research, serve, target):
    research(MODE_ON)
    serve()
    first = await capture_filing(ctx)
    second = await capture_filing(ctx)
    docs = (await ctx.db.execute(select(Document))).scalars().all()
    assert len(docs) == 1 and str(docs[0].id) in second and "ALREADY COLLECTED" in second
    assert "ALREADY" not in first and ctx.document_ids == [docs[0].id]


async def test_replay_mode_serves_the_stored_capture_and_refuses_without_one(ctx, research, serve, target):
    from tret.net.policy import MODE_REPLAY

    research(MODE_ON)
    serve()
    await capture_filing(ctx)
    serve.calls.clear()
    ctx.document_ids.clear()
    research(MODE_REPLAY)
    out = await capture_filing(ctx)
    assert "REPLAYED" in out and serve.calls == [] and len(ctx.document_ids) == 1
    docs = (await ctx.db.execute(select(Document))).scalars().all()
    for d in docs:
        await ctx.db.delete(d)
    await ctx.db.commit()
    with pytest.raises(ToolError, match="replay mode"):
        await capture_filing(ctx)


async def test_a_hostile_report_is_killed_by_the_worker_deadline(ctx, research, serve, target, monkeypatch):
    research(MODE_ON)
    serve()
    monkeypatch.setattr(filing_tools, "ANALYSIS_TIMEOUT_SECONDS", 0.001)
    with pytest.raises(ToolError, match="exceeded"):
        await capture_filing(ctx)
    assert (await ctx.db.execute(select(Document))).scalars().all() == []


# ── fetch_page(sniff_zip=True) ────────────────────────────────────────────────────
def _wire(monkeypatch, handler):
    from contextlib import asynccontextmanager

    import httpx

    from tret.net.fetch import fetch as fetch_module

    @asynccontextmanager
    async def fake_open_client(egress_class, *, timeout=None, headers=None, follow_redirects=False, policy=None):
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), headers=headers, timeout=timeout) as c:
            yield c

    async def allow(url, egress_class, policy=None):
        return None

    monkeypatch.setattr(fetch_module, "open_client", fake_open_client)
    monkeypatch.setattr(fetch_module, "check_url", allow)


async def test_sniff_zip_accepts_octet_stream_with_zip_magic_and_refuses_otherwise(monkeypatch):
    import httpx

    from tret.net.fetch import FetchError, fetch_page

    body = make_esef_zip(make_paged_report(6))
    _wire(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=body))
    page = await fetch_page(PKG, capture=True, sniff_zip=True, max_bytes=10**8, fail_on_truncate=True)
    assert page.extension == ".zip" and page.body == body
    with pytest.raises(FetchError, match="ZIP header"):
        _wire(monkeypatch, lambda req: httpx.Response(
            200, headers={"content-type": "application/octet-stream"}, content=b"<html>not a zip</html>"))
        await fetch_page(PKG, capture=True, sniff_zip=True, max_bytes=10**8, fail_on_truncate=True)
    # without the flag octet-stream is still read as a (non-)PDF
    _wire(monkeypatch, lambda req: httpx.Response(200, headers={"content-type": "application/octet-stream"}, content=body))
    with pytest.raises(FetchError, match="%PDF-"):
        await fetch_page(PKG, capture=True, max_bytes=10**8, fail_on_truncate=True)


# -- package resolution (the register's API) ----------------------------------

PERIOD = "2025-12-31"


def _api_filter(call):
    q = parse_qs(urlsplit(call["url"]).query)
    assert q["page[size]"] == ["10"]
    return json.loads(q["filter"][0])


async def test_lookup_by_lei_and_period_resolves_a_package_named_unlike_the_report_directory(
        ctx, research, serve, target):
    research(MODE_ON)
    serve()
    serve.api.append(api_page(OTHER_PKG_PATH))
    out = await capture_filing(ctx)
    assert len(serve.api_calls) == 1
    call = serve.api_calls[0]
    assert _api_filter(call) == [
        {"name": "period_end", "op": "eq", "val": PERIOD},
        {"name": "entity.identifier", "op": "eq", "val": LEI},
    ]
    assert "report_url" not in call["url"]
    assert call["max_bytes"] == 1024 * 1024 and call["fail_on_truncate"] and not call.get("capture")
    assert call["policy"].allow_hosts == frozenset({"filings.xbrl.org"})
    assert [c["url"] for c in serve.calls] == [OTHER_PKG]
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == OTHER_PKG and doc.meta["package_url_source"] == "lookup"
    assert OTHER_PKG in out and "source: lookup" in out
    # A read, not a capture: the capture counter is the download only.
    assert ctx.web_captures == 1 and ctx.web_fetches == 1
    assert ctx.web_bytes == len(api_page(OTHER_PKG_PATH).body) + len(make_esef_zip(make_paged_report(6)))


def _pkg(name):
    return f"/{LEI}/{PERIOD}/ESEF/DE/0/{name}"


def test_several_filings_prefer_report_url_then_directory_then_errors_then_latest():
    from tret.services import esef_filing as ef

    def body(*entries):
        return api_page(entries=list(entries)).body

    # 1. the entry whose report_url equals ours wins, whatever its errors or date
    got = ef.package_url_from_filings_api(body(
        {"package_url": _pkg("a.zip"), "report_url": _pkg("x/reports/x.xhtml"), "error_count": 0, "processed": "2026-05"},
        {"package_url": _pkg("b.zip"), "report_url": REPORT_PATH, "error_count": 9, "processed": "2020-01"},
    ), REPORT)
    assert got.endswith("/b.zip")
    # 2. else the package in the report's /LEI/period/ESEF/country/n/ directory
    got = ef.package_url_from_filings_api(body(
        {"package_url": f"/{LEI}/{PERIOD}/ESEF/FR/0/a.zip", "error_count": 0},
        {"package_url": _pkg("b.zip"), "error_count": 5},
    ), REPORT)
    assert got.endswith("/DE/0/b.zip")
    # 3. else the lowest error_count, then the latest processed
    got = ef.package_url_from_filings_api(body(
        {"package_url": f"/{LEI}/{PERIOD}/ESEF/FR/0/a.zip", "error_count": 2, "processed": "2026-01"},
        {"package_url": f"/{LEI}/{PERIOD}/ESEF/FR/1/b.zip", "error_count": 0, "processed": "2026-02"},
        {"package_url": f"/{LEI}/{PERIOD}/ESEF/FR/2/c.zip", "error_count": 0, "processed": "2026-03"},
    ), REPORT)
    assert got.endswith("/FR/2/c.zip")
    assert ef.package_url_from_filings_api(body(), REPORT) is None
    assert ef.package_url_from_filings_api(b"not json", REPORT) is None


async def test_lookup_without_a_match_falls_back_to_the_derived_url(ctx, research, serve, target):
    research(MODE_ON)
    serve()
    serve.api.append(api_page(None))
    await capture_filing(ctx)
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == PKG and doc.meta["package_url_source"] == "derived"
    assert len(serve.api_calls) == 1 and ctx.web_fetches == 1


async def test_an_api_403_is_a_failed_lookup_and_is_recorded(ctx, research, serve, target):
    from tret.db.models import EgressCall

    research(MODE_ON)
    serve()
    serve.api.append(FetchError("HTTP 403 from https://filings.xbrl.org/api/filings", status_code=403))
    await capture_filing(ctx)
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == PKG and doc.meta["package_url_source"] == "derived"
    assert len(serve.api_calls) == 1 and [c["url"] for c in serve.calls] == [PKG]
    assert ctx.web_fetches == 0
    rows = [r for r in (await ctx.db.execute(select(EgressCall))).scalars().all() if r.path == "/api/filings"]
    assert [r.decision for r in rows] == ["denied"] and "403" in rows[0].reason


async def test_a_failing_lookup_falls_back_to_the_derived_url(ctx, research, serve, target):
    research(MODE_ON)
    serve()  # no scripted answers: every API request raises FetchError
    await capture_filing(ctx)
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == PKG and doc.meta["package_url_source"] == "derived"
    assert len(serve.api_calls) == 1 and [c["url"] for c in serve.calls] == [PKG]
    assert ctx.web_fetches == 0


async def test_a_derived_404_retries_the_lookup_once(ctx, research, serve, target):
    research(MODE_ON)
    serve()
    serve.api.extend([FetchError("boom"), api_page(OTHER_PKG_PATH)])
    serve.fail[PKG] = FetchError(f"HTTP 404 from {PKG}", status_code=404)
    await capture_filing(ctx)
    assert [c["url"] for c in serve.calls] == [PKG, OTHER_PKG]
    assert len(serve.api_calls) == 2
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == OTHER_PKG and doc.meta["package_url_source"] == "lookup"


async def test_a_derived_404_with_no_lookup_answer_fails_after_one_retry(ctx, research, serve, target):
    research(MODE_ON)
    serve()
    serve.fail[PKG] = FetchError(f"HTTP 404 from {PKG}", status_code=404)
    with pytest.raises(ToolError, match="404"):
        await capture_filing(ctx)
    assert len(serve.api_calls) == 2 and [c["url"] for c in serve.calls] == [PKG]
    assert (await ctx.db.execute(select(Document))).scalars().all() == []


@pytest.mark.parametrize("package", [
    "/5299000000000000000X/2025-12-31/ESEF/DE/0/acme.zip",  # another LEI
    "/529900T8BM49AURSDO55/2024-12-31/ESEF/DE/0/acme.zip",  # another period
    "/529900T8BM49AURSDO55/2025-12-31/xbrl/DE/0/acme.zip",  # not under /ESEF/
    "/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme.txt",  # not a package
    "https://evil.example/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme.zip",  # another host
    "https://filings.xbrl.org.evil.example/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme.zip",
    "//evil.example/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme.zip",
])
async def test_a_looked_up_package_outside_the_filing_is_refused(ctx, research, serve, target, package):
    research(MODE_ON)
    serve()
    serve.api.append(api_page(package))
    with pytest.raises(ToolError, match="refused"):
        await capture_filing(ctx)
    assert serve.calls == []
    assert (await ctx.db.execute(select(Document))).scalars().all() == []


async def test_a_zip_identifier_is_used_directly_without_a_lookup(ctx, research, serve, target):
    research(MODE_ON)
    t, _ = target
    t.identifiers = {"lei": LEI, "source_filing_url": OTHER_PKG}
    await ctx.db.commit()
    serve(url=OTHER_PKG)
    await capture_filing(ctx)
    assert serve.api_calls == [] and [c["url"] for c in serve.calls] == [OTHER_PKG]
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == OTHER_PKG and doc.meta["package_url_source"] == "register"
    assert ctx.web_fetches == 0 and ctx.web_captures == 1


async def test_the_lookup_is_audited_and_a_redirect_off_host_is_not_trusted(ctx, research, serve, target):
    from tret.db.models import EgressCall

    research(MODE_ON)
    serve()
    serve.api.append(api_page(OTHER_PKG_PATH, url="https://cdn.example.com/x", redirects=(API,)))
    await capture_filing(ctx)
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == PKG and doc.meta["package_url_source"] == "derived"
    rows = (await ctx.db.execute(select(EgressCall))).scalars().all()
    assert any(r.host == "cdn.example.com" for r in rows)


async def test_capture_source_refuses_filings_xbrl_org(ctx, research):
    from tret.engine.tools import capture_source

    research(MODE_ON)
    for url in (REPORT, PKG, "https://FILINGS.xbrl.org./x.zip"):
        with pytest.raises(ToolError, match="Use capture_filing for ESEF filings; it records the filing for page mapping."):
            await capture_source(ctx, url=url)
    assert ctx.web_captures == 0


def test_the_description_says_the_tool_resolves_the_package_itself():
    desc = get_builtin_tools()["capture_filing"].description
    assert "resolves the package itself" in desc


XBRI_PATH = f"/{LEI}/{PERIOD}/ESEF/DE/0/{LEI}-{PERIOD}.xbri"
XBRI = "https://filings.xbrl.org" + XBRI_PATH


async def test_an_xbri_package_is_resolved_and_captured(ctx, research, serve, target):
    research(MODE_ON)
    serve(url=XBRI)
    serve.api.append(api_page(XBRI_PATH))
    await capture_filing(ctx)
    assert [c["url"] for c in serve.calls] == [XBRI]
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url"] == XBRI and doc.meta["package_url_source"] == "lookup"
    assert doc.extraction_status == "done"


async def test_a_recorded_xbri_url_is_used_directly(ctx, research, serve, target):
    research(MODE_ON)
    t, _ = target
    t.identifiers = {"lei": LEI, "source_filing_url": XBRI}
    await ctx.db.commit()
    serve(url=XBRI)
    await capture_filing(ctx)
    assert serve.api_calls == [] and [c["url"] for c in serve.calls] == [XBRI]
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.meta["package_url_source"] == "register"


@pytest.mark.parametrize("headers", [
    {},
    {"content-type": "application/octet-stream"},
    {"content-type": "application/vnd.xbrl.report+zip"},
    {"content-type": "application/x-xbrl-report-package"},
])
async def test_sniff_zip_accepts_a_missing_or_package_content_type_with_zip_magic(monkeypatch, headers):
    import httpx

    from tret.net.fetch import fetch_page

    body = make_esef_zip(make_paged_report(6))
    _wire(monkeypatch, lambda req: httpx.Response(200, headers=headers, content=body))
    page = await fetch_page(XBRI, capture=True, sniff_zip=True, max_bytes=10**8, fail_on_truncate=True)
    assert page.extension == ".zip" and page.body == body


@pytest.mark.parametrize("headers", [{}, {"content-type": "application/vnd.xbrl.report+zip"}])
async def test_sniff_zip_refuses_a_non_zip_body_with_no_or_package_content_type(monkeypatch, headers):
    import httpx

    from tret.net.fetch import FetchError, fetch_page

    _wire(monkeypatch, lambda req: httpx.Response(200, headers=headers, content=b"<html>login</html>"))
    with pytest.raises(FetchError, match="ZIP header"):
        await fetch_page(XBRI, capture=True, sniff_zip=True, max_bytes=10**8, fail_on_truncate=True)
    # without the flag a missing type is still unreadable
    _wire(monkeypatch, lambda req: httpx.Response(200, headers=headers, content=make_esef_zip(make_paged_report(6))))
    with pytest.raises(FetchError):
        await fetch_page(XBRI, capture=True, max_bytes=10**8, fail_on_truncate=True)
