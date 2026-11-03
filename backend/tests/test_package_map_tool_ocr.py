"""`propose_package_map` carries the OCR mode into the draft map (engine/tools.py).

Self-contained on purpose (own sqlite fixtures) so it does not depend on the shape
of the larger tests/test_package_map.py.
"""
from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tests.test_packaging import make_pdf
from tret.db.models import Base, Document, Finding, Project, Workspace
from tret.engine.tools import RunContext, ToolError, get_builtin_tools, propose_package_map
from tret.services import packaging


@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def ctx(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'ocr_tool.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        workspace = Workspace(name="W")
        session.add(workspace)
        await session.flush()
        project = Project(workspace_id=workspace.id, name="P")
        session.add(project)
        await session.commit()
        data = make_pdf()
        sha = hashlib.sha256(data).hexdigest()
        path = Path(tmp_path) / "source.pdf"
        path.write_bytes(data)
        doc = Document(
            project_id=project.id,
            filename="Report.pdf",
            content_type="application/pdf",
            byte_size=len(data),
            storage_path=str(path),
            extraction_status="done",
            sha256=sha,
            source_kind="web",
            meta={"provenance": "web_capture", "url": "https://example.org/r.pdf"},
        )
        session.add(doc)
        await session.flush()
        yield RunContext(
            db=session,
            run_id=uuid.uuid4(),
            project_id=project.id,
            pack_id=None,
            doctrine_sha="sha-d",
            model_used="test/model",
            document_ids=[doc.id],
            output_schemas={},
            workspace_id=project.workspace_id,
        ), doc
    await engine.dispose()


BASE = dict(title="Fact sheet", role="primary_disclosure", scope="standalone", keep_ranges=None)


def test_ocr_parameter_is_described_for_the_model():
    props = get_builtin_tools()["propose_package_map"].parameters["properties"]
    assert props["ocr"]["enum"] == list(packaging.OCR_MODES) == ["off", "auto", "all"]
    assert props["ocr"]["default"] == "off"
    assert "auto" in props["ocr"]["description"] and "never replaces" in props["ocr"]["description"]
    assert "ocr" not in get_builtin_tools()["propose_package_map"].parameters["required"]


@pytest.mark.parametrize("mode", ["off", "auto", "all"])
async def test_ocr_mode_is_stored_in_the_draft_map(ctx, mode):
    context, doc = ctx
    await propose_package_map(context, source_document_id=str(doc.id), ocr=mode, **BASE)
    finding = (await context.db.execute(select(Finding))).scalars().one()
    assert finding.payload["map"]["ocr"] == mode


async def test_ocr_defaults_to_off(ctx):
    context, doc = ctx
    await propose_package_map(context, source_document_id=str(doc.id), **BASE)
    finding = (await context.db.execute(select(Finding))).scalars().one()
    assert finding.payload["map"]["ocr"] == "off"


async def test_a_bad_ocr_value_is_an_invalid_map_error(ctx):
    context, doc = ctx
    with pytest.raises(ToolError, match="Invalid package map: ocr must be one of"):
        await propose_package_map(context, source_document_id=str(doc.id), ocr="sometimes", **BASE)
    assert (await context.db.execute(select(Finding))).scalars().all() == []
