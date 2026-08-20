"""The web tools: switchable, capped, and one tier below everything else.

Three properties are load-bearing, and each is a way this feature could quietly
break the product it was added to:

1. **A number from a web page still cannot be cited.** The trust doctrine says
   values enter through `lookup_dataset` and `run_method`. If `fetch_url` had
   made that three doors, every guarantee downstream of it would be softer, and
   nothing in the codebase would say so out loud.
2. **Switching web access off does not break a harness that uses it.** The tools
   stay registered; the run proceeds without them and says so.
3. **A fetched page is a Document**, with its URL, its hash and its fetch time —
   auditable months later, not prose in a transcript.

Nothing here touches the network: `fetch_page` is stubbed, and the search backend
is the null one unless a test says otherwise.
"""
from __future__ import annotations

import uuid

import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from bench.db.models import Base, Document, EgressCall, Project, Workspace
from bench.engine import tools as tools_module
from bench.engine.tools import (
    WEB_TOOL_NAMES,
    RunContext,
    ToolError,
    execute_tool,
    fetch_url,
    get_builtin_tools,
    read_document,
    web_search,
    withheld_web_tools,
)
from bench.engine.validation import validate_cited_values
from bench.net import policy
from bench.net.fetch import SOURCE_KIND_WEB, FetchedPage
from bench.net.policy import CLASS_RESEARCH, MODE_OFF, MODE_ON, MODE_REPLAY

PAGE_HTML = b"<html><head><title>Filing</title></head><body><p>Revenue was 41.2 million.</p></body></html>"


# ── a disposable sqlite bench ────────────────────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'web.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    # `audit.record_durably` opens its own session through the module-level
    # factory — deliberately, so a denial survives the engine's rollback — so the
    # test database has to be the one that factory hands out too. Same seam
    # tests/evals/golden_world.py uses.
    import bench.db.engine as db_engine

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
async def ctx(db, tmp_path, monkeypatch):
    monkeypatch.setenv("BENCH_STORAGE_DIR", str(tmp_path / "storage"))
    from bench.config import get_settings

    get_settings.cache_clear()
    project = (await db.execute(select(Project))).scalars().one()
    yield RunContext(
        db=db,
        run_id=uuid.uuid4(),
        project_id=project.id,
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
    )
    get_settings.cache_clear()


@pytest.fixture()
def research(monkeypatch):
    """Turn the research class on (or to any mode) without touching the env."""

    def _set(mode: str = MODE_ON):
        monkeypatch.setattr(tools_module, "_research_mode", lambda: mode)

    return _set


@pytest.fixture()
def fetches(monkeypatch):
    """Stub the fetcher. Returns the page a test wants, with no socket in sight."""

    def _install(body: bytes = PAGE_HTML, url: str = "https://example.com/filing", **kw):
        import hashlib

        page = FetchedPage(
            url=url,
            requested_url=kw.pop("requested_url", url),
            status_code=200,
            content_type="text/html",
            extension=".html",
            body=body,
            sha256=hashlib.sha256(body).hexdigest(),
            duration_ms=12,
            redirects=kw.pop("redirects", ()),
            truncated=False,
        )

        async def fake_fetch(target: str):
            return page

        monkeypatch.setattr(tools_module, "fetch_page", fake_fetch)
        return page

    return _install


@pytest.fixture(autouse=True)
def _no_leaked_overrides():
    policy.clear_all_runtime_overrides()
    yield
    policy.clear_all_runtime_overrides()


# ── the registry stays complete ───────────────────────────────────────────────
def test_the_web_tools_are_always_registered():
    """Availability is a switch; existence is not. A harness that lists
    `web_search` on an offline deployment must not fail as `unknown_tool`."""
    builtins = get_builtin_tools()
    for name in WEB_TOOL_NAMES:
        assert name in builtins


def test_web_tools_are_withheld_when_research_is_off(monkeypatch):
    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_OFF)
    enabled = ["read_document", "web_search", "fetch_url"]
    assert withheld_web_tools(enabled) == ["web_search", "fetch_url"]


