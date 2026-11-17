"""Page maps and packaging by filing unit (ESEF fallback, stage 2)."""
# ruff: noqa: F811
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from tests.test_esef_filing import make_esef_zip, make_paged_report, make_section_report
from tests.test_package_map import EMBEDDED, attached, ctx, db  # noqa: F401
from tret.db.models import Document, Finding
from tret.engine.tools import ToolError, propose_package_map
from tret.services import packaging as pk
from tret.services.packaging import PackageMap, PackageMapError, PackagingError, validate_map

URL = "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme-2025.zip"


async def _filing_doc(ctx, tmp_path: Path, report: bytes) -> Document:
    data = make_esef_zip(report)
    path = tmp_path / "acme-2025.zip"
    path.write_bytes(data)
    a = pk.analyse_filing(path, max_chars=2_000_000)
    doc = Document(
        project_id=ctx.project_id, filename="acme-2025.zip", content_type="application/zip",
        byte_size=len(data), storage_path=str(path), extraction_status="done", extracted_text=a["text"],
        sha256=hashlib.sha256(data).hexdigest(), source_kind="web",
        meta={"provenance": "esef_filing", "url": URL, "package_url": URL, "filing_url": URL,
              "lei": "529900T8BM49AURSDO55", "report_member": a["member"]["name"],
              "report_member_sha256": a["member"]["sha256"], "layout": a["layout"], "unit": a["unit"],
              "unit_count": a["unit_count"], "unit_text_sha256": a["unit_text_sha256"]},
    )
    ctx.db.add(doc)
    await ctx.db.flush()
    ctx.document_ids.append(doc.id)
    return doc


KW = dict(title="Sustainability statement", role="embedded_statement_and_assurance", scope="embedded")


def test_validate_map_generalises_bounds_without_changing_pdf_wording():
    m = PackageMap(title="t", role="other", scope="embedded", keep_ranges=((2, 9),))
    with pytest.raises(PackageMapError, match=r"source has 6 pages"):
        validate_map(m, 6)
    f = PackageMap(title="t", role="other", scope="embedded", keep_ranges=((2, 9),), unit="filing_section")
    with pytest.raises(PackageMapError, match=r"filing has 6 sections"):
        validate_map(f, 6)
    assert validate_map(PackageMap("t", "other", "embedded", ((2, 3),), unit="filing_page"), 6) == [2, 3]
    with pytest.raises(PackageMapError, match="ocr applies to PDF"):
        validate_map(PackageMap("t", "other", "embedded", ((2, 3),), ocr="auto", unit="filing_page"), 6)
    with pytest.raises(PackageMapError, match="unit must be"):
        validate_map(PackageMap("t", "other", "embedded", ((2, 3),), unit="slide"), 6)


async def test_pdf_payload_is_unchanged_and_has_no_unit(ctx, attached):
    await propose_package_map(ctx, source_document_id=str(attached.id), **EMBEDDED)
    f = (await ctx.db.execute(select(Finding))).scalars().one()
    assert "unit" not in f.payload["map"] and "unit" not in f.payload["source"]
    assert "filing" not in f.payload["source"] and f.payload["source"]["page_count"] == 6


async def test_paged_filing_proposal(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_paged_report(6))
    out = await propose_package_map(ctx, source_document_id=str(doc.id), keep_ranges=[[2, 4]], **KW)
    f = (await ctx.db.execute(select(Finding))).scalars().one()
    assert f.payload["map"]["unit"] == "filing_page" and f.payload["source"]["unit_count"] == 6
    assert f.payload["source"]["page_count"] == 6 and f.payload["source"]["filing"]["layout"] == "paged"
    assert f.payload["selected_page_count"] == 3
    assert "regulated filing, not the issuer's own PDF" in out
    assert 'filing page 2 opens "Body of page 2' in out and "filing page 4" in out
    with pytest.raises(ToolError, match="This filing has 6 pages"):
        await propose_package_map(ctx, source_document_id=str(doc.id), keep_ranges=[[2, 9]], **KW)
    with pytest.raises(ToolError, match="ocr applies to PDF"):
        await propose_package_map(ctx, source_document_id=str(doc.id), keep_ranges=[[2, 4]], ocr="auto", **KW)


