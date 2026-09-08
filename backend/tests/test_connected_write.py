"""Write-back to SharePoint: `propose_connected_write` (engine/tools.py).

`propose_connected_write` never touches Microsoft Graph — it validates what it
can validate now (target exists, filename is safe, inline content fits, a
claimed deliverable actually has a drafted section) and records a
`connected_write` Finding with status `draft`, exactly like `record_finding`/
`draft_section`. The actual upload is a separate concern
(`api/findings.py::decide_finding`'s approval side effect), covered in
`test_findings_write.py`.

As in `test_connected_tools.py`, nothing here hits the network: every
`tret.services.connections` entry point the tool calls is monkeypatched on
`tools_module.connections_service` — the exact module object `engine/tools.py`
imported — so patching it there reaches every call the tool makes without
needing the real service module to have landed its side of the contract yet.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.db.models import Base, Finding, Project, Workspace
from tret.engine import tools as tools_module
from tret.engine.tools import (
    MAX_CONNECTED_WRITE_CONTENT_BYTES,
    RunContext,
    ToolError,
    WRITE_CONNECTOR_TOOL_NAMES,
    execute_tool,
    get_builtin_tools,
    propose_connected_write,
    withheld_connector_tools,
)


# ── a fake of the services/connections.py write contract ────────────────────
class FakeConnectionUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FakeConnection:
    write_scopes: bool = True


@dataclass(frozen=True)
class FakeWriteTarget:
    slug: str
    label: str
    path: str
    site_id: str | None = None
    drive_id: str | None = None
    item_id: str | None = None
    web_url: str | None = None


# ── a disposable sqlite tret ─────────────────────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'connected_write.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import tret.db.engine as db_engine

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
async def ctx(db):
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
        workspace_id=project.workspace_id,
    )


TARGET = FakeWriteTarget(slug="site-finance", label="Finance", path="/Documents/tret")


@pytest.fixture()
def targets(monkeypatch):
    """`list_write_targets` returning one target, remembering call kwargs."""
    calls: list[dict] = []

    async def _list(db, workspace_id):
        calls.append({"workspace_id": workspace_id})
        return [TARGET]

    monkeypatch.setattr(tools_module.connections_service, "list_write_targets", _list, raising=False)
    monkeypatch.setattr(
        tools_module.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    return calls


@pytest.fixture()
def safe_filename(monkeypatch):
    """`safe_upload_filename` — a permissive real-shaped fake: refuses a path
    separator or empty name, otherwise returns the name unchanged."""

    def _safe(name: str) -> str:
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            raise ValueError(f"'{name}' is not a safe filename")
        return name

    monkeypatch.setattr(tools_module.connections_service, "safe_upload_filename", _safe, raising=False)
    return _safe


@pytest.fixture()
def no_graph_calls(monkeypatch):
    """Fails the test if propose_connected_write ever calls the upload — it
    must never touch Graph on its own."""

    async def _boom(*args, **kwargs):
        raise AssertionError("propose_connected_write must never call upload_connected_file")

    monkeypatch.setattr(tools_module.connections_service, "upload_connected_file", _boom, raising=False)


async def _draft_section(db, project_id, run_id, deliverable: str, section: str = "governance") -> Finding:
    finding = Finding(
        run_id=run_id,
        project_id=project_id,
        schema_slug="draft_section",
        subject={"deliverable": deliverable, "section": section},
        payload={"markdown": "# Governance\n\nSome drafted prose long enough to pass minLength."},
        provenance={},
        status="draft",
    )
    db.add(finding)
    await db.flush()
    return finding


# ── the tool is registered and classified ────────────────────────────────────
def test_propose_connected_write_is_registered():
    assert "propose_connected_write" in get_builtin_tools()
    assert WRITE_CONNECTOR_TOOL_NAMES == {"propose_connected_write"}


# ── the happy paths ───────────────────────────────────────────────────────────
async def test_propose_with_inline_content_creates_the_exact_payload_shape(
    ctx, targets, safe_filename, no_graph_calls
):
    result = await propose_connected_write(
        ctx, target="site-finance", filename="notes.md", content="hello world"
    )

    finding = (await ctx.db.execute(select(Finding))).scalars().one()
    assert finding.schema_slug == "connected_write"
    assert finding.status == "draft"
    assert finding.subject == {"target": "site-finance", "filename": "notes.md"}
    assert finding.payload == {
        "target_slug": "site-finance",
        "target_label": "Finance",
        "target_path": "/Documents/tret",
        "filename": "notes.md",
        "content_type": "text/markdown",
        "source": {"kind": "inline", "content": "hello world"},
        "size": len(b"hello world"),
        "content_sha256": finding.payload["content_sha256"],  # checked below
        "upload": None,
    }
    import hashlib

    assert finding.payload["content_sha256"] == hashlib.sha256(b"hello world").hexdigest()
    assert finding.id in ctx.findings_created

    assert "Proposed notes.md to Finance/tret" in result
    assert "awaiting approval" in result
    assert f"finding {finding.id}" in result
    assert "Nothing has been written to SharePoint." in result


@pytest.mark.parametrize(
    "extension,expected_type",
    [
        ("md", "text/markdown"),
        ("html", "text/html"),
        ("txt", "text/plain"),
        ("json", "application/json"),
        ("csv", "text/csv"),
        ("xyz", "application/octet-stream"),
    ],
)
async def test_inline_content_type_is_derived_from_the_extension(
    ctx, targets, safe_filename, no_graph_calls, extension, expected_type
):
    await propose_connected_write(
        ctx, target="site-finance", filename=f"file.{extension}", content="x"
    )
    finding = (await ctx.db.execute(select(Finding))).scalars().one()
    assert finding.payload["content_type"] == expected_type


async def test_propose_with_deliverable_renders_at_approval_not_now(
    ctx, targets, safe_filename, no_graph_calls
):
    await _draft_section(ctx.db, ctx.project_id, ctx.run_id, "tcfd-report")
    await ctx.db.commit()

    result = await propose_connected_write(
        ctx, target="site-finance", filename="report.txt", deliverable="tcfd-report", format="html"
    )

    finding = (await ctx.db.execute(select(Finding).where(Finding.schema_slug == "connected_write"))).scalars().one()
    assert finding.payload["source"] == {"kind": "deliverable", "slug": "tcfd-report", "format": "html"}
    # The extension is forced to match the render format, even though the
    # model asked for "report.txt".
    assert finding.payload["filename"] == "report.txt".replace(".txt", ".html")
    assert finding.subject["filename"] == "report.html"
    assert finding.payload["content_type"] == "text/html"
    assert finding.payload["size"] is None
    assert finding.payload["content_sha256"] is None
    assert finding.payload["upload"] is None
    assert "Proposed report.html to Finance/tret" in result


async def test_propose_defaults_to_markdown_format_for_a_deliverable(
    ctx, targets, safe_filename, no_graph_calls
):
    await _draft_section(ctx.db, ctx.project_id, ctx.run_id, "tcfd-report")
    await ctx.db.commit()
    await propose_connected_write(ctx, target="site-finance", filename="report", deliverable="tcfd-report")
    finding = (
        await ctx.db.execute(select(Finding).where(Finding.schema_slug == "connected_write"))
    ).scalars().one()
    assert finding.payload["source"]["format"] == "markdown"
    assert finding.payload["filename"] == "report.md"
    assert finding.payload["content_type"] == "text/markdown"


# ── refusals ──────────────────────────────────────────────────────────────────
async def test_unknown_target_lists_the_allowed_slugs(ctx, targets, safe_filename, no_graph_calls):
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(ctx, target="not-a-target", filename="x.md", content="hi")
    assert "Unknown write target 'not-a-target'" in str(excinfo.value)
    assert "site-finance" in str(excinfo.value)
    assert not (await ctx.db.execute(select(Finding))).scalars().all()


async def test_a_bad_filename_is_refused(ctx, targets, safe_filename, no_graph_calls):
    with pytest.raises(ToolError):
        await propose_connected_write(ctx, target="site-finance", filename="../etc/passwd", content="hi")
    assert not (await ctx.db.execute(select(Finding))).scalars().all()


async def test_both_content_and_deliverable_is_refused(ctx, targets, safe_filename, no_graph_calls):
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(
            ctx, target="site-finance", filename="x.md", content="hi", deliverable="tcfd-report"
        )
    assert "exactly one" in str(excinfo.value).lower()
    assert not (await ctx.db.execute(select(Finding))).scalars().all()


async def test_neither_content_nor_deliverable_is_refused(ctx, targets, safe_filename, no_graph_calls):
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(ctx, target="site-finance", filename="x.md")
    assert "exactly one" in str(excinfo.value).lower()


async def test_oversize_inline_content_tells_the_model_to_use_a_deliverable(
    ctx, targets, safe_filename, no_graph_calls
):
    too_big = "x" * (MAX_CONNECTED_WRITE_CONTENT_BYTES + 1)
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(ctx, target="site-finance", filename="x.md", content=too_big)
    assert "deliverable" in str(excinfo.value).lower()
    assert not (await ctx.db.execute(select(Finding))).scalars().all()


async def test_a_missing_deliverable_is_refused(ctx, targets, safe_filename, no_graph_calls):
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(
            ctx, target="site-finance", filename="report.md", deliverable="does-not-exist"
        )
    assert "does-not-exist" in str(excinfo.value)
    assert "draft_section" in str(excinfo.value)
    assert not (await ctx.db.execute(select(Finding))).scalars().all()


async def test_an_unusable_connection_is_a_tool_error(ctx, safe_filename, no_graph_calls, monkeypatch):
    async def _unavailable(db, workspace_id):
        raise FakeConnectionUnavailable("No Microsoft 365 connection is active for this workspace.")

    monkeypatch.setattr(tools_module.connections_service, "list_write_targets", _unavailable, raising=False)
    monkeypatch.setattr(
        tools_module.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(ctx, target="site-finance", filename="x.md", content="hi")
    assert "no microsoft 365 connection" in str(excinfo.value).lower()


async def test_a_run_with_no_workspace_cannot_propose(db, no_graph_calls):
    project = (await db.execute(select(Project))).scalars().one()
    ctx = RunContext(
        db=db,
        run_id=uuid.uuid4(),
        project_id=project.id,
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
    )
    with pytest.raises(ToolError) as excinfo:
        await propose_connected_write(ctx, target="site-finance", filename="x.md", content="hi")
    assert "workspace" in str(excinfo.value).lower()


# ── availability: withheld separately from the read trio ────────────────────
@pytest.fixture()
def usable_connection(monkeypatch):
    calls = {"n": 0}
    conn = FakeConnection(write_scopes=True)

    async def _ensure(db, workspace_id, provider="m365"):
        calls["n"] += 1
        return conn

    monkeypatch.setattr(tools_module.connections_service, "ensure_connection_usable", _ensure, raising=False)
    monkeypatch.setattr(
        tools_module.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    return calls


async def test_withheld_when_the_connection_lacks_write_scopes(db, usable_connection, monkeypatch):
    project = (await db.execute(select(Project))).scalars().one()

    async def _ensure_no_write(db_, workspace_id, provider="m365"):
        return FakeConnection(write_scopes=False)

    monkeypatch.setattr(
        tools_module.connections_service, "ensure_connection_usable", _ensure_no_write, raising=False
    )
    monkeypatch.setattr(
        tools_module.connections_service,
        "connection_has_write_scopes",
        lambda c: c.write_scopes,
        raising=False,
    )

    withheld, reason = await withheld_connector_tools(
        db, project.workspace_id, ["list_connected_sources", "propose_connected_write"]
    )
    assert withheld == {"propose_connected_write"}
    assert "write" in reason.lower()


async def test_withheld_when_there_are_no_write_targets(db, usable_connection, monkeypatch):
    project = (await db.execute(select(Project))).scalars().one()
    monkeypatch.setattr(
        tools_module.connections_service, "connection_has_write_scopes", lambda c: True, raising=False
    )

    async def _no_targets(db_, workspace_id):
        return []

    monkeypatch.setattr(tools_module.connections_service, "list_write_targets", _no_targets, raising=False)

    withheld, reason = await withheld_connector_tools(
        db, project.workspace_id, ["search_connected_files", "propose_connected_write"]
    )
    assert withheld == {"propose_connected_write"}
    assert "no write target" in reason.lower()


async def test_read_tools_stay_available_when_only_the_write_tool_is_withheld(
    db, usable_connection, monkeypatch
):
    project = (await db.execute(select(Project))).scalars().one()
    monkeypatch.setattr(
        tools_module.connections_service, "connection_has_write_scopes", lambda c: False, raising=False
    )

    withheld, _ = await withheld_connector_tools(
        db,
        project.workspace_id,
        ["list_connected_sources", "search_connected_files", "read_connected_file", "propose_connected_write"],
    )
    assert withheld == {"propose_connected_write"}


async def test_nothing_withheld_when_write_scopes_and_targets_are_both_present(
    db, usable_connection, monkeypatch, targets
):
    project = (await db.execute(select(Project))).scalars().one()
    monkeypatch.setattr(
        tools_module.connections_service, "connection_has_write_scopes", lambda c: True, raising=False
    )
    withheld, reason = await withheld_connector_tools(
        db, project.workspace_id, ["list_connected_sources", "propose_connected_write"]
    )
    assert withheld == set()
    assert reason is None


async def test_both_read_and_write_are_withheld_when_the_connection_itself_is_unusable(db, monkeypatch):
    project = (await db.execute(select(Project))).scalars().one()

    async def _fail(db_, workspace_id, provider="m365"):
        raise FakeConnectionUnavailable("No Microsoft 365 connection is active for this workspace.")

    monkeypatch.setattr(tools_module.connections_service, "ensure_connection_usable", _fail, raising=False)
    monkeypatch.setattr(
        tools_module.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )

    withheld, reason = await withheld_connector_tools(
        db, project.workspace_id, ["list_connected_sources", "propose_connected_write"]
    )

    assert withheld == {"list_connected_sources", "propose_connected_write"}
    assert "no microsoft 365 connection" in reason.lower()


# ── the withheld tool is still a well-formed ToolError when force-called ────
async def test_execute_tool_wraps_an_unavailable_write_target_lookup(ctx, no_graph_calls, monkeypatch):
    async def _unavailable(db, workspace_id):
        raise FakeConnectionUnavailable("Connections are not included in this workspace's plan.")

    monkeypatch.setattr(tools_module.connections_service, "list_write_targets", _unavailable, raising=False)
    monkeypatch.setattr(
        tools_module.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    spec = get_builtin_tools()["propose_connected_write"]
    result, is_error = await execute_tool(
        ctx, spec, {"target": "site-finance", "filename": "x.md", "content": "hi"}
    )
    assert is_error is True
    assert "not included in this workspace's plan" in result