def test_nothing_is_withheld_when_research_is_on(monkeypatch):
    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_ON)
    assert withheld_web_tools(["web_search", "read_document"]) == []


async def test_calling_a_web_tool_while_off_names_the_setting(ctx, research):
    research(MODE_OFF)
    with pytest.raises(ToolError) as excinfo:
        await web_search(ctx, query="anything")
    assert "BENCH_EGRESS_RESEARCH" in str(excinfo.value)


async def test_web_search_without_a_backend_says_which_variable_to_set(ctx, research):
    research(MODE_ON)
    with pytest.raises(ToolError) as excinfo:
        await web_search(ctx, query="anything")
    assert "BENCH_SEARCH_PROVIDER" in str(excinfo.value)


# ── a fetched page is a document ─────────────────────────────────────────────
async def test_fetch_url_records_a_web_document_and_attaches_it(ctx, research, fetches):
    research(MODE_ON)
    page = fetches()
    result = await fetch_url(ctx, url=page.url)

    doc = (await ctx.db.execute(select(Document))).scalars().one()
    assert doc.source_kind == SOURCE_KIND_WEB
    assert doc.sha256 == page.sha256
    assert doc.meta["url"] == page.url
    assert doc.meta["http_status"] == 200
    assert doc.meta["fetched_by_run"] == str(ctx.run_id)
    assert doc.uploaded_by is None  # nobody put this here
    # Attached, so read_document can page through it like any other document.
    assert doc.id in ctx.document_ids
    assert str(doc.id) in result
    assert "Revenue was 41.2 million." in result


async def test_the_snapshot_bytes_are_written_to_storage(ctx, research, fetches):
    research(MODE_ON)
    fetches()
    await fetch_url(ctx, url="https://example.com/filing")
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    from pathlib import Path

    assert Path(doc.storage_path).read_bytes() == PAGE_HTML


async def test_every_fetch_result_carries_the_unverified_notice(ctx, research, fetches):
    research(MODE_ON)
    fetches()
    result = await fetch_url(ctx, url="https://example.com/filing")
    assert "UNVERIFIED WEB SOURCE" in result


async def test_reading_a_web_document_later_still_says_it_is_unverified(ctx, research, fetches):
    """The tier travels with the text. By iteration nine the fetch_url result
    that explained where this came from is far up the transcript."""
    research(MODE_ON)
    fetches()
    await fetch_url(ctx, url="https://example.com/filing")
    doc = (await ctx.db.execute(select(Document))).scalars().one()
    text = await read_document(ctx, document_id=str(doc.id))
    assert "UNVERIFIED WEB SOURCE" in text
    assert "https://example.com/filing" in text


async def test_a_javascript_only_page_reads_as_unread_not_as_empty(ctx, research, fetches):
    research(MODE_ON)
    fetches(body=b"<html><body><script>render()</script></body></html>")
    result = await fetch_url(ctx, url="https://example.com/app")
    assert "unread rather than as empty" in result


# ── the evidence tier ────────────────────────────────────────────────────────
async def test_a_number_from_a_web_page_still_cannot_be_cited(ctx, research, fetches):
    """The property the whole design is arranged around.

    The page says revenue was 41.2 million. The model read it. It still may not
    put 41.2 in cited_values, because `fetch_url` registers nothing in
    ctx.retrieved_values — so the existing cross-check refuses it without having
    been taught anything about the web.
    """
    research(MODE_ON)
    fetches()
    await fetch_url(ctx, url="https://example.com/filing")

    assert ctx.retrieved_values == []
    errors = validate_cited_values(
        [{"dataset": "example.com", "row_ref": "web:1", "value": "41.2"}],
        ctx.retrieved_values,
    )
    assert errors, "a value seen only on a web page must fail the cited-values check"