async def test_section_filing_proposal_quotes_headings(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_section_report(5))
    out = await propose_package_map(ctx, source_document_id=str(doc.id), keep_ranges=[[3, 5]], **KW)
    f = (await ctx.db.execute(select(Finding))).scalars().one()
    assert f.payload["map"]["unit"] == "filing_section"
    assert 'section 3 is "Chapter 2 title"; section 5 is "Chapter 3 title"' in out and "(3 sections)" in out


async def test_a_filing_document_without_layout_meta_is_refused(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_paged_report(6))
    doc.meta = {"provenance": "esef_filing"}
    with pytest.raises(ToolError, match="no recorded layout"):
        await propose_package_map(ctx, source_document_id=str(doc.id), keep_ranges=[[2, 4]], **KW)


def _source(doc, unit_count, layout):
    m = doc.meta
    return {"document_id": str(doc.id), "sha256": doc.sha256, "url": URL, "unit": m["unit"],
            "unit_count": unit_count,
            "filing": {k: m[k] for k in ("lei", "filing_url", "package_url", "report_member",
                                         "report_member_sha256", "layout", "unit_text_sha256")}}


@pytest.mark.parametrize("kind", ["paged", "sections"])
async def test_package_filing_writes_verified_files(ctx, tmp_path, kind):
    report = make_paged_report(6) if kind == "paged" else make_section_report(5)
    doc = await _filing_doc(ctx, tmp_path, report)
    mapping = PackageMap("Statement", "primary_disclosure", "embedded", ((2, 3),), unit=doc.meta["unit"])
    out = pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "out", stem="acme",
                            source_meta=_source(doc, doc.meta["unit_count"], kind))
    names = sorted(f.filename for f in out.files)
    assert names == ["acme.extract.html", "acme.extraction_stats.json", "acme.layout.md",
                     "acme.package.json", "acme.page_map.json"]
    assert not list((tmp_path / "out").glob("*.pdf"))
    layout = (tmp_path / "out/acme.layout.md").read_text()
    marker = "## Page 2\n" if kind == "paged" else "## Section 2: "
    assert marker in layout and ("## Page 1\n" not in layout if kind == "paged" else "## Section 1: " not in layout)
    html = (tmp_path / "out/acme.extract.html").read_text()
    assert "<script" not in html and "SECRET-FACT" not in html
    rec = json.loads((tmp_path / "out/acme.package.json").read_text())
    assert rec["unit"] == doc.meta["unit"] and rec["keep_ranges"] == [[2, 3]]
    assert rec["source_sha256"] == doc.sha256 and rec["report_member_sha256"] == doc.meta["report_member_sha256"]
    assert rec["lei"] == "529900T8BM49AURSDO55" and rec["filing_url"] == URL and rec["retained_unit_count"] == 2
    assert any("regulated ESEF filing, not the issuer's own PDF; units are filing pages/sections" in x
               for x in rec["limitations"])
    assert rec["verification"]["oracle"] == "text_readback"
    pm = json.loads((tmp_path / "out/acme.page_map.json").read_text())
    assert [u["source_unit"] for u in pm["units"]] == [2, 3]
    if kind == "paged":
        assert pm["units"][0]["printed_folio"] == 2
    else:
        assert pm["units"][0]["heading"] == "Sub 1.1"
    assert out.retained_page_count == 2 and out.source_page_count == doc.meta["unit_count"]
    assert out.text_mode == "esef_html" and {f.role for f in out.files} >= {"extract_html", "layout_text"}


async def test_package_filing_refuses_changed_sources_and_existing_files(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_paged_report(6))
    mapping = PackageMap("S", "other", "embedded", ((2, 3),), unit="filing_page")
    src = _source(doc, 6, "paged")
    with pytest.raises(PackagingError, match="source bytes changed"):
        pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "o1", stem="a",
                          source_meta={**src, "sha256": "0" * 64})
    with pytest.raises(PackagingError, match="report inside the package changed"):
        pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "o2", stem="a",
                          source_meta={**src, "filing": {**src["filing"], "report_member_sha256": "1" * 64}})
    with pytest.raises(PackagingError, match="now reads as"):
        pk.package_filing(Path(doc.storage_path), PackageMap("S", "other", "embedded", ((2, 3),), unit="filing_section"),
                          tmp_path / "o3", stem="a", source_meta=src)
    with pytest.raises(PackageMapError, match="Out-of-bounds"):
        pk.package_filing(Path(doc.storage_path), PackageMap("S", "other", "embedded", ((2, 9),), unit="filing_page"),
                          tmp_path / "o4", stem="a", source_meta=src)
    pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "o5", stem="a", source_meta=src)
    with pytest.raises(PackagingError, match="refusing to overwrite"):
        pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "o5", stem="a", source_meta=src)
    assert not (tmp_path / "o1").exists()


