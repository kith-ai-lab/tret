"""Export styles and formats wired through the API and the connected-write path.

`render_deliverable_bytes` itself is covered by `test_deliverable_export.py` and
`test_export_themes.py`; here it is monkeypatched so these tests pin only the
wiring: which kwargs the route passes, which theme a workspace defaults to, how
a bad value surfaces (422, never a 500), what headers each format is served
with, and that a deliverable-sourced `connected_write` carries its theme from
the proposal through to the render at approval time.
"""
from __future__ import annotations

import types
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tret.api import findings as findings_api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Base, Finding, Project, Workspace
from tret.engine import tools as tools_module
from tret.engine.tools import RunContext, ToolError, propose_connected_write
from tret.services import export as export_module
from tret.services.export_themes import ExportOptionError, RendererUnavailable

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
CONTENT_TYPES = {
    "markdown": "text/markdown",
    "html": "text/html",
    "pdf": "application/pdf",
    "docx": DOCX,
    "pptx": PPTX,
}
PROJECT_ID = uuid.uuid4()


# ── the export route ─────────────────────────────────────────────────────────
@pytest.fixture()
def calls(monkeypatch):
    """Replace the renderer with one that records its kwargs and, like the real
    one, raises ValueError for an unknown theme/accent."""
    recorded: list[dict] = []

    async def _render(db, project_id, slug, format, include_draft=False, **kwargs):
        if kwargs.get("theme") not in (None, "plain", "report"):
            raise ExportOptionError(f"theme must be one of plain|report, got {kwargs['theme']!r}")
        accent = kwargs.get("accent")
        if accent is not None and not (len(accent) == 7 and accent.startswith("#")):
            raise ExportOptionError("accent must be a #rrggbb hex colour")
        recorded.append({"slug": slug, "format": format, **kwargs})
        return b"BYTES", CONTENT_TYPES[format]

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _render)
    return recorded


def _client(monkeypatch, *, workspace_settings=None, name="Acme Climate") -> TestClient:
    async def _project(db, workspace_id):
        return types.SimpleNamespace(id=PROJECT_ID)

    monkeypatch.setattr(findings_api, "current_project", _project)
    workspace = Workspace(id=uuid.uuid4(), name=name, kind="team", settings=workspace_settings)
    app = FastAPI()
    app.include_router(findings_api.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[current_workspace] = lambda: WorkspaceContext(workspace, "owner")
    app.dependency_overrides[get_db] = lambda: None
    return TestClient(app)


def test_theme_accent_and_subtitle_are_passed_through(monkeypatch, calls):
    client = _client(monkeypatch)
    response = client.get("/api/deliverables/tcfd/export?format=pdf&theme=report&accent=%23336699")
    assert response.status_code == 200
    assert calls == [
        {
            "slug": "tcfd",
            "format": "pdf",
            "theme": "report",
            "accent": "#336699",
            "subtitle": "Acme Climate",
        }
    ]


def test_default_theme_is_plain_in_the_standard_edition(monkeypatch, calls):
    _client(monkeypatch).get("/api/deliverables/tcfd/export?format=html")
    assert calls[0]["theme"] == "plain"
    assert calls[0]["accent"] is None


def test_default_theme_is_report_in_the_cohort_edition(monkeypatch, calls):
    client = _client(monkeypatch, workspace_settings={"edition": "cohort"})
    client.get("/api/deliverables/tcfd/export?format=html")
    assert calls[0]["theme"] == "report"


def test_an_explicit_theme_beats_the_edition_default(monkeypatch, calls):
    client = _client(monkeypatch, workspace_settings={"edition": "cohort"})
    client.get("/api/deliverables/tcfd/export?format=docx&theme=plain")
    assert calls[0]["theme"] == "plain"


@pytest.mark.parametrize(
    "query",
    ["format=pdf&theme=neon", "format=pdf&accent=red", "format=rtf", "format=html&accent=%23zzz"],
)
def test_bad_theme_accent_or_format_is_a_422(monkeypatch, calls, query):
    response = _client(monkeypatch).get(f"/api/deliverables/tcfd/export?{query}")
    assert response.status_code == 422
    assert response.json()["detail"]
    assert calls == []


def test_a_renderer_without_docx_support_is_a_clean_422(monkeypatch):
    async def _no_docx(db, project_id, slug, format, include_draft=False, **kw):
        raise ExportOptionError(f"format must be markdown|html|pdf, got {format!r}")

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _no_docx)
    response = _client(monkeypatch).get("/api/deliverables/tcfd/export?format=docx")
    assert response.status_code == 422
    assert "format must be" in response.json()["detail"]


