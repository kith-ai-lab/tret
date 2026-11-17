"""The batch evaluator's evidence for page maps of an ESEF filing (filing pages or
sections) and, unchanged, for a PDF map."""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from tret.batches.profiles import disclosure as d

PDF_TEXT = "## Page 1\nCover\n\n## Page 2\nSecond page body\n\n## Page 3\nThird page body\n"
SECTION_TEXT = (
    "## Section 1: Introduction\nIntro body\n\n"
    "## Section 2: Sustainability statement\nESRS E1 climate body text\n\n"
    "## Section 3: Assurance report\nLimited assurance body\n"
)
FILING_PAGE_TEXT = "## Page 1\nCover\n\n## Page 2\nFiling page two body\n\n## Page 3\nFiling page three\n"


class FakeDb:
    def __init__(self, docs):
        self.docs = docs

    async def get(self, _model, key):
        return self.docs.get(key)


def _doc(text, filename):
    return SimpleNamespace(
        id=uuid.uuid4(), extracted_text=text, filename=filename, sha256="a" * 64, meta={}, content_type="text/plain",
    )


def _finding(source_doc, *, unit=None, keep=((2, 3),), package=None, source_extra=None):
    m = {"title": "T", "role": "primary_disclosure", "scope": "embedded", "keep_ranges": [list(r) for r in keep]}
    if unit:
        m["unit"] = unit
    source = {"document_id": str(source_doc.id), "url": "https://x.example/r", "page_count": 3,
              "filename": source_doc.filename, **(source_extra or {})}
    if unit:
        source["unit"] = unit
    return SimpleNamespace(
        id=uuid.uuid4(), schema_slug="package_map", status="approved",
        payload={"stem": "acme", "map": m, "source": source, "package": package},
    )


def _parts(source_doc, finding):
    db = FakeDb({source_doc.id: source_doc})
    target = SimpleNamespace(status="completed", error=None, outcome={"status": "captured", "summary": "ok"})
    return asyncio.run(d._run_parts(db, target, None, [finding], "success"))


def test_unit_defaults_to_pdf_page_and_ignores_unknown_values():
    assert d._unit_of({"map": {}, "source": {}}) == "pdf_page"
    assert d._unit_of({"map": {"unit": "weird"}}) == "pdf_page"
    assert d._unit_of({"map": {"unit": "filing_section"}}) == "filing_section"
    assert d._unit_of({"source": {"unit": "filing_page"}}) == "filing_page"


def test_excerpts_read_page_and_section_markers():
    assert d._page_excerpt(PDF_TEXT, 2, 400).startswith("Second page body")
    assert d._page_excerpt(FILING_PAGE_TEXT, 2, 400, "filing_page").startswith("Filing page two body")
    assert d._page_excerpt(SECTION_TEXT, 2, 400, "filing_section").startswith("[Sustainability statement] ESRS E1")
    assert d._page_excerpt(SECTION_TEXT, 9, 400, "filing_section") is None
    # a section marker is not a page marker, and the reverse
    assert d._page_excerpt(SECTION_TEXT, 2, 400) is None
    assert d._page_excerpt(PDF_TEXT, 2, 400, "filing_section") is None


def test_pdf_map_evidence_is_unchanged():
    src = _doc(PDF_TEXT, "r.pdf")
    f = _finding(src, package={"status": "packaged", "retained_page_count": 2, "documents": [], "limitations": []})
    result, blocks, excerpts, _ = _parts(src, f)
    block = blocks[0]
    assert "source pages: 3" in block and "keep ranges (page numbers): [[2, 3]]" in block
    assert "retained pages: 2" in block and "ESEF" not in block and "- retained:" not in block
    assert any(e.startswith("### acme: first retained page (page 2)\nSecond page body") for e in excerpts)
    assert "### acme: last retained page (page 3)\nThird page body" in excerpts