async def test_run_packaging_dispatches_a_filing_map_in_the_worker(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_paged_report(6))
    mapping = PackageMap("S", "other", "embedded", ((1, 2),), unit="filing_page")
    out = pk.run_packaging(Path(doc.storage_path), mapping, tmp_path / "w", stem="w",
                           source_meta=_source(doc, 6, "paged"), deadline_seconds=120)
    assert out.text_mode == "esef_html" and out.retained_page_count == 2
    assert (tmp_path / "w/w.layout.md").is_file()


# ── approval -> packaging -> ingest ─────────────────────────────────────────────
from tests.test_package_map import (  # noqa: E402,F401
    _package_docs, client, decide, login_as, members, people, storage, world,
)


def test_approving_a_filing_map_packages_and_ingests_each_file(client, world, people, tmp_path):
    import uuid

    data = make_esef_zip(make_paged_report(6))
    path = tmp_path / "captures" / "acme.zip"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    a = pk.analyse_filing(path, max_chars=2_000_000)
    sha = hashlib.sha256(data).hexdigest()
    src = Document(id=uuid.uuid4(), project_id=world.source.project_id, filename="acme.zip",
                   content_type="application/zip", byte_size=len(data), storage_path=str(path), sha256=sha,
                   source_kind="web", meta={"provenance": "esef_filing"})
    world.db.store["Document"] = {src.id: src}
    m = {"report_member": a["member"]["name"], "report_member_sha256": a["member"]["sha256"], "layout": "paged",
         "lei": "L", "filing_url": URL, "package_url": URL, "unit_text_sha256": a["unit_text_sha256"]}
    world.finding.payload = {
        **world.finding.payload,
        "source": {"document_id": str(src.id), "sha256": sha, "url": URL, "unit": "filing_page",
                   "unit_count": 6, "page_count": 6, "filing": m},
        "map": {**world.finding.payload["map"], "keep_ranges": [[2, 3]], "unit": "filing_page", "ocr": "off"},
        "stem": "acme",
    }
    login_as(client, people["approver"])
    assert decide(client, world.finding.id, action="approve").status_code == 200
    package = world.finding.payload["package"]
    assert package["status"] == "packaged", package["error"]
    assert package["text_mode"] == "esef_html" and package["retained_page_count"] == 2
    docs = {d.meta["package_role"]: d for d in _package_docs(world)}
    assert set(docs) == {"extract_html", "layout_text", "page_map", "extraction_stats", "package_record"}
    assert docs["extract_html"].content_type.startswith("text/html")
    assert docs["extract_html"].extraction_status == "skipped" and docs["extract_html"].extracted_text is None
    assert docs["layout_text"].extracted_text.startswith("# S") and "## Page 2\n" in docs["layout_text"].extracted_text
    assert docs["layout_text"].meta["unit"] == "filing_page"


async def test_package_filing_fails_closed_on_a_unit_hash_mismatch(ctx, tmp_path):
    doc = await _filing_doc(ctx, tmp_path, make_paged_report(6))
    src = _source(doc, 6, "paged")
    hashes = list(src["filing"]["unit_text_sha256"])
    hashes[1] = "0" * 64  # unit 2 differs from what was captured
    bad = {**src, "filing": {**src["filing"], "unit_text_sha256": hashes}}
    mapping = PackageMap("S", "other", "embedded", ((2, 3),), unit="filing_page")
    with pytest.raises(PackagingError, match="text of page 2 differs from what was captured"):
        pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "h1", stem="a", source_meta=bad)
    # a mismatch outside the kept units does not matter
    hashes = list(src["filing"]["unit_text_sha256"])
    hashes[5] = "0" * 64
    pk.package_filing(Path(doc.storage_path), mapping, tmp_path / "h2", stem="a",
                      source_meta={**src, "filing": {**src["filing"], "unit_text_sha256": hashes}})


async def test_the_layout_header_clips_hostile_member_names():
    assert "\n" not in pk._safe_member_name("a/reports/x\ny.xhtml") and "`" not in pk._safe_member_name("a`b")
