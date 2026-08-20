"""Deliverable assembly and rendering: `services/export.py` end to end.

`test_export_safety.py` covers the *sanitisation* of this path — that model-authored
markdown cannot become live HTML or make WeasyPrint fetch anything. This file
covers the assembly itself, which is what a reviewer actually reads:

* only approved sections are exportable, and `include_draft` says so in the
  document rather than quietly blending drafts in;
* the provenance appendix carries each section's approval status, so a PDF that
  contains a draft says which section is one;
* section ordering and the supersede rule (latest finding per section wins);
* the footprint line is summed over distinct runs, and a run with no estimate
  contributes nothing rather than a zero.

The database is a fake that honours the WHERE clauses assembly relies on —
project, schema and status — because "only approved sections are exportable" is a
claim about exactly those filters.
"""
from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import findings as findings_api
from tret.api.auth import current_user
from tret.db.engine import get_db
from tret.db.models import Finding, Project, Run
from tret.services.export import assemble_deliverable, render_pdf

PROJECT_ID = uuid.uuid4()
OTHER_PROJECT_ID = uuid.uuid4()
T0 = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


# ── fake database that respects the filters under test ───────────────────────
def _keep(row, stmt) -> bool:
    where = stmt.whereclause
    if where is None:
        return True
    for clause in getattr(where, "clauses", [where]):
        left = getattr(clause, "left", None)
        if left is None or not hasattr(left, "key"):
            continue
        actual = getattr(row, left.key, None)
        operator = getattr(clause.operator, "__name__", "")
        right = getattr(clause, "right", None)
        if operator == "in_op":
            if actual not in [v for v in right.value]:
                return False
        elif hasattr(right, "value"):
            if actual != right.value:
                return False
    return True


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self, findings=(), runs=(), projects=None):
        self.rows = {
            "Finding": list(findings),
            "Run": list(runs),
            "Project": list(
                projects
                if projects is not None
                else [Project(id=PROJECT_ID, workspace_id=uuid.uuid4(), name="P", created_at=T0)]
            ),
        }

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"].__name__
        return FakeResult([r for r in self.rows.get(entity, []) if _keep(r, stmt)])


def section(
    name: str,
    *,
    markdown: str = "Body text.",
    status: str = "approved",
    minutes: int = 0,
    run_id=None,
    project_id=PROJECT_ID,
    deliverable: str = "tcfd-assessment",
    model: str = "anthropic/test-model",
    doctrine_sha: str = "abcdef0123456789abcdef",
) -> Finding:
    return Finding(
        id=uuid.uuid4(),
        run_id=run_id,
        project_id=project_id,
        schema_slug="draft_section",
        subject={"deliverable": deliverable, "section": name},
        payload={"markdown": markdown},
        provenance={"model": model, "doctrine_sha": doctrine_sha},
        status=status,
        created_at=T0 + timedelta(minutes=minutes),
    )


def run(energy_wh=None, co2e_g=None, run_id=None) -> Run:
    accounting = None
    if energy_wh is not None:
        accounting = {"co2e_g": co2e_g, "grid_co2e_g_per_kwh": 400.0}
    return Run(
        id=run_id or uuid.uuid4(),
        project_id=PROJECT_ID,
        harness_id=uuid.uuid4(),
        task_type="draft_section",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        energy_wh=Decimal(str(energy_wh)) if energy_wh is not None else None,
        energy_accounting=accounting,
    )


async def assemble(db, deliverable="tcfd-assessment", include_draft=False):
    return await assemble_deliverable(db, PROJECT_ID, deliverable, include_draft)


# ── only approved sections are exportable ────────────────────────────────────
async def test_an_empty_deliverable_assembles_to_nothing():
    result = await assemble(FakeSession())
    assert result == {"markdown": "", "sections": [], "html": ""}


async def test_a_deliverable_with_only_drafts_exports_nothing_by_default():
    """The blessing gate extends to the document: no approval, no export."""
    db = FakeSession([section("governance", status="draft")])
    result = await assemble(db)
    assert result["sections"] == []
    assert result["markdown"] == ""


async def test_include_draft_pulls_the_drafts_in_and_labels_the_document():
    db = FakeSession([section("governance", status="draft")])
    result = await assemble(db, include_draft=True)
    assert [s["section"] for s in result["sections"]] == ["governance"]
    assert result["sections"][0]["status"] == "draft"
    assert "approved+draft section(s)" in result["markdown"]


