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
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Finding, Project, Run, Workspace
from tret.services.export import assemble_deliverable, render_pdf, strip_draft_status_banner

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


def run(
    energy_wh=None, co2e_g=None, run_id=None, energy_boundary=None, method_id=None,
    grid_co2e_g_per_kwh: float = 400.0,
) -> Run:
    accounting = None
    if energy_wh is not None:
        accounting = {"co2e_g": co2e_g, "grid_co2e_g_per_kwh": grid_co2e_g_per_kwh,
                      "grid_co2e_basis": "location_based",
                      "grid_factor_boundary": "lifecycle_electricity_generation",
                      "grid_gas_coverage": "co2e", "grid_gwp_horizon_years": 100,
                      "grid_gwp_assessment_basis": "ar6",
                      "grid_includes_td_losses": False,
                      "grid_electricity_mix_basis": "production"}
        # `energy_boundary` is deliberately absent unless a test asks for it — a
        # missing key is exactly what a pre-batch (legacy) run looks like; see
        # the footprint tests below (F5).
        if energy_boundary is not None:
            accounting["energy_boundary"] = energy_boundary
        if method_id is not None:
            accounting["method_id"] = method_id
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


def legacy_run(
    energy_wh, co2e_g, run_id=None,
    grid_co2e_g_per_kwh: float = 470.0, grid_co2e_basis: str = "location_based",
) -> Run:
    """A pre-provenance run: only the grid value and basis are recorded — none
    of the boundary/gas-coverage/GWP/mix metadata a v2 run carries, and no
    `grid_co2e_layer`. Same shape as the live 2026-08-28 runs behind defect 3."""
    return Run(
        id=run_id or uuid.uuid4(),
        project_id=PROJECT_ID,
        harness_id=uuid.uuid4(),
        task_type="draft_section",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        energy_wh=Decimal(str(energy_wh)),
        energy_accounting={
            "co2e_g": co2e_g,
            "grid_co2e_g_per_kwh": grid_co2e_g_per_kwh,
            "grid_co2e_basis": grid_co2e_basis,
        },
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


# ── a stale "draft status" banner never ships ────────────────────────────────
DRAFT_BANNER = (
    "**Draft status:** This section is a draft awaiting review by a named "
    "human reviewer before publication."
)


def test_strip_draft_status_banner_removes_a_bold_leading_paragraph():
    markdown = f"{DRAFT_BANNER}\n\nActual section content."
    assert strip_draft_status_banner(markdown) == "Actual section content."


def test_strip_draft_status_banner_removes_a_plain_leading_paragraph():
    markdown = "Draft status: still awaiting review.\n\nActual section content."
    assert strip_draft_status_banner(markdown) == "Actual section content."


def test_strip_draft_status_banner_leaves_other_content_untouched():
    markdown = "# Governance\n\nBody text with no banner at all."
    assert strip_draft_status_banner(markdown) == markdown


def test_strip_draft_status_banner_removes_a_banner_under_the_section_heading():
    # The live case: the section opens with its own heading, then the banner.
    markdown = f"## Metrics and Targets\n\n{DRAFT_BANNER}\n\nNo evidence on climate-risk metrics."
    assert strip_draft_status_banner(markdown) == (
        "## Metrics and Targets\n\nNo evidence on climate-risk metrics."
    )


def test_strip_draft_status_banner_never_blanks_a_body():
    # Banner and body separated by a single newline: only the banner line goes.
    markdown = f"{DRAFT_BANNER}\nThe flood exposure at Alder Point is material."
    assert strip_draft_status_banner(markdown) == "The flood exposure at Alder Point is material."
    # A banner with nothing after it is left alone rather than emptied.
    assert strip_draft_status_banner(DRAFT_BANNER) == DRAFT_BANNER
    assert strip_draft_status_banner(f"{DRAFT_BANNER}\n\n   ") == f"{DRAFT_BANNER}\n\n   "


def test_strip_draft_status_banner_handles_none_and_empty():
    assert strip_draft_status_banner(None) is None
    assert strip_draft_status_banner("") == ""


async def test_an_approved_sections_draft_banner_is_stripped_at_assembly():
    # Defensive stripping at assembly: a section approved before this
    # stripping existed must still never ship the banner in an export.
    db = FakeSession([section("governance", markdown=f"{DRAFT_BANNER}\n\nReal content.")])
    body = (await assemble(db))["markdown"]
    assert "Draft status" not in body
    assert "Real content." in body


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


# ── F5: a multi-run legacy deliverable must not lose its footprint paragraph ──
async def test_two_legacy_runs_still_sum_with_a_qualifier():
    """Every pre-batch run lacks `energy_boundary` entirely. Two of them behind
    one deliverable used to drop the whole footprint paragraph; they must now
    sum, with a sentence naming the boundary as legacy and unresolved."""
    a, b = uuid.uuid4(), uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=a), section("strategy", run_id=b, minutes=1)],
        runs=[
            run(energy_wh=2.0, co2e_g=0.8, run_id=a),
            run(energy_wh=3.0, co2e_g=1.2, run_id=b),
        ],
    )
    result = await assemble(db)
    footprint = result["energy"]
    assert footprint["runs"] == 2
    assert footprint["energy_wh"] == pytest.approx(5.0)
    assert footprint["energy_boundary_legacy_qualifier"] is True
    assert "compute footprint" in result["markdown"]
    assert "5 Wh" in result["markdown"]
    assert "legacy, unresolved" in result["markdown"]