@pytest.mark.parametrize(
    ("fmt", "extension"),
    [("pdf", "pdf"), ("docx", "docx"), ("pptx", "pptx")],
)
def test_binary_formats_are_attachments_with_the_right_type_and_name(monkeypatch, calls, fmt, extension):
    response = _client(monkeypatch).get(f"/api/deliverables/tcfd/export?format={fmt}")
    assert response.status_code == 200
    assert response.headers["content-type"] == CONTENT_TYPES[fmt]
    assert response.headers["content-disposition"] == (
        f'attachment; filename="tcfd.{extension}"; filename*=UTF-8\'\'tcfd.{extension}'
    )
    assert response.content == b"BYTES"


def test_a_missing_renderer_package_is_a_501(monkeypatch):
    async def _missing(db, project_id, slug, format, include_draft=False, **kw):
        raise RendererUnavailable("docx export needs the python-docx package, which is not installed")

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _missing)
    response = _client(monkeypatch).get("/api/deliverables/tcfd/export?format=docx")
    assert response.status_code == 501
    assert "python-docx" in response.json()["detail"]


def test_an_internal_value_error_is_a_generic_500_without_detail(monkeypatch):
    async def _boom(db, project_id, slug, format, include_draft=False, **kw):
        raise ValueError("secret internal path /srv/x/y.py line 3")

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _boom)
    response = _client(monkeypatch).get("/api/deliverables/tcfd/export?format=docx")
    assert response.status_code == 500
    assert response.json() == {"detail": "Export failed"}


@pytest.mark.parametrize(
    "slug",
    ["a%22b%3Bx%3D1", "line%0D%0AX-Evil%3A%201", "r%C3%A9sum%C3%A9-%E6%97%A5%E6%9C%AC", "%22%22", "%E6%97%A5%E6%9C%AC", "%20"],
)
def test_content_disposition_survives_hostile_slugs(monkeypatch, calls, slug):
    response = _client(monkeypatch).get(f"/api/deliverables/{slug}/export?format=docx")
    assert response.status_code == 200
    header = response.headers["content-disposition"]
    assert "\r" not in header and "\n" not in header and "X-Evil" not in response.headers
    ascii_part, _, star = header.partition("; filename*=UTF-8''")
    assert ascii_part.startswith('attachment; filename="') and ascii_part.endswith('.docx"')
    name = ascii_part[len('attachment; filename="') : -1]
    assert name.isascii() and '"' not in name and ";" not in name and name.strip(".-_")
    assert star.endswith(".docx") and star.isascii() and '"' not in star


def test_content_disposition_helper_values():
    f = findings_api._content_disposition
    assert f('a"b', "pdf") == "attachment; filename=\"a-b.pdf\"; filename*=UTF-8''a%22b.pdf"
    assert f("", "pdf").startswith('attachment; filename="deliverable.pdf"')
    assert f("日本", "pdf").startswith('attachment; filename="deliverable.pdf"')
    assert "filename*=UTF-8''%E6%97%A5%E6%9C%AC.pdf" in f("日本", "pdf")
    assert "%0D%0A" not in f("x\r\ny", "pdf") and "\r" not in f("x\r\ny", "pdf")
    assert f("lone\ud800", "pdf")  # no UnicodeEncodeError


def test_markdown_and_html_keep_their_types(monkeypatch, calls):
    client = _client(monkeypatch)
    md = client.get("/api/deliverables/tcfd/export?format=markdown")
    assert md.headers["content-type"].startswith("text/markdown")
    html = client.get("/api/deliverables/tcfd/export?format=html")
    assert html.headers["content-type"].startswith("text/html")
    assert "sandbox" in html.headers["content-security-policy"]