async def test_an_approved_only_document_says_it_is_approved_only():
    db = FakeSession([section("governance"), section("strategy", minutes=1)])
    result = await assemble(db)
    assert "from 2 approved section(s)" in result["markdown"]


async def test_a_rejected_section_is_never_exported(monkeypatch):
    db = FakeSession([section("governance", status="rejected")])
    assert (await assemble(db))["sections"] == []
    assert (await assemble(db, include_draft=True))["sections"] == []


async def test_another_projects_sections_are_not_assembled():
    db = FakeSession([section("governance", project_id=OTHER_PROJECT_ID)])
    assert (await assemble(db))["sections"] == []


async def test_another_deliverables_sections_are_not_assembled():
    db = FakeSession([section("governance", deliverable="csrd-report")])
    assert (await assemble(db))["sections"] == []


# ── ordering and the supersede rule ──────────────────────────────────────────
async def test_sections_appear_in_the_order_they_were_first_drafted():
    db = FakeSession(
        [
            section("governance", minutes=0),
            section("strategy", minutes=5),
            section("metrics", minutes=10),
        ]
    )
    result = await assemble(db)
    assert [s["section"] for s in result["sections"]] == ["governance", "strategy", "metrics"]
    body = result["markdown"]
    assert body.index("## Governance") < body.index("## Strategy") < body.index("## Metrics")


async def test_the_latest_finding_for_a_section_supersedes_the_earlier_one():
    old = section("governance", markdown="First attempt.", minutes=0)
    new = section("governance", markdown="Corrected figures.", minutes=30)
    result = await assemble(FakeSession([old, new]))
    assert len(result["sections"]) == 1
    assert result["sections"][0]["finding_id"] == str(new.id)
    assert "Corrected figures." in result["markdown"]
    assert "First attempt." not in result["markdown"]


async def test_a_superseding_section_keeps_its_original_position():
    """Re-drafting section one must not move it below section two."""
    db = FakeSession(
        [
            section("governance", minutes=0),
            section("strategy", minutes=1),
            section("governance", markdown="Redrafted.", minutes=9),
        ]
    )
    result = await assemble(db)
    assert [s["section"] for s in result["sections"]] == ["governance", "strategy"]


# ── the assembled markdown ───────────────────────────────────────────────────
async def test_the_document_is_titled_from_the_deliverable_slug():
    db = FakeSession([section("governance")])
    result = await assemble(db)
    assert result["markdown"].startswith("# Tcfd Assessment\n")


async def test_a_section_heading_the_model_repeated_is_not_printed_twice():
    db = FakeSession([section("governance", markdown="# Governance\n\nBody text.")])
    body = (await assemble(db))["markdown"]
    assert body.count("Governance") == 1
    assert "Body text." in body


async def test_a_different_heading_of_the_models_own_is_kept():
    db = FakeSession([section("governance", markdown="# Board oversight\n\nBody text.")])
    body = (await assemble(db))["markdown"]
    assert "## Governance" in body
    assert "# Board oversight" in body


async def test_the_html_rendering_carries_the_section_bodies():
    db = FakeSession([section("governance", markdown="Body with **emphasis**.")])
    html = (await assemble(db))["html"]
    assert "<strong>emphasis</strong>" in html
    assert "<h2>Governance</h2>" in html


# ── provenance per section ───────────────────────────────────────────────────
async def test_each_section_reports_its_model_status_and_doctrine():
    db = FakeSession([section("governance")])
    meta = (await assemble(db))["sections"][0]
    assert meta["model"] == "anthropic/test-model"
    assert meta["status"] == "approved"
    assert meta["doctrine_sha"] == "abcdef012345"  # truncated for display
    assert meta["run_id"] is None


async def test_a_section_with_no_provenance_reports_nulls_not_guesses():
    bare = section("governance")
    bare.provenance = {}
    meta = (await assemble(FakeSession([bare])))["sections"][0]
    assert meta["model"] is None
    assert meta["doctrine_sha"] == ""