async def test_a_legacy_run_and_a_v2_run_withhold_the_sum_but_keep_the_paragraph():
    """One pre-batch run plus one boundary-labelled `node_it` run: the runs
    genuinely disagree on energy boundary, so the combined figure is withheld
    — but the paragraph must still appear, with per-boundary subtotals, rather
    than being dropped as it was before F5."""
    legacy, v2 = uuid.uuid4(), uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=legacy), section("strategy", run_id=v2, minutes=1)],
        runs=[
            run(energy_wh=2.0, co2e_g=0.8, run_id=legacy),
            run(energy_wh=4.0, co2e_g=1.6, run_id=v2, energy_boundary="node_it", method_id="class_ladder_v2"),
        ],
    )
    result = await assemble(db)
    footprint = result["energy"]
    assert footprint["runs"] == 2
    assert footprint["energy_wh"] is None
    assert footprint["energy_boundary_legacy_qualifier"] is False
    subtotals = {s["boundary"]: (s["energy_wh"], s["runs"]) for s in footprint["energy_boundary_subtotals"]}
    assert subtotals["legacy_unresolved"] == (2.0, 1)
    assert subtotals["node_it"] == (4.0, 1)
    assert "compute footprint" in result["markdown"]
    assert "withheld" in result["markdown"]
    assert "node_it" in result["markdown"] and "legacy_unresolved" in result["markdown"]


# ── defect 3: legacy rows on one identical grid factor must sum carbon ───────
async def test_three_legacy_runs_on_one_identical_factor_sum_carbon():
    """Live bug: a 3-run deliverable, all priced at 470 gCO2e/kWh
    location_based with no boundary/dataset-version provenance, showed
    "5.2 Wh · — at 470 g/kWh, over 3 runs" — carbon withheld even though
    every row used the identical factor. Missing provenance is not a reason
    to treat identical factors as incompatible (see grid_comparison_signature)."""
    a, b, c = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = FakeSession(
        findings=[
            section("governance", run_id=a),
            section("strategy", run_id=b, minutes=1),
            section("risk", run_id=c, minutes=2),
        ],
        runs=[
            legacy_run(1.0, 0.47, run_id=a),
            legacy_run(2.0, 0.94, run_id=b),
            legacy_run(2.0, 0.94, run_id=c),
        ],
    )
    result = await assemble(db)
    footprint = result["energy"]
    assert footprint["runs"] == 3
    assert footprint["energy_wh"] == pytest.approx(5.0)
    assert footprint["co2e_g"] == pytest.approx(2.35)
    assert footprint["carbon_compatible"] is True
    assert footprint["grid_co2e_g_per_kwh"] == 470.0
    assert "gCO2e" in result["markdown"]


async def test_legacy_run_and_a_different_factor_withhold_carbon_with_subtotals():
    """A legacy 470 gCO2e/kWh row and a new-provenance 458.49 gCO2e/kWh row are
    genuinely different factors, so carbon stays withheld — the paragraph is
    still printed, energy still sums (same basis, no boundary conflict)."""
    legacy, new = uuid.uuid4(), uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=legacy), section("strategy", run_id=new, minutes=1)],
        runs=[
            legacy_run(1.0, 0.47, run_id=legacy),
            run(energy_wh=2.0, co2e_g=0.91698, run_id=new, grid_co2e_g_per_kwh=458.49),
        ],
    )
    result = await assemble(db)
    footprint = result["energy"]
    assert footprint["runs"] == 2
    assert footprint["energy_wh"] == pytest.approx(3.0)
    assert footprint["co2e_g"] is None
    assert footprint["carbon_compatible"] is False
    assert "combined carbon withheld" in result["markdown"]


async def test_a_single_legacy_run_sums_carbon_same_as_before():
    """A lone legacy run has nothing to disagree with, so its carbon has
    always summed — this must remain true after the signature-based fix."""
    only = uuid.uuid4()
    db = FakeSession(
        findings=[section("governance", run_id=only)],
        runs=[legacy_run(1.0, 0.47, run_id=only)],
    )
    result = await assemble(db)
    footprint = result["energy"]
    assert footprint["runs"] == 1
    assert footprint["co2e_g"] == pytest.approx(0.47)
    assert footprint["carbon_compatible"] is True


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

    # The workspace this fake db's project actually belongs to — read off the
    # fixture rather than a fixed id, since callers construct `Project` (and
    # therefore its `workspace_id`) fresh per FakeSession.
    def _fake_current_workspace():
        projects = db.rows.get("Project") or []
        workspace_id = projects[0].workspace_id if projects else uuid.uuid4()
        return WorkspaceContext(Workspace(id=workspace_id, name="W", kind="team"), "owner")

    app.dependency_overrides[current_workspace] = _fake_current_workspace
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