# ── budgets and replay ───────────────────────────────────────────────────────
async def test_the_per_run_fetch_budget_is_enforced(ctx, research, fetches, monkeypatch):
    research(MODE_ON)
    fetches()
    monkeypatch.setenv("BENCH_EGRESS_RESEARCH_MAX_FETCHES_PER_RUN", "1")
    from bench.config import get_settings

    get_settings.cache_clear()
    await fetch_url(ctx, url="https://example.com/filing")
    with pytest.raises(ToolError) as excinfo:
        await fetch_url(ctx, url="https://example.com/filing")
    assert "per-run limit" in str(excinfo.value)
    get_settings.cache_clear()


async def test_replay_reads_a_snapshot_and_makes_no_request(ctx, research, fetches, monkeypatch):
    research(MODE_ON)
    fetches()
    await fetch_url(ctx, url="https://example.com/filing")

    async def explode(url):
        raise AssertionError("replay must not fetch")

    monkeypatch.setattr(tools_module, "fetch_page", explode)
    research(MODE_REPLAY)
    ctx.document_ids.clear()
    result = await fetch_url(ctx, url="https://example.com/filing")
    assert "REPLAYED" in result
    assert len(ctx.document_ids) == 1


async def test_replay_refuses_a_url_nobody_snapshotted(ctx, research):
    research(MODE_REPLAY)
    with pytest.raises(ToolError) as excinfo:
        await fetch_url(ctx, url="https://example.com/never-seen")
    assert "replay mode" in str(excinfo.value)


async def test_search_is_unavailable_in_replay_mode(ctx, research):
    research(MODE_REPLAY)
    with pytest.raises(ToolError) as excinfo:
        await web_search(ctx, query="anything")
    assert "replay mode" in str(excinfo.value)


# ── the audit trail ──────────────────────────────────────────────────────────
async def test_a_fetch_writes_an_audit_row_without_the_query_string(ctx, research, fetches):
    research(MODE_ON)
    fetches(url="https://example.com/filing?token=secret")
    await fetch_url(ctx, url="https://example.com/filing?token=secret")
    call = (await ctx.db.execute(select(EgressCall))).scalars().one()
    assert call.decision == "allowed"
    assert call.host == "example.com"
    assert call.path == "/filing"
    assert "secret" not in (call.path or "")
    assert call.run_id == ctx.run_id
    assert call.byte_count == len(PAGE_HTML)


async def test_a_refused_fetch_is_recorded_too(ctx, research, monkeypatch):
    """A denial is the most interesting row in this table."""
    research(MODE_ON)
    from bench.net.policy import EgressDenied

    async def refuse(url):
        raise EgressDenied("private_address", url, CLASS_RESEARCH, "resolves to 127.0.0.1")

    monkeypatch.setattr(tools_module, "fetch_page", refuse)
    with pytest.raises(ToolError):
        await fetch_url(ctx, url="https://sneaky.example.com/")

    # The engine rolls a failed tool call's writes out of the session, so the
    # rollback is part of the scenario, not an artefact of the test: an audit
    # trail a refused request can erase is not an audit trail.
    await ctx.db.rollback()
    call = (await ctx.db.execute(select(EgressCall))).scalars().one()
    assert call.decision == "denied"
    assert call.reason == "private_address"
    assert call.host == "sneaky.example.com"


async def test_a_denied_fetch_is_a_tool_error_not_a_dead_run(ctx, research, monkeypatch):
    """The model gets told and can adapt, the same as any other tool error."""
    research(MODE_ON)
    from bench.net.policy import EgressDenied

    async def refuse(url):
        raise EgressDenied("host_not_allowed", url, CLASS_RESEARCH, "not in the allowlist")

    monkeypatch.setattr(tools_module, "fetch_page", refuse)
    spec = get_builtin_tools()["fetch_url"]
    result, is_error = await execute_tool(ctx, spec, {"url": "https://blocked.example.com/"})
    assert is_error is True
    assert "host_not_allowed" in result