# ── the footprint line ───────────────────────────────────────────────────────
async def test_the_footprint_sums_distinct_runs_only_once():
    """Two sections drafted by one run must not be counted twice."""
    shared = uuid.uuid4()
    db = FakeSession(
        findings=[
            section("governance", run_id=shared),
            section("strategy", run_id=shared, minutes=1),
        ],
        runs=[run(energy_wh=2.0, co2e_g=0.8, run_id=shared)],
    )
    result = await assemble(db)
    assert result["energy"]["runs"] == 1
    assert result["energy"]["energy_wh"] == pytest.approx(2.0)
    assert result["energy"]["co2e_g"] == pytest.approx(0.8)
    assert "2 Wh" in result["markdown"]


async def test_a_run_without_an_estimate_is_counted_as_missing_not_as_zero():
    with_estimate, without = uuid.uuid4(), uuid.uuid4()
    db = FakeSession(
        findings=[
            section("governance", run_id=with_estimate),
            section("strategy", run_id=without, minutes=1),
        ],
        runs=[run(energy_wh=3.0, co2e_g=1.0, run_id=with_estimate), run(run_id=without)],
    )
    footprint = (await assemble(db))["energy"]
    assert footprint["runs"] == 1
    assert footprint["runs_without_estimate"] == 1
    assert footprint["energy_wh"] == pytest.approx(3.0)


async def test_no_estimate_anywhere_means_no_footprint_claim():
    """A document with nothing to report says nothing rather than "0 Wh"."""
    only = uuid.uuid4()
    db = FakeSession(findings=[section("governance", run_id=only)], runs=[run(run_id=only)])
    result = await assemble(db)
    assert result["energy"]["energy_wh"] is None
    assert "compute footprint" not in result["markdown"]
    assert result["sections"][0]["energy_wh"] is None


async def test_the_footprint_is_labelled_an_estimate():
    only = uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=only)],
        runs=[run(energy_wh=1.0, co2e_g=0.4, run_id=only)],
    )
    result = await assemble(db)
    assert result["energy"]["estimated"] is True
    assert "not a measurement" in result["markdown"]
    assert "Heuristic estimate" in result["markdown"]


# ── the PDF provenance appendix ──────────────────────────────────────────────
class _CapturingHTML:
    captured: dict = {}

    def __init__(self, string=None, url_fetcher=None, **_kw):
        _CapturingHTML.captured = {"string": string, "url_fetcher": url_fetcher}

    def write_pdf(self):
        return b"%PDF-1.7 fake"


@pytest.fixture()
def fake_weasyprint(monkeypatch):
    module = types.ModuleType("weasyprint")
    module.HTML = _CapturingHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    _CapturingHTML.captured = {}
    return module


async def _rendered_pdf_document(db, include_draft=False) -> str:
    result = await assemble(db, include_draft=include_draft)
    render_pdf(result["html"], "tcfd-assessment", result["sections"], None)
    return _CapturingHTML.captured["string"]


async def test_the_provenance_appendix_names_every_section_and_its_status(fake_weasyprint):
    db = FakeSession([section("governance"), section("strategy", minutes=1)])
    document = await _rendered_pdf_document(db)
    assert document.count("<td>approved</td>") == 2
    assert "<td>governance</td>" in document
    assert "<td>strategy</td>" in document
    assert "<th>Status</th>" in document


async def test_a_pdf_containing_a_draft_says_which_section_is_a_draft(fake_weasyprint):
    """Otherwise `include_draft` produces a document that reads as fully blessed."""
    db = FakeSession([section("governance"), section("strategy", status="draft", minutes=1)])
    document = await _rendered_pdf_document(db, include_draft=True)
    assert "<td>governance</td><td>approved</td>" in document.replace("\n", "")
    assert "<td>strategy</td><td>draft</td>" in document.replace("\n", "")


async def test_the_appendix_reports_an_unknown_footprint_as_a_dash(fake_weasyprint):
    only = uuid.uuid4()
    db = FakeSession(findings=[section("governance", run_id=only)], runs=[run(run_id=only)])
    document = await _rendered_pdf_document(db)
    assert "—" in document
    assert "estimate" in document


async def test_the_appendix_reports_a_known_footprint_with_its_carbon(fake_weasyprint):
    only = uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=only)],
        runs=[run(energy_wh=1.25, co2e_g=0.4, run_id=only)],
    )
    document = await _rendered_pdf_document(db)
    assert "~1.25 Wh" in document
    assert "0.4 gCO2e" in document