# ── connected writes ─────────────────────────────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def make_ctx(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'wiring.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    target = types.SimpleNamespace(slug="site-finance", label="Finance", path="/Documents/tret")

    async def _list(db, workspace_id):
        return [target]

    monkeypatch.setattr(tools_module.connections_service, "list_write_targets", _list, raising=False)

    async with factory() as session:

        async def _make(settings=None):
            workspace = Workspace(name="W", settings=settings or {})
            session.add(workspace)
            await session.flush()
            project = Project(workspace_id=workspace.id, name="P")
            session.add(project)
            await session.flush()
            session.add(
                Finding(
                    run_id=uuid.uuid4(),
                    project_id=project.id,
                    schema_slug="draft_section",
                    subject={"deliverable": "tcfd-report", "section": "governance"},
                    payload={"markdown": "# Governance\n\nProse."},
                    provenance={},
                    status="draft",
                )
            )
            await session.commit()
            return RunContext(
                db=session,
                run_id=uuid.uuid4(),
                project_id=project.id,
                pack_id=None,
                doctrine_sha=None,
                model_used=None,
                document_ids=[],
                output_schemas={},
                workspace_id=workspace.id,
            )

        yield _make

    await engine.dispose()


async def _proposed(ctx) -> Finding:
    return (
        await ctx.db.execute(select(Finding).where(Finding.schema_slug == "connected_write"))
    ).scalars().one()


async def test_a_deliverable_write_carries_an_explicit_theme(make_ctx):
    ctx = await make_ctx()
    await propose_connected_write(
        ctx, target="site-finance", filename="report", deliverable="tcfd-report", format="docx", theme="report"
    )
    finding = await _proposed(ctx)
    assert finding.payload["source"] == {
        "kind": "deliverable",
        "slug": "tcfd-report",
        "format": "docx",
        "theme": "report",
    }
    assert finding.payload["filename"] == "report.docx"
    assert finding.payload["content_type"] == DOCX


async def test_pptx_gets_its_extension_and_content_type(make_ctx):
    ctx = await make_ctx()
    await propose_connected_write(
        ctx, target="site-finance", filename="deck.md", deliverable="tcfd-report", format="pptx"
    )
    finding = await _proposed(ctx)
    assert finding.payload["filename"] == "deck.pptx"
    assert finding.payload["content_type"] == PPTX


async def test_the_theme_defaults_by_edition(make_ctx):
    standard = await make_ctx()
    await propose_connected_write(standard, target="site-finance", filename="r", deliverable="tcfd-report")
    assert (await _proposed(standard)).payload["source"]["theme"] == "plain"

    cohort = await make_ctx({"edition": "cohort"})
    await propose_connected_write(cohort, target="site-finance", filename="r2", deliverable="tcfd-report")
    findings = (
        await cohort.db.execute(
            select(Finding).where(
                Finding.schema_slug == "connected_write", Finding.project_id == cohort.project_id
            )
        )
    ).scalars().all()
    assert findings[0].payload["source"]["theme"] == "report"


async def test_an_unknown_theme_or_format_is_refused(make_ctx):
    ctx = await make_ctx()
    with pytest.raises(ToolError):
        await propose_connected_write(
            ctx, target="site-finance", filename="r", deliverable="tcfd-report", theme="neon"
        )
    with pytest.raises(ToolError):
        await propose_connected_write(
            ctx, target="site-finance", filename="r", deliverable="tcfd-report", format="rtf"
        )


async def test_inline_content_does_not_gain_a_theme_or_office_formats(make_ctx):
    ctx = await make_ctx()
    await propose_connected_write(ctx, target="site-finance", filename="n.md", content="hello")
    assert "theme" not in (await _proposed(ctx)).payload["source"]


async def test_render_write_bytes_passes_the_stored_theme(monkeypatch):
    seen: list[dict] = []

    async def _render(db, project_id, slug, format, include_draft=False, **kwargs):
        seen.append({"slug": slug, "format": format, **kwargs})
        return b"x", CONTENT_TYPES[format]

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _render)
    source = {"kind": "deliverable", "slug": "tcfd", "format": "pptx", "theme": "report"}
    data, content_type = await findings_api._render_write_bytes(None, PROJECT_ID, {}, source)
    assert (data, content_type) == (b"x", PPTX)
    assert seen == [{"slug": "tcfd", "format": "pptx", "theme": "report"}]

    # A proposal made before themes existed has none and renders as it always did.
    seen.clear()
    await findings_api._render_write_bytes(
        None, PROJECT_ID, {}, {"kind": "deliverable", "slug": "tcfd", "format": "pdf"}
    )
    assert seen == [{"slug": "tcfd", "format": "pdf"}]
