"""`propose_package_map` (engine/tools.py) and approval-time packaging (api/findings.py).

The tool records a `package_map` Finding and packages nothing; approving that
finding runs `services/packaging.py` over the approved map. PDFs are built with
`tests/test_packaging.py::make_pdf` (one distinct text marker per page), the
source file is written under a tmp `storage_dir`, and its Document row is
created by hand, as `test_connected_write.py` does for write-back sources.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import ARRAY, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from tests import test_findings_write as fw
from tests.test_packaging import make_pdf
from tret.api import auth, findings as findings_api
from tret.api.auth import login_limiter
from tret.db.engine import get_db
from tret.db.models import Base, Document, Finding, Project, Workspace
from tret.engine import tools as tools_module
from tret.engine.tools import (
    PACKAGING_TOOL_NAMES,
    RunContext,
    ToolError,
    execute_tool,
    get_builtin_tools,
    propose_package_map,
    read_document,
)
from tret.services import packaging
from tret.services.packaging import ROLES, SCOPES

# Pages 2-4 and 6 of the 6-page make_pdf fixture (page 2 links to 4 and to 1).
PROJECT_ID, WORKSPACE = fw.PROJECT_ID, fw.WORKSPACE
decide, login_as = fw.decide, fw.login_as
# The blessing-gate fixtures, shared with test_findings_write.py as-is.
people, members = fw.people, fw.members
EMBEDDED = dict(
    title="Sustainability statement",
    role="embedded_statement_and_assurance",
    scope="embedded",
    keep_ranges=[[2, 4], [6, 6]],
)


# ── a disposable sqlite tret (the tool side) ─────────────────────────────────
@compiles(JSONB, "sqlite")
def _jsonb_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@compiles(ARRAY, "sqlite")
def _array_on_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL only
    return "JSON"


@pytest.fixture()
async def db(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'package_map.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        workspace = Workspace(name="W")
        session.add(workspace)
        await session.flush()
        session.add(Project(workspace_id=workspace.id, name="P"))
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture()
async def ctx(db):
    project = (await db.execute(select(Project))).scalars().one()
    yield RunContext(
        db=db,
        run_id=uuid.uuid4(),
        project_id=project.id,
        pack_id=None,
        doctrine_sha="sha-d",
        model_used="test/model",
        document_ids=[],
        output_schemas={},
        workspace_id=project.workspace_id,
    )


async def _source_document(
    db, project_id, tmp_path: Path, *, data: bytes | None = None, filename="Siemens Report 2025.pdf",
    source_kind="web", meta=None, write_file=True,
) -> Document:
    data = make_pdf() if data is None else data
    sha = hashlib.sha256(data).hexdigest()
    path = tmp_path / f"{sha}-source.pdf"
    if write_file:
        path.write_bytes(data)
    doc = Document(
        project_id=project_id,
        filename=filename,
        content_type="application/pdf",
        byte_size=len(data),
        storage_path=str(path),
        extraction_status="done",
        sha256=sha,
        source_kind=source_kind,
        meta=meta if meta is not None else {"provenance": "web_capture", "url": "https://example.org/r.pdf"},
    )
    db.add(doc)
    await db.flush()
    return doc


@pytest.fixture()
async def attached(ctx, tmp_path):
    doc = await _source_document(ctx.db, ctx.project_id, tmp_path)
    ctx.document_ids.append(doc.id)
    return doc


# ══════════════════════════════════════════════════════════════════════════
# the tool
# ══════════════════════════════════════════════════════════════════════════
def test_the_tool_is_registered_and_its_names_are_exported():
    assert PACKAGING_TOOL_NAMES == frozenset({"propose_package_map"})
    spec = get_builtin_tools()["propose_package_map"]
    props = spec.parameters["properties"]
    assert spec.parameters["required"] == ["source_document_id", "title", "role", "scope"]
    assert props["role"]["enum"] == list(ROLES)
    assert props["scope"]["enum"] == list(SCOPES)
    assert "physical" in spec.description.lower()


async def test_a_proposal_records_a_draft_with_the_exact_payload_shape(ctx, attached):
    result = await propose_package_map(
        ctx,
        source_document_id=str(attached.id),
        incorporations=[
            {
                "originating_page": 2,
                "cited_component": "Remuneration report",
                "resolved_range": [3, 4],
                "status": "retained",
                "note": "pp. 3-4 in full",
            },
            {"originating_page": 3, "cited_component": "Annex", "status": "unresolved"},
        ],
        exclusions=[{"range": [5, 5], "reason": "advertising"}],
        review_note="check page 6",
        **EMBEDDED,
    )

    finding = (await ctx.db.execute(select(Finding))).scalars().one()
    assert finding.schema_slug == "package_map"
    assert finding.status == "draft"
    assert finding.subject == {
        "source_document_id": str(attached.id),
        "stem": "Siemens-Report-2025",
        "title": "Sustainability statement",
    }
    assert finding.payload == {
        "source": {
            "document_id": str(attached.id),
            "filename": "Siemens Report 2025.pdf",
            "sha256": attached.sha256,
            "byte_size": attached.byte_size,
            "page_count": 6,
            "source_kind": "web",
            "provenance": "web_capture",
            "url": "https://example.org/r.pdf",
        },
        "map": {
            "title": "Sustainability statement",
            "role": "embedded_statement_and_assurance",
            "scope": "embedded",
            "keep_ranges": [[2, 4], [6, 6]],
            "review_note": "check page 6",
            "incorporations": [
                {
                    "originating_page": 2,
                    "cited_component": "Remuneration report",
                    "resolved_range": [3, 4],
                    "status": "retained",
                    "note": "pp. 3-4 in full",
                },
                {
                    "originating_page": 3,
                    "cited_component": "Annex",
                    "resolved_range": None,
                    "status": "unresolved",
                },
            ],
            "exclusions": [{"range": [5, 5], "reason": "advertising"}],
        },
        "stem": "Siemens-Report-2025",
        "selected_page_count": 4,
        "full_original": False,
        "package": None,
    }
    assert finding.provenance["model"] == "test/model"
    assert finding.provenance["doctrine_sha"] == "sha-d"
    assert finding.provenance["retrieved_values"] == []
    assert finding.provenance["document_ids"] == [str(attached.id)]
    assert ctx.findings_created == [finding.id]
    json.dumps(finding.payload)  # JSON-clean

    assert "4 of 6" in result
    assert str(finding.id) in result
    assert "NOTHING has been packaged" in result
    assert "approver accepts" in result
    assert "physical" in result


async def test_a_standalone_whole_report_uses_null_ranges(ctx, attached):
    await propose_package_map(
        ctx,
        source_document_id=str(attached.id),
        title="Whole report",
        role="primary_disclosure",
        scope="standalone",
        keep_ranges=None,
        stem="whole.report-1",
    )
    finding = (await ctx.db.execute(select(Finding))).scalars().one()
    assert finding.payload["map"]["keep_ranges"] is None
    assert finding.payload["selected_page_count"] == 6
    assert finding.payload["full_original"] is True
    assert finding.payload["stem"] == "whole.report-1"
    assert finding.payload["map"]["incorporations"] == []
    assert finding.payload["map"]["exclusions"] == []


async def test_an_earlier_draft_with_the_same_subject_is_superseded(ctx, attached):
    await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    await propose_package_map(
        ctx, source_document_id=str(attached.id), **{**EMBEDDED, "keep_ranges": [[2, 3]]}
    )
    rows = (await ctx.db.execute(select(Finding).order_by(Finding.created_at))).scalars().all()
    assert len(rows) == 2
    older = next(f for f in rows if f.payload["selected_page_count"] == 4)
    newer = next(f for f in rows if f.payload["selected_page_count"] == 2)
    assert older.status == "superseded"
    assert older.provenance["superseded_by"] == str(newer.id)
    assert newer.status == "draft"
    assert newer.provenance["supersedes"] == [str(older.id)]


async def test_a_different_subject_is_not_superseded(ctx, attached):
    await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    await propose_package_map(
        ctx, source_document_id=str(attached.id), **{**EMBEDDED, "title": "Assurance only"}
    )
    statuses = [f.status for f in (await ctx.db.execute(select(Finding))).scalars().all()]
    assert statuses == ["draft", "draft"]


async def test_an_approved_package_map_is_never_superseded(ctx, attached):
    await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    first = (await ctx.db.execute(select(Finding))).scalars().one()
    first.status = "approved"
    await ctx.db.flush()
    await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    await ctx.db.refresh(first)
    assert first.status == "approved"


@pytest.mark.parametrize(
    ("overrides", "fragment"),
    [
        ({"keep_ranges": [[2, 9]]}, "Out-of-bounds range"),
        ({"keep_ranges": [[4, 5], [2, 3]]}, "increasing, disjoint"),
        ({"keep_ranges": [[2, 4], [4, 5]]}, "increasing, disjoint"),
        ({"keep_ranges": [[3, 2]]}, "Out-of-bounds range"),
        ({"keep_ranges": [[0, 2]]}, "Out-of-bounds range"),
        ({"keep_ranges": [[1, 6]]}, "cannot silently retain every page"),
        ({"keep_ranges": None}, "require explicit reviewed keep_ranges"),
        ({"keep_ranges": []}, "nonempty"),
        ({"keep_ranges": [[1, "2"]]}, "integer pair"),
        ({"keep_ranges": [[1, 2, 3]]}, "integer pair"),
        ({"role": "nonsense"}, "role must be one of"),
        ({"scope": "sideways"}, "scope=standalone or embedded"),
        ({"title": "   "}, "title is required"),
    ],
)
async def test_invalid_ranges_are_a_tool_error_quoting_the_packaging_error(
    ctx, attached, overrides, fragment
):
    with pytest.raises(ToolError, match=fragment):
        await propose_package_map(ctx, source_document_id=str(attached.id), **{**EMBEDDED, **overrides})
    assert (await ctx.db.execute(select(Finding))).scalars().all() == []


@pytest.mark.parametrize(
    ("kwargs", "fragment"),
    [
        ({"incorporations": [{"originating_page": 99, "cited_component": "x", "status": "unresolved"}]},
         "originating_page"),
        ({"incorporations": [{"originating_page": 2, "cited_component": "x", "status": "maybe"}]}, "status"),
        ({"incorporations": [{"originating_page": 2, "cited_component": "x", "status": "retained"}]},
         "no resolved_range"),
        ({"incorporations": [{"originating_page": 2, "cited_component": "x", "status": "retained",
                              "resolved_range": [5, 5]}]}, "not all in keep_ranges"),
        ({"incorporations": [{"originating_page": 2, "cited_component": "x", "status": "retained",
                              "resolved_range": [3, 40]}]}, "Out-of-bounds"),
        ({"exclusions": [{"range": [5, 5]}]}, "reason"),
        ({"exclusions": [{"range": [5, 50], "reason": "r"}]}, "Out-of-bounds"),
        ({"incorporations": "nope"}, "array"),
    ],
)
async def test_malformed_incorporations_and_exclusions_are_refused(ctx, attached, kwargs, fragment):
    with pytest.raises(ToolError, match=fragment):
        await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED, **kwargs)


async def test_a_non_pdf_document_is_refused(ctx, tmp_path):
    doc = await _source_document(
        ctx.db, ctx.project_id, tmp_path, data=b"just some text, not a pdf\n", filename="notes.pdf"
    )
    ctx.document_ids.append(doc.id)
    with pytest.raises(ToolError, match="not a PDF"):
        await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)


async def test_a_corrupt_pdf_is_a_tool_error(ctx, tmp_path):
    doc = await _source_document(
        ctx.db, ctx.project_id, tmp_path, data=b"%PDF-1.7\nthis is not really a pdf\n", filename="bad.pdf"
    )
    ctx.document_ids.append(doc.id)
    with pytest.raises(ToolError, match="could not be parsed"):
        await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)


async def test_a_document_not_attached_to_the_run_is_refused(ctx, tmp_path):
    doc = await _source_document(ctx.db, ctx.project_id, tmp_path)  # not in ctx.document_ids
    with pytest.raises(ToolError, match="not attached to this run"):
        await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)


async def test_a_document_from_another_project_is_refused(ctx, tmp_path):
    other = Project(workspace_id=ctx.workspace_id, name="Other")
    ctx.db.add(other)
    await ctx.db.flush()
    doc = await _source_document(ctx.db, other.id, tmp_path)
    ctx.document_ids.append(doc.id)
    with pytest.raises(ToolError, match="not found in this project"):
        await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)


async def test_a_bad_document_id_is_refused(ctx):
    with pytest.raises(ToolError, match="not a valid document id"):
        await propose_package_map(ctx, source_document_id="nope", **EMBEDDED)


async def test_a_missing_stored_file_is_refused(ctx, tmp_path):
    doc = await _source_document(ctx.db, ctx.project_id, tmp_path, write_file=False)
    ctx.document_ids.append(doc.id)
    with pytest.raises(ToolError, match="missing"):
        await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)


async def test_a_file_that_no_longer_matches_its_hash_is_refused(ctx, attached):
    Path(attached.storage_path).write_bytes(make_pdf(8))  # a different, valid PDF
    with pytest.raises(ToolError, match="no longer matches"):
        await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    assert (await ctx.db.execute(select(Finding))).scalars().all() == []


@pytest.mark.parametrize("stem", ["../escape", "a b", "-leading", ".hidden", "x" * 81, "sl/ash", ""])
async def test_an_unsafe_explicit_stem_is_refused(ctx, attached, stem):
    with pytest.raises(ToolError, match="stem"):
        await propose_package_map(ctx, source_document_id=str(attached.id), stem=stem, **EMBEDDED)


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("Siemens Report 2025.pdf", "Siemens-Report-2025"),
        ("../../etc/passwd.pdf", "passwd"),
        ("---weird name!!.pdf", "weird-name"),
        ("report.v2.final.pdf", "report.v2.final"),
        ("日本語.pdf", "package"),
        (("a" * 120) + ".pdf", "a" * 80),
    ],
)
async def test_the_default_stem_is_sanitised(ctx, tmp_path, filename, expected):
    doc = await _source_document(ctx.db, ctx.project_id, tmp_path, filename=filename)
    ctx.document_ids.append(doc.id)
    await propose_package_map(ctx, source_document_id=str(doc.id), **EMBEDDED)
    finding = (await ctx.db.execute(select(Finding))).scalars().one()
    assert finding.payload["stem"] == expected
    assert packaging._STEM_RE.fullmatch(expected)


async def test_execute_tool_returns_a_tool_error_for_a_bad_map(ctx, attached):
    spec = get_builtin_tools()["propose_package_map"]
    result, is_error = await execute_tool(
        ctx, spec, {"source_document_id": str(attached.id), **EMBEDDED, "keep_ranges": [[2, 99]]}
    )
    assert is_error is True
    assert "Out-of-bounds" in result


# ══════════════════════════════════════════════════════════════════════════
# read_document banner
# ══════════════════════════════════════════════════════════════════════════
async def test_read_document_marks_a_package_document(ctx):
    source_id = uuid.uuid4()
    doc = Document(
        project_id=ctx.project_id,
        filename="siemens.layout.md",
        content_type="text/markdown; charset=utf-8",
        byte_size=10,
        storage_path="/nope",
        extracted_text="Physical page 2 marker",
        extraction_status="done",
        sha256="f" * 64,
        source_kind="package",
        meta={
            "provenance": "package",
            "source_document_id": str(source_id),
            "source_sha256": "a" * 64,
            "retained_sha256": "b" * 64,
            "text_mode": "pdftotext",
            "package_role": "layout_text",
        },
    )
    ctx.db.add(doc)
    await ctx.db.flush()
    ctx.document_ids.append(doc.id)

    out = await read_document(ctx, document_id=str(doc.id))

    assert "REVIEWED PACKAGE EXTRACT" in out
    assert f"document {source_id}" in out
    assert "b" * 64 in out
    assert "text_mode pdftotext" in out
    assert "No number here may be cited" in out
    assert "Physical page 2 marker" in out
    assert tools_module._document_tier_label("package") == " REVIEWED PACKAGE EXTRACT"


# ══════════════════════════════════════════════════════════════════════════
# approval-time packaging
# ══════════════════════════════════════════════════════════════════════════
class PackagingSession(fw.FakeSession):
    """FakeSession whose flush assigns ids to rows added without one, as a real flush does."""

    async def flush(self):
        for bucket in self.store.values():
            for key, obj in list(bucket.items()):
                if getattr(obj, "id", "x") is None:
                    obj.id = uuid.uuid4()
                    del bucket[key]
                    bucket[obj.id] = obj


@pytest.fixture
def storage(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "storage"
    root.mkdir()
    monkeypatch.setattr(findings_api, "get_settings", lambda: SimpleNamespace(storage_dir=str(root)))
    return root


def _map_finding(source_doc: Document, **payload_over) -> Finding:
    payload = {
        "source": {
            "document_id": str(source_doc.id),
            "filename": source_doc.filename,
            "sha256": source_doc.sha256,
            "byte_size": source_doc.byte_size,
            "page_count": 6,
            "source_kind": "web",
            "provenance": "web_capture",
            "url": "https://example.org/r.pdf",
        },
        "map": {
            "title": "Sustainability statement",
            "role": "embedded_statement_and_assurance",
            "scope": "embedded",
            "keep_ranges": [[2, 4], [6, 6]],
            "review_note": "",
            "incorporations": [
                {
                    "originating_page": 2,
                    "cited_component": "Remuneration report",
                    "resolved_range": [3, 4],
                    "status": "retained",
                }
            ],
            "exclusions": [{"range": [5, 5], "reason": "advertising"}],
        },
        "stem": "siemens-2025",
        "selected_page_count": 4,
        "full_original": False,
        "package": None,
    }
    payload.update(payload_over)
    return Finding(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        schema_slug="package_map",
        subject={"source_document_id": str(source_doc.id), "stem": "siemens-2025", "title": "S"},
        payload=payload,
        provenance={"model": "anthropic/test"},
        status="draft",
    )


def _stored_source(tmp_path: Path, data: bytes | None = None) -> tuple[Document, Path]:
    data = make_pdf() if data is None else data
    sha = hashlib.sha256(data).hexdigest()
    path = tmp_path / "captures" / f"{sha}-siemens.pdf"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    doc = Document(
        id=uuid.uuid4(), project_id=PROJECT_ID, filename="siemens.pdf", content_type="application/pdf",
        byte_size=len(data), storage_path=str(path), sha256=sha, source_kind="web",
        meta={"provenance": "web_capture"},
    )
    return doc, path


@pytest.fixture
def world(people, members, tmp_path, storage):
    source, source_path = _stored_source(tmp_path)
    finding = _map_finding(source)
    session = PackagingSession(
        users=people.values(),
        findings_=[finding],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=members,
    )
    session.store["Document"] = {source.id: source}
    return SimpleNamespace(
        db=session, finding=finding, source=source, source_path=source_path, storage=storage
    )


@pytest.fixture
def client(world) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: world.db
    login_limiter.reset()
    with TestClient(app) as c:
        yield c
    login_limiter.reset()


def _package_docs(world) -> list[Document]:
    return [d for d in world.db.store["Document"].values() if d.source_kind == "package"]


def test_approving_packages_the_approved_pages(client, world, people):
    login_as(client, people["approver"])
    response = decide(client, world.finding.id, action="approve")

    assert response.status_code == 200
    assert world.finding.status == "approved"
    package = world.finding.payload["package"]
    assert package["status"] == "packaged"
    assert package["error"] is None
    assert package["approver_id"] == str(people["approver"].id)
    assert package["retained_page_count"] == 4
    assert package["source_page_count"] == 6
    assert package["text_mode"] in ("pdftotext", "pypdf")
    assert package["packaged_at"]
    assert isinstance(package["limitations"], list) and package["limitations"]
    assert package["wall_seconds"] >= 0

    out_dir = world.storage / "packages" / str(world.finding.id)
    assert sorted(p.name for p in out_dir.iterdir()) == sorted(
        f"siemens-2025{suffix}"
        for suffix in (
            ".pdf", ".layout.md", ".raw.md", ".page_map.json", ".links.json",
            ".extraction_stats.json", ".package.json",
        )
    )
    retained = out_dir / "siemens-2025.pdf"
    reader = PdfReader(str(retained))
    assert len(reader.pages) == 4
    assert [("Physical page %d marker" % n) in reader.pages[i].extract_text()
            for i, n in enumerate((2, 3, 4, 6))] == [True] * 4
    assert package["retained_sha256"] == hashlib.sha256(retained.read_bytes()).hexdigest()

    docs = _package_docs(world)
    assert len(docs) == 7
    assert {d["role"] for d in package["documents"]} == {
        "retained_pdf", "layout_text", "raw_text", "page_map", "links", "extraction_stats",
        "package_record",
    }
    by_role = {d["role"]: d for d in package["documents"]}
    stored = {str(d.id): d for d in docs}
    assert set(stored) == {d["document_id"] for d in package["documents"]}
    for role, entry in by_role.items():
        doc = stored[entry["document_id"]]
        assert doc.source_kind == "package"
        assert doc.uploaded_by is None
        assert doc.project_id == PROJECT_ID
        assert doc.filename == entry["filename"] == f"siemens-2025{packaging._FILE_SUFFIX[role]}"
        assert doc.sha256 == entry["sha256"]
        assert doc.byte_size == entry["byte_size"] == Path(doc.storage_path).stat().st_size
        assert Path(doc.storage_path).parent == out_dir.resolve()
        assert doc.content_type == entry["content_type"]
        assert doc.meta["provenance"] == "package"
        assert doc.meta["package_finding_id"] == str(world.finding.id)
        assert doc.meta["package_role"] == role
        assert doc.meta["source_document_id"] == str(world.source.id)
        assert doc.meta["source_sha256"] == world.source.sha256
        assert doc.meta["retained_sha256"] == package["retained_sha256"]
        assert doc.meta["text_mode"] == package["text_mode"]
        assert doc.meta["stem"] == "siemens-2025"
        assert doc.meta["title"] == "Sustainability statement"
        assert doc.meta["role"] == "embedded_statement_and_assurance"
        assert doc.meta["scope"] == "embedded"
        assert doc.meta["keep_ranges"] == [[2, 4], [6, 6]]
    # extraction ran on the registered files
    assert "Physical page 3 marker" in (stored[by_role["layout_text"]["document_id"]].extracted_text or "")
    # the page map and the record carry the approved map
    record = json.loads((out_dir / "siemens-2025.package.json").read_text())
    assert record["keep_ranges"] == [[2, 4], [6, 6]]
    assert record["source_meta"]["document_id"] == str(world.source.id)
    page_map = json.loads((out_dir / "siemens-2025.page_map.json").read_text())
    assert [p["source_pdf_page"] for p in page_map["pages"]] == [2, 3, 4, 6]
    # the source file was not touched
    assert world.source_path.read_bytes() == make_pdf()


def test_a_standalone_null_map_retains_the_original_byte_for_byte(client, world, people):
    world.finding.payload["map"].update(scope="standalone", role="primary_disclosure", keep_ranges=None,
                                        incorporations=[], exclusions=[])
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    package = world.finding.payload["package"]
    assert package["status"] == "packaged"
    assert package["retained_page_count"] == 6
    assert package["retained_sha256"] == world.source.sha256
    retained = world.storage / "packages" / str(world.finding.id) / "siemens-2025.pdf"
    assert retained.read_bytes() == world.source_path.read_bytes()


def test_a_changed_source_fails_packaging_and_leaves_nothing(client, world, people):
    world.source_path.write_bytes(make_pdf(8))  # changed between proposal and approval
    login_as(client, people["approver"])
    response = decide(client, world.finding.id, action="approve")

    assert response.status_code == 200  # the approval itself stands
    assert world.finding.status == "approved"
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert package["error"] == "source bytes changed since the map was reviewed; recapture and re-review"
    assert package["documents"] == []
    assert _package_docs(world) == []
    assert not (world.storage / "packages" / str(world.finding.id)).exists()


def test_a_missing_source_file_fails_packaging(client, world, people):
    world.source_path.unlink()
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert "missing" in package["error"]
    assert _package_docs(world) == []


def test_a_source_in_another_project_fails_packaging(client, world, people):
    world.source.project_id = uuid.uuid4()
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert world.finding.payload["package"]["status"] == "failed"
    assert _package_docs(world) == []


def test_an_invalid_approved_map_fails_without_files(client, world, people):
    world.finding.payload["map"]["keep_ranges"] = [[2, 99]]  # tampered after review
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert "Out-of-bounds" in package["error"]
    assert not (world.storage / "packages" / str(world.finding.id)).exists()
    assert _package_docs(world) == []


def test_the_packager_runs_in_a_child_process_with_the_deadline(client, world, people, monkeypatch):
    seen = {}
    real = packaging.run_packaging

    def _spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", _spy)
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert world.finding.payload["package"]["status"] == "packaged"
    assert seen["deadline_seconds"] == findings_api.PACKAGING_TIMEOUT_SECONDS == 300
    assert seen["stem"] == "siemens-2025"


def test_a_packager_crash_is_recorded_and_cleans_up(client, world, people, monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", _boom)
    login_as(client, people["approver"])
    response = decide(client, world.finding.id, action="approve")
    assert response.status_code == 200
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert "disk on fire" in package["error"]
    assert not (world.storage / "packages" / str(world.finding.id)).exists()
    assert _package_docs(world) == []


@pytest.mark.parametrize(
    "message", ["packaging exceeded 300 s", "packaging worker died (killed by signal 9)"]
)
def test_a_deadline_or_worker_death_is_recorded_as_a_failure(
    client, world, people, monkeypatch, message
):
    def _fail(*a, **kw):
        raise packaging.PackagingError(message)

    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", _fail)
    login_as(client, people["approver"])
    response = decide(client, world.finding.id, action="approve")
    assert response.status_code == 200
    assert world.finding.status == "approved"
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert package["error"] == message
    assert _package_docs(world) == []
    assert not (world.storage / "packages" / str(world.finding.id)).exists()


def test_files_published_but_not_registered_are_removed(client, world, people, monkeypatch):
    """The runner succeeded but registering a file failed: nothing may be left."""

    async def _boom(**kwargs):
        raise RuntimeError("extraction exploded")

    monkeypatch.setattr(findings_api, "ingest_document", _boom)
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    package = world.finding.payload["package"]
    assert package["status"] == "failed"
    assert "extraction exploded" in package["error"]
    assert not (world.storage / "packages" / str(world.finding.id)).exists()
    assert _package_docs(world) == []


def _registered_package_document(world, existing: Path) -> Document:
    doc = Document(
        id=uuid.uuid4(), project_id=PROJECT_ID, filename="siemens-2025.pdf",
        content_type="application/pdf", byte_size=8, storage_path=str(existing / "siemens-2025.pdf"),
        sha256="0" * 64, source_kind="package",
        meta={"provenance": "package", "package_finding_id": str(world.finding.id)},
    )
    world.db.store["Document"][doc.id] = doc
    return doc


def test_a_package_directory_with_registered_documents_is_refused_and_left_alone(
    client, world, people
):
    existing = world.storage / "packages" / str(world.finding.id)
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("evidence")
    _registered_package_document(world, existing)
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert world.finding.payload["package"]["status"] == "failed"
    assert "already exists" in world.finding.payload["package"]["error"]
    assert (existing / "keep.txt").read_text() == "evidence"


def test_an_orphan_package_directory_is_cleared_on_retry(client, world, people):
    """Debris from an attempt that died before registering anything must not make
    every retry fail until someone clears it by hand."""
    orphan = world.storage / "packages" / str(world.finding.id)
    orphan.mkdir(parents=True)
    (orphan / "siemens-2025.pdf").write_bytes(b"half-written")
    world.finding.status = "approved"
    world.finding.payload["package"] = {"status": "failed", "error": "died", "documents": []}
    login_as(client, people["approver"])

    response = client.post(f"/api/findings/{world.finding.id}/package-retry")

    assert response.status_code == 200
    assert world.finding.payload["package"]["status"] == "packaged"
    assert len(_package_docs(world)) == 7
    assert (orphan / "siemens-2025.pdf").read_bytes() != b"half-written"


def test_approval_records_running_in_the_same_commit_as_the_status(client, world, people, monkeypatch):
    seen = {}
    real = findings_api._package_approved_map

    async def _spy(db, finding, **kw):
        seen["status"] = finding.status
        seen["package"] = dict(finding.payload["package"])
        seen["commits"] = db.commits
        return await real(db, finding, **kw)

    monkeypatch.setattr(findings_api, "_package_approved_map", _spy)
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert seen["status"] == "approved"
    assert seen["package"]["status"] == "running"
    assert seen["package"]["started_at"]
    assert seen["commits"] == 1  # the approval commit already carried it
    assert world.finding.payload["package"]["status"] == "packaged"


def _running(world, *, age_seconds: float) -> None:
    from datetime import datetime, timedelta, timezone

    world.finding.status = "approved"
    world.finding.payload["package"] = {
        "status": "running",
        "started_at": (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
    }


def test_retry_while_running_is_a_409_and_touches_nothing(client, world, people, monkeypatch):
    called = []
    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", lambda *a, **k: called.append(1))
    _running(world, age_seconds=5)
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 409
    assert "still running" in response.json()["detail"]
    assert called == []
    assert world.finding.payload["package"]["status"] == "running"


def test_retry_of_a_stale_running_record_is_allowed(client, world, people):
    _running(
        world,
        age_seconds=findings_api.PACKAGING_TIMEOUT_SECONDS + findings_api.PACKAGING_STALE_GRACE_SECONDS + 5,
    )
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 200
    assert world.finding.payload["package"]["status"] == "packaged"


def test_retry_just_inside_the_grace_window_is_still_refused(client, world, people):
    _running(
        world,
        age_seconds=findings_api.PACKAGING_TIMEOUT_SECONDS + findings_api.PACKAGING_STALE_GRACE_SECONDS - 30,
    )
    login_as(client, people["approver"])
    assert client.post(f"/api/findings/{world.finding.id}/package-retry").status_code == 409


def test_a_running_record_with_no_usable_start_time_counts_as_stale(client, world, people):
    world.finding.status = "approved"
    world.finding.payload["package"] = {"status": "running"}
    login_as(client, people["approver"])
    assert client.post(f"/api/findings/{world.finding.id}/package-retry").status_code == 200


def test_a_retry_marks_running_before_it_packages(client, world, people, monkeypatch):
    world.finding.status = "approved"
    world.finding.payload["package"] = {"status": "failed", "error": "x", "documents": []}
    seen = {}
    real = findings_api._package_approved_map

    async def _spy(db, finding, **kw):
        seen["package"] = dict(finding.payload["package"])
        seen["commits"] = db.commits
        return await real(db, finding, **kw)

    monkeypatch.setattr(findings_api, "_package_approved_map", _spy)
    login_as(client, people["approver"])
    client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert seen["package"]["status"] == "running"
    assert seen["commits"] == 1  # committed (lock released) before the long part


def test_a_later_failing_attempt_never_deletes_a_prior_attempts_files(
    client, world, people, monkeypatch
):
    """The files in the package directory belong to a published package; an attempt
    that fails before publishing anything must leave them exactly as they were."""
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    out_dir = world.storage / "packages" / str(world.finding.id)
    before = {p.name: p.read_bytes() for p in out_dir.iterdir()}
    assert len(before) == 7

    # A second attempt (a retry that slipped past, or a stale-running takeover)
    # whose runner fails: with registered documents present it is refused up
    # front, and even a failure inside the runner must not touch the directory.
    def _boom(*a, **kw):
        raise packaging.PackagingError("packaging worker died (killed by signal 9)")

    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", _boom)
    world.finding.payload = {**world.finding.payload, "package": {"status": "failed", "error": "x"}}
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")

    assert response.status_code == 200
    assert world.finding.payload["package"]["status"] == "failed"
    assert {p.name: p.read_bytes() for p in out_dir.iterdir()} == before
    assert len(_package_docs(world)) == 7


def test_a_runner_failure_after_the_directory_check_does_not_delete_the_directory(
    client, world, people, monkeypatch
):
    """Same guarantee at the cleanup gate itself: the runner raises while the
    directory already holds files this attempt did not publish."""
    out_dir = world.storage / "packages" / str(world.finding.id)

    def _boom(source_path, mapping, target, **kw):
        # Files appear after the existence check, as if another attempt published
        # them in the meantime; this attempt then fails.
        target.mkdir(parents=True, exist_ok=True)
        (target / "other-attempt.pdf").write_bytes(b"theirs")
        raise packaging.PackagingError("packaging exceeded 300 s")

    monkeypatch.setattr(findings_api.packaging_service, "run_packaging", _boom)
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")

    assert world.finding.payload["package"]["status"] == "failed"
    assert (out_dir / "other-attempt.pdf").read_bytes() == b"theirs"


def test_retry_reruns_packaging_after_the_file_is_restored(client, world, people):
    world.source_path.write_bytes(make_pdf(8))
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert world.finding.payload["package"]["status"] == "failed"

    world.source_path.write_bytes(make_pdf())  # restored
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")

    assert response.status_code == 200
    assert response.json()["payload"]["package"]["status"] == "packaged"
    assert world.finding.payload["package"]["status"] == "packaged"
    assert world.finding.payload["package"]["approver_id"] == str(people["approver"].id)
    assert len(_package_docs(world)) == 7
    assert (world.storage / "packages" / str(world.finding.id) / "siemens-2025.pdf").is_file()


def test_retry_after_a_failed_run_that_stayed_failed_is_allowed_again(client, world, people):
    world.source_path.write_bytes(make_pdf(8))
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 200
    assert response.json()["payload"]["package"]["status"] == "failed"
    assert _package_docs(world) == []


def test_retry_accepts_an_approved_finding_whose_outcome_was_never_recorded(client, world, people):
    world.finding.status = "approved"  # process died after the approval commit
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 200
    assert world.finding.payload["package"]["status"] == "packaged"


def test_retry_on_a_packaged_finding_is_a_409(client, world, people):
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert world.finding.payload["package"]["status"] == "packaged"
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 409
    assert "nothing to retry" in response.json()["detail"]
    assert len(_package_docs(world)) == 7


def test_retry_on_a_draft_is_a_409(client, world, people):
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 409
    assert _package_docs(world) == []


def test_retry_on_another_schema_is_a_409(client, world, people):
    world.finding.schema_slug = "connected_write"
    world.finding.status = "approved"
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code == 409


def test_retry_requires_the_approver_role(client, world, people):
    world.finding.status = "approved"
    login_as(client, people["analyst"])
    response = client.post(f"/api/findings/{world.finding.id}/package-retry")
    assert response.status_code in (401, 403)
    assert _package_docs(world) == []


def test_retry_on_an_unknown_finding_is_404(client, world, people):
    login_as(client, people["approver"])
    response = client.post(f"/api/findings/{uuid.uuid4()}/package-retry")
    assert response.status_code == 404


def test_rejecting_does_not_package(client, world, people):
    login_as(client, people["approver"])
    response = decide(client, world.finding.id, action="reject")
    assert response.status_code == 200
    assert world.finding.status == "rejected"
    assert world.finding.payload["package"] is None
    assert _package_docs(world) == []
    assert not (world.storage / "packages").exists()


def test_approving_a_non_package_finding_never_packages(client, world, people, monkeypatch):
    called = []
    monkeypatch.setattr(findings_api, "_package_approved_map", lambda *a, **k: called.append(1))
    world.finding.schema_slug = "draft_section"
    world.finding.payload = {"markdown": "body"}
    login_as(client, people["approver"])
    decide(client, world.finding.id, action="approve")
    assert called == []