def test_an_empty_deliverable_still_renders_a_valid_appendix(fake_weasyprint):
    assert render_pdf("<p>body</p>", "tcfd-assessment", [], None) == b"%PDF-1.7 fake"
    document = _CapturingHTML.captured["string"]
    assert "<h2>Provenance</h2>" in document
    assert "Pack doctrine hash" not in document  # nothing to cite


def test_the_doctrine_hash_is_cited_when_every_section_shares_one(fake_weasyprint):
    render_pdf("<p>body</p>", "tcfd-assessment", [], "abcdef0123456789abcdef")
    assert "abcdef0123456789" in _CapturingHTML.captured["string"]


# ── the endpoint around it ───────────────────────────────────────────────────
def _client(db) -> TestClient:
    app = FastAPI()
    app.include_router(findings_api.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_exporting_a_deliverable_with_no_approved_sections_is_a_404():
    client = _client(FakeSession([section("governance", status="draft")]))
    response = client.get("/api/deliverables/tcfd-assessment/export")
    assert response.status_code == 404
    assert "No approved sections" in response.json()["detail"]


def test_exporting_an_unknown_deliverable_is_a_404():
    client = _client(FakeSession([section("governance")]))
    assert client.get("/api/deliverables/nope/export").status_code == 404


def test_markdown_is_the_default_format_and_is_served_as_text():
    client = _client(FakeSession([section("governance")]))
    response = client.get("/api/deliverables/tcfd-assessment/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.text.startswith("# Tcfd Assessment")


def test_the_json_format_carries_the_sections_and_the_footprint():
    client = _client(FakeSession([section("governance")]))
    body = client.get("/api/deliverables/tcfd-assessment/export?format=json").json()
    assert [s["section"] for s in body["sections"]] == ["governance"]
    assert body["energy"]["estimated"] is True
    assert "markdown" in body and "html" in body


def test_an_unknown_export_format_is_refused():
    """Falling through to markdown for `format=docx` looks like success."""
    client = _client(FakeSession([section("governance")]))
    response = client.get("/api/deliverables/tcfd-assessment/export?format=docx")
    assert response.status_code == 422


def test_the_pdf_response_is_an_attachment_named_after_the_deliverable(fake_weasyprint):
    client = _client(FakeSession([section("governance")]))
    response = client.get("/api/deliverables/tcfd-assessment/export?format=pdf")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == 'attachment; filename="tcfd-assessment.pdf"'
    assert response.content == b"%PDF-1.7 fake"


def test_an_anonymous_request_cannot_export():
    app = FastAPI()
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: FakeSession([section("governance")])
    client = TestClient(app)
    assert client.get("/api/deliverables/tcfd-assessment/export").status_code == 401
    assert client.get("/api/deliverables").status_code == 401


# ── the deliverables listing the UI drives the export from ───────────────────
def test_the_listing_counts_approved_and_draft_sections():
    db = FakeSession(
        [
            section("governance"),
            section("strategy", status="draft", minutes=1),
            section("metrics", status="draft", minutes=2),
        ]
    )
    listed = _client(db).get("/api/deliverables").json()
    assert len(listed) == 1
    assert listed[0]["slug"] == "tcfd-assessment"
    assert listed[0]["approved_count"] == 1
    assert listed[0]["draft_count"] == 2
    assert {s["section"] for s in listed[0]["sections"]} == {"governance", "strategy", "metrics"}


def test_the_listing_shows_the_latest_state_of_a_redrafted_section():
    db = FakeSession(
        [
            section("governance", status="approved", minutes=0),
            section("governance", status="draft", minutes=5),  # redrafted after approval
        ]
    )
    listed = _client(db).get("/api/deliverables").json()
    assert listed[0]["approved_count"] == 0
    assert listed[0]["draft_count"] == 1


def test_the_listing_does_not_offer_another_projects_deliverables():
    """It used to span every project while the export read only one, so the UI
    could offer a deliverable whose export could only 404."""
    db = FakeSession(
        [
            section("governance", deliverable="ours"),
            section("governance", deliverable="theirs", project_id=OTHER_PROJECT_ID),
        ]
    )
    listed = _client(db).get("/api/deliverables").json()
    assert [d["slug"] for d in listed] == ["ours"]


def test_the_listing_is_empty_before_a_project_exists():
    assert _client(FakeSession(projects=[])).get("/api/deliverables").json() == []