def test_filing_page_map_evidence_names_the_unit_and_the_limitation():
    src = _doc(FILING_PAGE_TEXT, "r.zip")
    pkg = {"status": "packaged", "retained_page_count": 2, "retained_unit_count": 2, "documents": [],
           "limitations": ["regulated ESEF filing, not the issuer's own PDF; units are filing pages/sections"]}
    f = _finding(src, unit="filing_page", package=pkg,
                 source_extra={"filing": {"filing_url": "https://filings.xbrl.org/x"}})
    _, blocks, excerpts, _ = _parts(src, f)
    block = blocks[0]
    assert "source filing pages: 3" in block
    assert "keep ranges (filing page numbers): [[2, 3]]" in block
    assert "retained filing pages: 2" in block
    assert "- retained: filing pages 2–3 (of 3)" in block
    assert "regulated ESEF filing (https://filings.xbrl.org/x), not the issuer's own PDF" in block
    assert "no PDF is retained" in block and "not the issuer's own PDF; units are filing pages" in block
    assert any(e.startswith("### acme: first retained filing page (filing page 2)\nFiling page two") for e in excerpts)


def test_filing_section_map_evidence_shows_headings():
    src = _doc(SECTION_TEXT, "r.zip")
    pkg = {"status": "packaged", "retained_page_count": 2, "documents": [], "limitations": []}
    f = _finding(src, unit="filing_section", keep=((2, 3),), package=pkg)
    _, blocks, excerpts, _ = _parts(src, f)
    block = blocks[0]
    assert "source sections: 3" in block and "retained sections: 2" in block
    assert "- retained: sections 2–3 (of 3): Sustainability statement … Assurance report" in block
    assert any(e.startswith("### acme: first retained section (section 2)\n[Sustainability statement]") for e in excerpts)
    assert any(e.startswith("### acme: last retained section (section 3)\n[Assurance report]") for e in excerpts)


def test_an_unpackaged_filing_map_does_not_report_zeros():
    src = _doc(SECTION_TEXT, "r.zip")
    f = _finding(src, unit="filing_section", package=None)
    _, blocks, _, _ = _parts(src, f)
    assert "packaging: not packaged; retained sections: None" in blocks[0]


def test_summaries_and_records_stay_pdf_shaped_unless_a_filing():
    src = _doc(SECTION_TEXT, "r.zip")
    pkg = {"status": "packaged", "retained_page_count": 2, "retained_sha256": "x", "documents": []}
    filing = d.package_summaries([_finding(src, unit="filing_section", package=pkg)])[0]
    pdf = d.package_summaries([_finding(src, package=pkg)])[0]
    assert filing["unit"] == "filing_section" and filing["retained_page_count"] == 2
    assert "unit" not in pdf


def _evidence(source_doc, finding):
    db = FakeDb({source_doc.id: source_doc})
    target = SimpleNamespace(
        status="completed", error=None, outcome={"status": "captured", "summary": "ok"}, company="Acme",
        market="EU", domicile_country=None, sector=None, category=None, expected_form="embedded",
        fiscal_year=2025, known_urls=[], notes=None, identifiers=None, slug="acme", run_id=None,
        written_files=[],
    )
    return asyncio.run(d.build_evidence(db, SimpleNamespace(), target, None, [finding]))[0]


def test_the_pack_carries_the_filing_unit_note_only_for_filing_maps():
    src = _doc(SECTION_TEXT, "r.zip")
    pkg = {"status": "packaged", "retained_page_count": 2, "documents": [], "limitations": []}
    text = _evidence(src, _finding(src, unit="filing_section", package=pkg))
    assert "## Filing units" in text and "filing pages or sections" in text
    assert text.index("## Result") < text.index("## Filing units") < text.index("## Page maps")
    pdf_src = _doc(PDF_TEXT, "r.pdf")
    assert "## Filing units" not in _evidence(pdf_src, _finding(pdf_src, package=pkg))
