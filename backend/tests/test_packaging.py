"""Reviewed PDF packaging (`tret/services/packaging.py`).

Test PDFs are built in code with pypdf. Each page carries a distinct text marker so
"the right pages were kept" is a statement about content, not about page counts.
"""
from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    NullObject,
    NumberObject,
)

from tret.services import packaging
from tret.services.packaging import (
    PackageMap,
    PackageMapError,
    PackagingError,
    package_pdf,
    source_info,
    validate_map,
)

HAS_POPPLER = shutil.which("pdftotext") is not None
needs_poppler = pytest.mark.skipif(not HAS_POPPLER, reason="pdftotext (Poppler) not installed")


def pypdf_text(value: str):
    from pypdf.generic import TextStringObject

    return TextStringObject(value)


def make_pdf(
    n: int = 6,
    *,
    encrypt: str | None = None,
    links: bool = True,
    malformed: bool = False,
    bad_dest: bool = False,
) -> bytes:
    """n pages, each reading `Physical page i marker`.

    Page 2 links to page 4 (kept in the embedded fixture), to page 1 (not kept)
    and to a URL. Labels: pages 1-2 roman, the rest decimal from 7.
    """
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for i in range(1, n + 1):
        page = writer.add_blank_page(300, 300)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 20 150 Td (Physical page {i} marker) Tj ET".encode())
        page[NameObject("/Contents")] = writer._add_object(stream)
    if links:
        writer.add_annotation(1, Link(rect=(10, 10, 100, 30), target_page_index=3))
        writer.add_annotation(1, Link(rect=(10, 40, 100, 60), target_page_index=0))
        writer.add_annotation(1, Link(rect=(10, 70, 100, 90), url="https://example.org/elsewhere"))
    if bad_dest:
        # An integer destination past the end of the document.
        writer.add_annotation(1, Link(rect=(10, 100, 100, 120), target_page_index=0))
        writer.pages[1]["/Annots"][-1].get_object()["/Dest"][0] = NumberObject(99)
    if malformed:
        annots = writer.pages[1]["/Annots"]
        annots.append(NullObject())  # a null entry
        annots.append(IndirectObject(9999, 0, writer))  # a reference to nothing
        bad_action = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([NumberObject(1), NumberObject(2), NumberObject(3), NumberObject(4)]),
                NameObject("/A"): NumberObject(5),  # /A must be a dictionary
            }
        )
        annots.append(writer._add_object(bad_action))
        bad_rect = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Annot"),
                NameObject("/Subtype"): NameObject("/Link"),
                NameObject("/Rect"): ArrayObject([NumberObject(1), NullObject(), NumberObject(3), NumberObject(4)]),
                NameObject("/A"): DictionaryObject(
                    {NameObject("/S"): NameObject("/URI"), NameObject("/URI"): pypdf_text("https://example.org/odd")}
                ),
            }
        )
        annots.append(writer._add_object(bad_rect))
    writer.set_page_label(0, 1, style="/r")
    writer.set_page_label(2, n - 1, style="/D", start=7)
    if encrypt is not None:
        writer.encrypt(encrypt)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


@pytest.fixture
def src(tmp_path: Path) -> Path:
    path = tmp_path / "source.pdf"
    path.write_bytes(make_pdf())
    return path


def embedded(ranges=((2, 4), (6, 6)), **kw) -> PackageMap:
    base = dict(
        title="Sustainability statement",
        role="embedded_statement_and_assurance",
        scope="embedded",
        keep_ranges=None if ranges is None else tuple(ranges),
        review_note="reviewed",
    )
    base.update(kw)
    return PackageMap(**base)


def standalone(**kw) -> PackageMap:
    base = dict(title="Fact sheet", role="primary_disclosure", scope="standalone", keep_ranges=None)
    base.update(kw)
    return PackageMap(**base)


def out_files(out: packaging.PackageOutput) -> dict[str, Path]:
    return {f.role: f.path for f in out.files}


# -- validate_map ------------------------------------------------------------


@pytest.mark.parametrize(
    "ranges",
    [
        ((0, 2),),  # below 1
        ((5, 7),),  # past the end of a 6 page source
        ((4, 3),),  # last before first
        ((1, 3), (3, 5)),  # overlapping
        ((4, 5), (1, 2)),  # decreasing
        ((1, 2), (1, 2)),  # duplicated
        ((1.0, 2),),  # not integers
        ((True, 2),),  # bool is not a page number
        ((1, 2, 3),),  # not a pair
        (),  # empty
    ],
)
def test_validate_rejects_bad_ranges(ranges):
    with pytest.raises(PackageMapError):
        validate_map(embedded(ranges), 6)


def test_validate_accepts_ordered_disjoint_ranges():
    assert validate_map(embedded(((2, 4), (6, 6))), 6) == [2, 3, 4, 6]
    # Adjacent ranges are fine; they do not overlap.
    assert validate_map(embedded(((1, 2), (3, 4))), 6) == [1, 2, 3, 4]
    # Lists, as they arrive from JSON, behave like tuples.
    assert validate_map(embedded([[2, 3]]), 6) == [2, 3]


def test_embedded_requires_explicit_ranges():
    with pytest.raises(PackageMapError, match="explicit reviewed keep_ranges"):
        validate_map(embedded(None), 6)


def test_embedded_cannot_retain_every_page():
    with pytest.raises(PackageMapError, match="silently retain every page"):
        validate_map(embedded(((1, 6),)), 6)
    with pytest.raises(PackageMapError, match="silently retain every page"):
        validate_map(embedded(((1, 3), (4, 6))), 6)


def test_standalone_none_means_every_page():
    assert validate_map(standalone(), 4) == [1, 2, 3, 4]


def test_standalone_may_name_ranges_including_all():
    assert validate_map(standalone(keep_ranges=((2, 3),)), 4) == [2, 3]
    assert validate_map(standalone(keep_ranges=((1, 4),)), 4) == [1, 2, 3, 4]


def test_bad_role_scope_and_title():
    with pytest.raises(PackageMapError, match="role"):
        validate_map(standalone(role="annual_report"), 3)
    with pytest.raises(PackageMapError, match="scope"):
        validate_map(standalone(scope="whole"), 3)
    with pytest.raises(PackageMapError, match="title"):
        validate_map(standalone(title="  "), 3)


def test_incorporations_and_exclusions_are_checked_for_shape_only():
    ok = embedded(
        incorporations=({"originating_page": 3, "cited_component": "Note 7", "resolved_range": [5, 6],
                         "status": "retained", "note": ""},
                        {"originating_page": 3, "resolved_range": None, "status": "unresolved"}),
        exclusions=({"range": [5, 5], "reason": "unrelated"},),
    )
    assert validate_map(ok, 6) == [2, 3, 4, 6]
    with pytest.raises(PackageMapError, match="resolved_range"):
        validate_map(embedded(incorporations=({"resolved_range": [5, 9]},)), 6)
    with pytest.raises(PackageMapError, match="exclusions"):
        validate_map(embedded(exclusions=({"range": [0, 1], "reason": "x"},)), 6)
    with pytest.raises(PackageMapError, match="objects"):
        validate_map(embedded(incorporations=("not a dict",)), 6)


@pytest.mark.parametrize("stem", ["", ".hidden", "-lead", "has space", "a/b", "../x", "ünï"])
def test_bad_stem(src, tmp_path, stem):
    with pytest.raises(PackageMapError, match="stem"):
        package_pdf(src, standalone(), tmp_path / "out", stem=stem)
    assert not (tmp_path / "out").exists()


def test_map_error_is_raised_against_the_real_page_count(src, tmp_path):
    with pytest.raises(PackageMapError, match="Out-of-bounds"):
        package_pdf(src, embedded(((5, 9),)), tmp_path / "out", stem="x")
    assert not (tmp_path / "out").exists()
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")]


# -- standalone whole-original copy -------------------------------------------


def test_standalone_full_copy_is_byte_identical(src, tmp_path):
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fact-sheet",
                      source_meta={"requested_url": "https://example.org/fs.pdf"})

    files = out_files(out)
    assert files["retained_pdf"].read_bytes() == src.read_bytes()
    assert out.full_original_retained is True
    assert out.source_sha256 == out.retained_sha256
    assert out.source_page_count == out.retained_page_count == 6
    assert out.text_mode == ("pdftotext" if HAS_POPPLER else "pypdf")
    assert (out.poppler_version is not None) == HAS_POPPLER
    # Every link on a whole original is audited as preserved, none rewritten.
    links = json.loads(files["links"].read_text())
    assert {a["status"] for a in links} == {"preserved", "external_destination_requires_separate_collection"}
    assert out.record["source_meta"] == {"requested_url": "https://example.org/fs.pdf"}


def test_package_file_inventory(src, tmp_path):
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fs.v1")

    assert [f.role for f in out.files] == [
        "retained_pdf", "layout_text", "raw_text", "page_map", "links", "extraction_stats", "package_record",
    ]
    assert [f.filename for f in out.files] == [
        "fs.v1.pdf", "fs.v1.layout.md", "fs.v1.raw.md", "fs.v1.page_map.json",
        "fs.v1.links.json", "fs.v1.extraction_stats.json", "fs.v1.package.json",
    ]
    assert sorted(p.name for p in (tmp_path / "pkg").iterdir()) == sorted(f.filename for f in out.files)
    import hashlib
    for f in out.files:
        assert f.path.parent == tmp_path / "pkg"
        assert f.byte_size == f.path.stat().st_size
        assert f.sha256 == hashlib.sha256(f.path.read_bytes()).hexdigest()
    by_role = {f.role: f.content_type for f in out.files}
    assert by_role["retained_pdf"] == "application/pdf"
    assert by_role["layout_text"].startswith("text/markdown")
    assert by_role["package_record"] == "application/json"
    # The record on disk is the record returned, and names every artifact.
    on_disk = json.loads(out_files(out)["package_record"].read_text())
    assert on_disk == out.record
    assert out.record["artifacts"]["retained_pdf"] == "fs.v1.pdf"
    assert set(out.record["artifacts"]) == {f.role for f in out.files}


# -- embedded subset ----------------------------------------------------------


def test_embedded_subset_pages_text_labels_and_map(src, tmp_path):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    files = out_files(out)

    assert out.full_original_retained is False
    assert out.source_page_count == 6
    assert out.retained_page_count == 4
    assert out.retained_sha256 != out.source_sha256

    source, retained = PdfReader(str(src)), PdfReader(str(files["retained_pdf"]))
    assert len(retained.pages) == 4
    for new_idx, old_no in enumerate([2, 3, 4, 6]):
        assert retained.pages[new_idx].extract_text() == source.pages[old_no - 1].extract_text()
        assert f"Physical page {old_no} marker" in retained.pages[new_idx].extract_text()
    # Each retained page carries the label it had in the source.
    assert retained.page_labels == [source.page_labels[i - 1] for i in (2, 3, 4, 6)] == ["ii", "7", "8", "10"]

    page_map = json.loads(files["page_map"].read_text())
    assert page_map["source_page_count"] == 6
    assert page_map["retained_page_count"] == 4
    assert page_map["source_sha256"] == out.source_sha256
    assert page_map["retained_sha256"] == out.retained_sha256
    assert page_map["pages"] == [
        {"retained_pdf_page": 1, "source_pdf_page": 2, "source_page_label": "ii"},
        {"retained_pdf_page": 2, "source_pdf_page": 3, "source_page_label": "7"},
        {"retained_pdf_page": 3, "source_pdf_page": 4, "source_page_label": "8"},
        {"retained_pdf_page": 4, "source_pdf_page": 6, "source_page_label": "10"},
    ]


def test_embedded_link_audit_statuses_and_remap(src, tmp_path):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    files = out_files(out)

    audit = json.loads(files["links"].read_text())
    by_status = {a["status"]: a for a in audit}
    assert set(by_status) == {
        "remapped", "target_page_not_retained", "external_destination_requires_separate_collection",
    }
    # Source page 2 -> retained page 1; its link to source page 4 lands on retained page 3.
    remapped = by_status["remapped"]
    assert (remapped["source_pdf_page"], remapped["retained_pdf_page"]) == (2, 1)
    assert (remapped["target_source_pdf_page"], remapped["target_retained_pdf_page"]) == (4, 3)
    dropped = by_status["target_page_not_retained"]
    assert dropped["target_source_pdf_page"] == 1
    assert dropped["target_retained_pdf_page"] is None
    external = by_status["external_destination_requires_separate_collection"]
    assert external["type"] == "/URI"
    assert external["uri"] == "https://example.org/elsewhere"

    stats = out.stats
    assert stats["internal_links_remapped"] == 1
    assert stats["unretained_internal_targets"] == 1
    assert stats["unresolved_links"] == 0
    assert stats["external_links"] == 1

    # The rewritten link in the PDF really points at the retained page 3.
    retained = PdfReader(str(files["retained_pdf"]))
    internal = []
    for ann_ref in retained.pages[0]["/Annots"]:
        ann = ann_ref.get_object()
        if "/Dest" in ann:
            internal.append(retained.get_destination_page_number(
                retained._build_destination("x", ann["/Dest"])))
    assert internal == [2]  # 0-based index of retained page 3
    uris = [a.get_object().get("/A", {}).get("/URI") for a in retained.pages[0]["/Annots"]]
    assert "https://example.org/elsewhere" in uris


def test_package_record_contents(src, tmp_path):
    mapping = embedded(
        incorporations=({"originating_page": 3, "cited_component": "Note 7", "resolved_range": [6, 6],
                         "status": "retained", "note": "n"},),
        exclusions=({"range": [5, 5], "reason": "unrelated"},),
    )
    out = package_pdf(src, mapping, tmp_path / "pkg", stem="stmt", source_meta={"sha256": "abc"})
    rec = out.record

    assert rec["title"] == "Sustainability statement"
    assert rec["role"] == "embedded_statement_and_assurance"
    assert rec["scope"] == "embedded"
    assert rec["keep_ranges"] == [[2, 4], [6, 6]]
    assert rec["review_note"] == "reviewed"
    assert rec["incorporations"] == [dict(mapping.incorporations[0])]
    assert rec["exclusions"] == [{"range": [5, 5], "reason": "unrelated"}]
    assert rec["source_meta"] == {"sha256": "abc"}
    assert rec["full_original_retained"] is False
    assert rec["source_pdf_state"]["is_encrypted"] is False
    assert rec["retained_pdf_state"]["password_attempt_policy"] == "empty string only; no password guessing"
    assert rec["text_mode"] == out.text_mode
    assert set(rec["runtime"]) == {"python", "pypdf"}
    assert rec["started_utc"] <= rec["completed_utc"]
    assert rec["wall_seconds"] >= 0
    assert len(rec["limitations"]) >= 3
    ver = rec["verification"]
    assert ver["establishes"] == "content extraction and page retention"
    assert "reading order" in ver["does_not_establish"]
    assert "visual layout fidelity" in ver["does_not_establish"]
    assert not any("byte equality" in c for c in ver["checked"])  # a subset has no byte-copy check


def test_whole_original_record_lists_the_byte_check(src, tmp_path):
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fs")
    assert any("byte equality" in c for c in out.record["verification"]["checked"])


def test_packaging_is_repeatable(src, tmp_path):
    a = package_pdf(src, embedded(), tmp_path / "a", stem="s")
    b = package_pdf(src, embedded(), tmp_path / "b", stem="s")
    assert a.retained_sha256 == b.retained_sha256
    assert out_files(a)["layout_text"].read_bytes() == out_files(b)["layout_text"].read_bytes()
    assert out_files(a)["links"].read_bytes() == out_files(b)["links"].read_bytes()


# -- text modes ----------------------------------------------------------------


def test_markdown_is_page_referenced_without_invented_headings(src, tmp_path):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    md = out_files(out)["raw_text"].read_text(encoding="utf-8")

    assert md.startswith("# Sustainability statement\n")
    assert "Source text is transcribed as extracted, without paraphrase." in md
    assert f"Original SHA-256: `{out.source_sha256}`" in md
    assert f"Retained SHA-256: `{out.retained_sha256}`" in md
    heads = re.findall(r"^## (.*)$", md, flags=re.M)
    assert heads == [
        'Original PDF page 2 (source label "ii"); retained page 1',
        'Original PDF page 3 (source label "7"); retained page 2',
        'Original PDF page 4 (source label "8"); retained page 3',
        'Original PDF page 6 (source label "10"); retained page 4',
    ]
    for n in (2, 3, 4, 6):
        assert f"Physical page {n} marker" in md
    assert "Physical page 5 marker" not in md


@needs_poppler
def test_pdftotext_layout_and_raw_contain_the_page_text(src, tmp_path):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    files = out_files(out)

    assert out.text_mode == "pdftotext"
    assert out.poppler_version and "pdftotext" in out.poppler_version.lower()
    for role, flag in (("layout_text", "pdftotext -layout"), ("raw_text", "pdftotext -raw")):
        md = files[role].read_text(encoding="utf-8")
        assert f"Extraction mode: {flag}." in md
        assert "Poppler (pdftotext) was unavailable" not in md
        for n in (2, 3, 4, 6):
            assert f"Physical page {n} marker" in md
    assert out.stats["text_mode"] == "pdftotext"
    assert set(out.stats["modes"]) == {"layout", "raw"}
    assert out.stats["modes"]["raw"]["empty_pages"] == []
    assert not any("pypdf text" in x for x in out.record["limitations"])


def test_poppler_fallback_uses_pypdf_and_says_so(src, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging.shutil, "which", lambda name, *a, **k: None)
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    files = out_files(out)

    assert out.text_mode == "pypdf"
    assert out.poppler_version is None
    assert out.record["text_mode"] == "pypdf"
    assert out.record["poppler_version"] is None
    assert any("Poppler" in x and "unavailable" in x for x in out.record["limitations"])
    assert len(out.record["limitations"]) == 4
    assert set(files) == {
        "retained_pdf", "layout_text", "raw_text", "page_map", "links", "extraction_stats", "package_record",
    }
    layout = files["layout_text"].read_text(encoding="utf-8")
    raw = files["raw_text"].read_text(encoding="utf-8")
    for md in (layout, raw):
        assert "Poppler (pdftotext) was unavailable" in md
        assert "Extraction mode: pypdf extract_text." in md
        for n in (2, 3, 4, 6):
            assert f"Physical page {n} marker" in md
    assert out.stats["text_mode"] == "pypdf"
    assert out.stats["modes"]["layout"]["poppler_stderr"] == ""


def test_pdftotext_failure_publishes_nothing_and_cleans_staging(src, tmp_path, monkeypatch):
    # Poppler is "present" but cannot run: an error, not a silent downgrade to pypdf.
    monkeypatch.setattr(packaging.shutil, "which", lambda name, *a, **k: str(tmp_path / "no-such-pdftotext"))
    out_dir = tmp_path / "work" / "pkg"
    with pytest.raises(PackagingError):
        package_pdf(src, embedded(), out_dir, stem="stmt")

    assert not out_dir.exists()
    assert list((tmp_path / "work").iterdir()) == []  # no staging leftovers beside out_dir


# -- encryption ------------------------------------------------------------------


def test_empty_password_encrypted_source_packages_and_records_state(tmp_path):
    path = tmp_path / "enc.pdf"
    path.write_bytes(make_pdf(encrypt=""))

    info = source_info(path)
    assert info.page_count == 6
    assert info.pdf_state == {
        "is_encrypted": True,
        "opened_with_empty_password": True,
        "password_attempt_policy": "empty string only; no password guessing",
    }

    out = package_pdf(path, embedded(), tmp_path / "pkg", stem="enc")
    assert out.record["source_pdf_state"]["is_encrypted"] is True
    assert out.record["source_pdf_state"]["opened_with_empty_password"] is True
    # A page extract is written as an ordinary unencrypted PDF.
    assert out.record["retained_pdf_state"]["is_encrypted"] is False
    assert PdfReader(str(out_files(out)["retained_pdf"])).is_encrypted is False

    whole = package_pdf(path, standalone(), tmp_path / "whole", stem="enc")
    assert whole.full_original_retained is True
    assert out_files(whole)["retained_pdf"].read_bytes() == path.read_bytes()
    assert whole.record["retained_pdf_state"]["is_encrypted"] is True


def test_password_protected_source_is_refused(tmp_path):
    path = tmp_path / "locked.pdf"
    path.write_bytes(make_pdf(encrypt="s3cret"))

    with pytest.raises(PackagingError, match="non-empty password"):
        source_info(path)
    with pytest.raises(PackagingError, match="non-empty password"):
        package_pdf(path, standalone(), tmp_path / "pkg", stem="locked")
    assert not (tmp_path / "pkg").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


def test_unreadable_source_is_a_packaging_error(tmp_path):
    path = tmp_path / "junk.pdf"
    path.write_bytes(b"this is not a pdf")
    with pytest.raises(PackagingError):
        source_info(path)
    with pytest.raises(PackagingError):
        package_pdf(path, standalone(), tmp_path / "pkg", stem="junk")
    with pytest.raises(PackagingError, match="not found"):
        source_info(tmp_path / "missing.pdf")


# -- never overwrite / cleanup -----------------------------------------------------


def test_second_run_into_the_same_out_dir_is_refused(src, tmp_path):
    out_dir = tmp_path / "pkg"
    first = package_pdf(src, embedded(), out_dir, stem="stmt")
    before = {f.filename: f.sha256 for f in first.files}

    with pytest.raises(PackagingError, match="refusing to overwrite"):
        package_pdf(src, embedded(), out_dir, stem="stmt")

    assert {p.name: p for p in out_dir.iterdir()}.keys() == before.keys()
    import hashlib
    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in out_dir.iterdir()} == before
    # Staging is created beside out_dir, and nothing is left there.
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


def test_a_different_stem_may_share_the_out_dir(src, tmp_path):
    package_pdf(src, embedded(), tmp_path / "pkg", stem="one")
    package_pdf(src, embedded(), tmp_path / "pkg", stem="two")
    assert len(list((tmp_path / "pkg").iterdir())) == 14


def test_one_pre_existing_name_blocks_the_whole_package(src, tmp_path):
    out_dir = tmp_path / "pkg"
    out_dir.mkdir()
    (out_dir / "stmt.links.json").write_text("keep me")
    with pytest.raises(PackagingError, match="stmt.links.json"):
        package_pdf(src, embedded(), out_dir, stem="stmt")
    assert [p.name for p in out_dir.iterdir()] == ["stmt.links.json"]
    assert (out_dir / "stmt.links.json").read_text() == "keep me"


def test_failure_mid_packaging_leaves_no_staging_and_no_out_dir(src, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PackagingError("simulated verification failure")

    monkeypatch.setattr(packaging, "_write_markdown", boom)
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match="simulated"):
        package_pdf(src, embedded(), work / "pkg", stem="stmt")
    assert list(work.iterdir()) == []


def test_content_verification_failure_is_a_packaging_error(src, tmp_path, monkeypatch):
    # Without Poppler the oracle is pypdf. Make the retained PDF read back differently
    # from the source.
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: None if name == "pdftotext" else real_which(name, *a, **k)
    )
    real_open = packaging._open_pdf

    def tampered(path, warnings, phase):
        reader, state = real_open(path, warnings, phase)
        if phase == "retained_pdf_open":
            for page in reader.pages:
                page.extract_text = lambda *a, **k: "different"
        return reader, state

    monkeypatch.setattr(packaging, "_open_pdf", tampered)
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match="text changed on source page 2"):
        package_pdf(src, embedded(), work / "pkg", stem="stmt")
    assert list(work.iterdir()) == []


@needs_poppler
def test_poppler_content_verification_failure_is_a_packaging_error(src, tmp_path, monkeypatch):
    # The oracle is Poppler: the retained layout pass must equal the source pass.
    real = packaging._pdftotext_pages

    def tampered(exe, pdf, mode, count, timeout=None):
        texts, err = real(exe, pdf, mode, count, timeout=timeout)
        if mode == "layout" and Path(pdf).name.endswith(".pdf") and pdf != src:
            texts = ["different"] * count
        return texts, err

    monkeypatch.setattr(packaging, "_pdftotext_pages", tampered)
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match="text changed on source page 2"):
        package_pdf(src, embedded(), work / "pkg", stem="stmt")
    assert list(work.iterdir()) == []


# -- parser warnings ----------------------------------------------------------------


def test_parser_repair_warnings_are_captured(tmp_path):
    good = make_pdf()
    # Point startxref at the wrong offset: pypdf can still read the file but has to
    # repair it, and says so.
    bad = re.sub(rb"startxref\n(\d+)", lambda m: b"startxref\n" + str(int(m.group(1)) + 7).encode(), good)
    assert bad != good
    path = tmp_path / "repaired.pdf"
    path.write_bytes(bad)

    info = source_info(path)
    assert info.page_count == 6
    assert info.parser_warnings
    assert info.parser_warnings[0]["phase"] == "source_open"
    assert info.parser_warnings[0]["category"] == "source_parser_repair_warning"

    out = package_pdf(path, embedded(), tmp_path / "pkg", stem="repaired")
    assert out.parser_warning_count >= 1
    assert out.stats["parser_warning_count"] == out.parser_warning_count
    assert out.stats["parser_warnings"] == out.record["parser_warnings"]
    first = out.stats["parser_warnings"][0]
    assert set(first) == {"phase", "level", "logger", "category", "message"}
    assert first["category"] == "source_parser_repair_warning"
    assert "startxref" in first["message"]


def test_clean_source_has_no_warnings(src, tmp_path):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="clean")
    assert out.parser_warning_count == 0
    assert out.stats["parser_warnings"] == []


def test_warning_capture_ignores_other_threads():
    import logging

    warnings: list[dict] = []
    other = threading.Thread(target=lambda: logging.getLogger("pypdf._reader").warning("from elsewhere"))
    with packaging._parser_warning_capture(warnings, "t"):
        logging.getLogger("pypdf._reader").warning("incorrect xref here")
        other.start()
        other.join()
    assert [w["message"] for w in warnings] == ["incorrect xref here"]
    assert warnings[0]["category"] == "source_parser_repair_warning"
    assert warnings[0]["phase"] == "t"


# -- stem length ----------------------------------------------------------------


def test_stem_length_is_capped(src, tmp_path):
    with pytest.raises(PackageMapError, match="100 characters"):
        package_pdf(src, standalone(), tmp_path / "out", stem="a" * 101)
    out = package_pdf(src, standalone(), tmp_path / "out", stem="a" * 100)
    assert out.files[0].filename == "a" * 100 + ".pdf"


# -- malformed annotations --------------------------------------------------------


@pytest.mark.parametrize("mapping_factory", [embedded, standalone])
def test_malformed_annotations_are_audited_not_fatal(tmp_path, mapping_factory):
    path = tmp_path / "odd.pdf"
    path.write_bytes(make_pdf(malformed=True))

    out = package_pdf(path, mapping_factory(), tmp_path / "pkg", stem="odd")

    audit = json.loads(out_files(out)["links"].read_text())
    bad = [a for a in audit if a["status"] == "malformed_annotation"]
    # null entry, dangling reference, link whose /A is not a dictionary
    assert len(bad) == 3
    assert {a["source_pdf_page"] for a in bad} == {2}
    assert sorted(a["annotation_index"] for a in bad) == [3, 4, 5]
    assert any("not an action dictionary" in a["detail"] for a in bad)
    assert out.stats["malformed_annotations"] == 3
    # A /Rect with a null in it loses its rectangle but the link is still audited.
    odd = next(a for a in audit if a.get("uri") == "https://example.org/odd")
    assert odd["rectangle"] is None
    assert odd["status"] == "external_destination_requires_separate_collection"
    # The three well-formed links were unaffected.
    assert sum(a["status"] != "malformed_annotation" for a in audit) == 4


def test_unexpected_link_failure_names_the_page(src, tmp_path, monkeypatch):
    def boom(*a, **k):
        raise TypeError("odd object")

    monkeypatch.setattr(packaging, "_resolve_dest", boom)
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match=r"source page 2: TypeError: odd object"):
        package_pdf(src, embedded(), work / "pkg", stem="stmt")
    assert list(work.iterdir()) == []


def test_out_of_range_destination_is_unresolved(tmp_path):
    path = tmp_path / "bad-dest.pdf"
    path.write_bytes(make_pdf(bad_dest=True))

    out = package_pdf(path, embedded(), tmp_path / "pkg", stem="bd")

    audit = json.loads(out_files(out)["links"].read_text())
    statuses = [a["status"] for a in audit if a.get("type") == "internal"]
    assert sorted(statuses) == ["remapped", "target_page_not_retained", "unresolved_source_destination"]
    bogus = next(a for a in audit if a["status"] == "unresolved_source_destination")
    assert bogus["target_source_pdf_page"] is None
    assert bogus["target_retained_pdf_page"] is None
    assert out.stats["unresolved_links"] == 1
    assert out.stats["unretained_internal_targets"] == 1


# -- annotations that must not drag pages in ----------------------------------------


def test_non_link_annotation_parent_chain_does_not_pull_in_excluded_pages(tmp_path):
    writer = PdfWriter()
    for i in range(1, 5):
        writer.add_blank_page(200, 200)
    # A widget on page 2 whose /Parent field says its /Kids live on page 4.
    parent = DictionaryObject({NameObject("/FT"): NameObject("/Tx"), NameObject("/T"): pypdf_text("f")})
    parent_ref = writer._add_object(parent)
    widget = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/Rect"): ArrayObject([NumberObject(0), NumberObject(0), NumberObject(10), NumberObject(10)]),
            NameObject("/Parent"): parent_ref,
        }
    )
    far = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/P"): writer.pages[3].indirect_reference,
            NameObject("/Parent"): parent_ref,
        }
    )
    parent[NameObject("/Kids")] = ArrayObject([writer._add_object(widget), writer._add_object(far)])
    writer.pages[1][NameObject("/Annots")] = ArrayObject([parent["/Kids"][0]])
    buf = io.BytesIO()
    writer.write(buf)
    path = tmp_path / "form.pdf"
    path.write_bytes(buf.getvalue())

    out = package_pdf(path, embedded(((2, 3),)), tmp_path / "pkg", stem="form")

    retained = PdfReader(str(out_files(out)["retained_pdf"]))
    assert len(retained.pages) == 2
    annot = retained.pages[0]["/Annots"][0].get_object()
    assert annot["/Subtype"] == "/Widget"
    assert "/Parent" not in annot


# -- stderr cap ---------------------------------------------------------------------


def test_poppler_stderr_is_capped_in_stats(src, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging.shutil, "which", lambda name, *a, **k: "/fake/pdftotext")
    monkeypatch.setattr(packaging, "_poppler_version", lambda exe: "pdftotext fake 1.0")
    huge = "W" * (packaging.MAX_POPPLER_STDERR_BYTES + 5000)
    seen = {}

    def fake(exe, pdf, mode, count, timeout=None):
        seen[mode] = timeout
        return ["page"] * count, huge if mode == "layout" else "short"

    monkeypatch.setattr(packaging, "_pdftotext_pages", fake)
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt", deadline_seconds=100)

    layout, raw = out.stats["modes"]["layout"], out.stats["modes"]["raw"]
    assert layout["stderr_truncated"] is True
    assert len(layout["poppler_stderr"].encode()) == packaging.MAX_POPPLER_STDERR_BYTES
    assert raw["stderr_truncated"] is False
    assert raw["poppler_stderr"] == "short"
    # Each Poppler run is bounded by what is left of the deadline.
    assert 0 < seen["layout"] <= 100 and 0 < seen["raw"] <= 100


def test_exhausted_deadline_stops_before_poppler(src, tmp_path, monkeypatch):
    monkeypatch.setattr(packaging.shutil, "which", lambda name, *a, **k: "/fake/pdftotext")
    monkeypatch.setattr(packaging, "_poppler_version", lambda exe: "fake")
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match="exceeded"):
        package_pdf(src, embedded(), work / "pkg", stem="stmt", deadline_seconds=1e-9)
    assert list(work.iterdir()) == []


# -- publish --------------------------------------------------------------------------


def _staged(tmp_path: Path, names: list[str]) -> Path:
    stage = tmp_path / "stage"
    stage.mkdir()
    for n in names:
        (stage / n).write_text(f"staged {n}")
    return stage


def test_publish_refuses_a_file_that_appeared_and_rolls_back_only_its_own(tmp_path):
    names = ["a.pdf", "b.json", "c.json", "d.md"]
    stage = _staged(tmp_path, names)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "c.json").write_text("someone else's")

    with pytest.raises(PackagingError, match="c.json"):
        packaging._publish(stage, out_dir, names)

    assert sorted(p.name for p in out_dir.iterdir()) == ["c.json"]
    assert (out_dir / "c.json").read_text() == "someone else's"


def test_publish_into_a_new_dir_leaves_no_dir_behind_on_conflict(tmp_path, monkeypatch):
    names = ["a.pdf", "b.json"]
    stage = _staged(tmp_path, names)
    out_dir = tmp_path / "fresh"
    real = packaging._create_exclusive

    def conflict_on_b(src, target):
        if target.name == "b.json":
            raise FileExistsError
        real(src, target)

    monkeypatch.setattr(packaging, "_create_exclusive", conflict_on_b)
    with pytest.raises(PackagingError, match="b.json"):
        packaging._publish(stage, out_dir, names)
    assert not out_dir.exists()


def test_publish_falls_back_to_exclusive_copy_when_links_fail(src, tmp_path, monkeypatch):
    def no_links(*a, **k):
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(packaging.os, "link", no_links)
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")
    for f in out.files:
        assert hashlib.sha256(f.path.read_bytes()).hexdigest() == f.sha256

    # The copy fallback is exclusive as well: an existing target is never replaced.
    stage = _staged(tmp_path, ["x.json"])
    target_dir = tmp_path / "t"
    target_dir.mkdir()
    (target_dir / "x.json").write_text("mine")
    with pytest.raises(PackagingError):
        packaging._publish(stage, target_dir, ["x.json"])
    assert (target_dir / "x.json").read_text() == "mine"


def test_publish_conflict_through_package_pdf_leaves_other_files_out(src, tmp_path, monkeypatch):
    # The up-front check passes, then a file shows up before publish.
    out_dir = tmp_path / "pkg"
    real_publish = packaging._publish

    def racing(stage, out_dir_, names, **kw):
        out_dir_.mkdir(parents=True, exist_ok=True)
        (out_dir_ / "stmt.links.json").write_text("raced in")
        real_publish(stage, out_dir_, names, **kw)

    monkeypatch.setattr(packaging, "_publish", racing)
    with pytest.raises(PackagingError, match="stmt.links.json"):
        package_pdf(src, embedded(), out_dir, stem="stmt")
    assert [p.name for p in out_dir.iterdir()] == ["stmt.links.json"]
    assert (out_dir / "stmt.links.json").read_text() == "raced in"
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


# -- run_packaging (child process) ----------------------------------------------------


def comparable(out: packaging.PackageOutput) -> dict:
    return {
        # The stats and the record carry wall-clock timings, so their bytes differ run to run.
        "files": {f.role: f.sha256 for f in out.files if f.role not in ("package_record", "extraction_stats")},
        "source": out.source_sha256,
        "retained": out.retained_sha256,
        "pages": (out.source_page_count, out.retained_page_count, out.full_original_retained),
        "text_mode": out.text_mode,
        "poppler": out.poppler_version,
        "stats": {k: v for k, v in out.stats.items() if k != "timings"},
        "warnings": out.parser_warning_count,
    }


@pytest.mark.parametrize("mapping_factory", [embedded, standalone])
def test_run_packaging_matches_package_pdf(src, tmp_path, mapping_factory):
    meta = {"requested_url": "https://example.org/x.pdf"}
    direct = package_pdf(src, mapping_factory(), tmp_path / "direct", stem="s", source_meta=meta)
    isolated = packaging.run_packaging(src, mapping_factory(), tmp_path / "iso", stem="s", source_meta=meta)

    assert isinstance(isolated, packaging.PackageOutput)
    assert all(isinstance(f, packaging.PackageFile) and isinstance(f.path, Path) for f in isolated.files)
    assert [f.path.parent for f in isolated.files] == [tmp_path / "iso"] * 7
    assert comparable(isolated) == comparable(direct)
    assert isolated.record["source_meta"] == meta
    assert isolated.record["keep_ranges"] == direct.record["keep_ranges"]
    on_disk = json.loads(out_files(isolated)["package_record"].read_text())
    assert on_disk == isolated.record
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


def test_run_packaging_works_with_the_pypdf_fallback_too(src, tmp_path):
    # The child decides on Poppler itself; with an empty PATH it must fall back.
    out = packaging.run_packaging(src, embedded(), tmp_path / "pkg", stem="s")
    assert out.text_mode == ("pdftotext" if HAS_POPPLER else "pypdf")


def test_run_packaging_reraises_the_right_error_types(src, tmp_path):
    with pytest.raises(PackageMapError, match="Out-of-bounds"):
        packaging.run_packaging(src, embedded(((5, 9),)), tmp_path / "a", stem="s")
    with pytest.raises(PackageMapError, match="stem"):
        packaging.run_packaging(src, standalone(), tmp_path / "a", stem="bad stem")
    locked = tmp_path / "locked.pdf"
    locked.write_bytes(make_pdf(encrypt="s3cret"))
    with pytest.raises(PackagingError, match="non-empty password"):
        packaging.run_packaging(locked, standalone(), tmp_path / "b", stem="s")
    packaging.run_packaging(src, standalone(), tmp_path / "c", stem="s")
    with pytest.raises(PackagingError, match="refusing to overwrite"):
        packaging.run_packaging(src, standalone(), tmp_path / "c", stem="s")
    assert not (tmp_path / "a").exists() and not (tmp_path / "b").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


def test_run_packaging_deadline_kills_the_child_and_leaves_nothing(src, tmp_path):
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match=r"packaging exceeded 0\.001 s"):
        packaging.run_packaging(src, embedded(), work / "pkg", stem="s", deadline_seconds=0.001)
    assert not (work / "pkg").exists()
    assert list(work.iterdir()) == []
    with pytest.raises(PackagingError, match="exceeded"):
        packaging.run_packaging(src, embedded(), work / "pkg", stem="s", deadline_seconds=0)


def _fake_worker(tmp_path: Path, monkeypatch, body: str) -> Path:
    script = tmp_path / "fake_worker.py"
    script.write_text("import json, os, sys, time\njob = json.load(sys.stdin)\n" + body)
    monkeypatch.setattr(packaging, "_WORKER_PATH", script)
    return script


def test_a_hung_child_is_killed_with_its_children_and_its_debris_removed(src, tmp_path, monkeypatch):
    pidfile = tmp_path / "child.pid"
    _fake_worker(
        tmp_path,
        monkeypatch,
        # Behave like a child killed between two publish steps: one file already in
        # out_dir, the staging dir populated, and a grandchild still running.
        "import subprocess\n"
        "os.makedirs(job['out_dir'], exist_ok=True)\n"
        "open(os.path.join(job['out_dir'], job['stem'] + '.pdf'), 'w').write('half published')\n"
        "open(os.path.join(job['stage_dir'], 'partial'), 'w').write('x')\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(job['source_meta']['pidfile'], 'w').write(str(g.pid))\n"
        "time.sleep(60)\n",
    )
    work = tmp_path / "work"
    with pytest.raises(PackagingError, match=r"packaging exceeded 1 s"):
        packaging.run_packaging(
            src, embedded(), work / "pkg", stem="s", source_meta={"pidfile": str(pidfile)}, deadline_seconds=1
        )

    assert list(work.iterdir()) == []  # out_dir (did not exist before) and staging both gone
    grandchild = int(pidfile.read_text())
    import time

    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the grandchild process survived the kill")


def test_a_killed_child_never_removes_files_that_were_there_before(src, tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, "time.sleep(60)\n")
    out_dir = tmp_path / "pkg"
    out_dir.mkdir()
    (out_dir / "other.pdf").write_text("unrelated")
    with pytest.raises(PackagingError, match="exceeded"):
        packaging.run_packaging(src, embedded(), out_dir, stem="s", deadline_seconds=0.5)
    assert [p.name for p in out_dir.iterdir()] == ["other.pdf"]


def test_a_dead_child_is_reported_with_its_stderr(src, tmp_path, monkeypatch):
    _fake_worker(tmp_path, monkeypatch, "print('Killed by the ceiling', file=sys.stderr)\nsys.exit(3)\n")
    with pytest.raises(PackagingError, match=r"died \(exit 3\): Killed by the ceiling"):
        packaging.run_packaging(src, embedded(), tmp_path / "pkg", stem="s")
    _fake_worker(tmp_path, monkeypatch, "import signal\nos.kill(os.getpid(), signal.SIGKILL)\n")
    with pytest.raises(PackagingError, match="SIGKILL"):
        packaging.run_packaging(src, embedded(), tmp_path / "pkg", stem="s")
    _fake_worker(tmp_path, monkeypatch, "print('not json')\n")
    with pytest.raises(PackagingError, match="no result"):
        packaging.run_packaging(src, embedded(), tmp_path / "pkg", stem="s")
    assert not (tmp_path / "pkg").exists()


def test_child_environment_is_path_only(src, tmp_path, monkeypatch):
    monkeypatch.setenv("SOME_API_KEY", "do-not-leak")
    probe = tmp_path / "env.json"
    _fake_worker(
        tmp_path,
        monkeypatch,
        f"json.dump(dict(os.environ), open({str(probe)!r}, 'w'))\n"
        "print(json.dumps({'ok': False, 'kind': 'packaging', 'message': 'probe'}))\n",
    )
    with pytest.raises(PackagingError, match="probe"):
        packaging.run_packaging(src, embedded(), tmp_path / "pkg", stem="s")
    assert set(json.loads(probe.read_text())) - {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"} == {"PATH"}
    assert sys.executable  # the worker is launched with the same interpreter


# -- OCR supplement ------------------------------------------------------------

HAS_OCR_TOOLS = all(shutil.which(t) for t in ("pdftoppm", "tesseract"))
try:
    import PIL  # noqa: F401

    HAS_PILLOW = True
except ImportError:  # pragma: no cover - Pillow is a dependency of the app
    HAS_PILLOW = False
needs_ocr = pytest.mark.skipif(
    not (HAS_OCR_TOOLS and HAS_PILLOW), reason="pdftoppm, tesseract and Pillow are needed for real OCR"
)

SCAN_WORDS = "SCANNED DICTIONARY ENTRY"


def make_mixed_pdf() -> bytes:
    """Three pages: native text, an image of text with no text layer, native text."""
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGB", (1500, 400), "white")
    ImageDraw.Draw(image).text((60, 140), SCAN_WORDS, fill="black", font=ImageFont.load_default(size=90))
    buf = io.BytesIO()
    image.save(buf, "PDF", resolution=200.0)
    native = PdfReader(io.BytesIO(make_pdf(3, links=False)))
    writer = PdfWriter()
    writer.add_page(native.pages[0])
    writer.add_page(PdfReader(io.BytesIO(buf.getvalue())).pages[0])
    writer.add_page(native.pages[2])
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


@pytest.fixture
def mixed_src(tmp_path: Path) -> Path:
    path = tmp_path / "mixed.pdf"
    path.write_bytes(make_mixed_pdf() if HAS_PILLOW else make_pdf(3, links=False))
    return path


@pytest.fixture
def fake_ocr(monkeypatch):
    """Pretend Tesseract is installed and 'read' each page; no subprocess involved."""
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil,
        "which",
        lambda name, *a, **k: f"/fake/{name}" if name in ("pdftoppm", "tesseract") else real_which(name, *a, **k),
    )
    monkeypatch.setattr(packaging, "_tesseract_version", lambda exe: "tesseract 9.9.9")
    seen: list[list[int]] = []

    def pages(pdftoppm, tesseract, pdf, work, retained_pages, remaining, deadline, scale_pages=frozenset()):
        seen.append(list(retained_pages))
        return [f"fake ocr of retained page {n}\n" for n in retained_pages], False

    monkeypatch.setattr(packaging, "_ocr_pages", pages)
    return seen


def test_validate_rejects_a_bad_ocr_value():
    with pytest.raises(PackageMapError, match="ocr must be one of off, auto, all"):
        validate_map(standalone(ocr="always"), 3)
    for ok in ("off", "auto", "all"):
        assert validate_map(standalone(ocr=ok), 3) == [1, 2, 3]
    assert standalone().ocr == "off"


def test_ocr_off_keeps_the_seven_file_contract(src, tmp_path):
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fs")
    assert len(out.files) == 7
    assert "ocr_text" not in {f.role for f in out.files}
    assert "ocr" not in out.stats and "ocr" not in out.record
    assert "ocr_text" not in out.record["artifacts"]
    assert not any("OCR" in line for line in out.record["limitations"])
    assert not (tmp_path / "pkg" / "fs.ocr.md").exists()


@needs_ocr
def test_ocr_auto_reads_only_native_empty_pages(mixed_src, tmp_path):
    out = package_pdf(mixed_src, standalone(ocr="auto"), tmp_path / "pkg", stem="mix")

    assert len(out.files) == 8
    assert [f.role for f in out.files][:4] == ["retained_pdf", "layout_text", "raw_text", "ocr_text"]
    ocr_file = next(f for f in out.files if f.role == "ocr_text")
    assert ocr_file.filename == "mix.ocr.md"
    assert ocr_file.content_type.startswith("text/markdown")
    assert sorted(p.name for p in (tmp_path / "pkg").iterdir()) == sorted(f.filename for f in out.files)

    stats = out.stats["ocr"]
    assert out.stats["modes"]["layout"]["empty_pages"] == [2]
    assert stats["status"] == "done"
    assert stats["mode"] == "auto"
    assert stats["engine"].startswith("tesseract")
    assert (stats["dpi"], stats["psm"]) == (200, 6)
    assert stats["pages"] == [2] and stats["page_count"] == 1
    assert stats["truncated"] is False
    assert stats["characters"] > 0

    md = (tmp_path / "pkg" / "mix.ocr.md").read_text()
    assert "OCR supplement" in md and "separately labelled" in md
    assert "OCR is not exact transcription; the retained PDF controls; native text is unchanged" in md
    assert "200 dpi" in md and "psm) 6" in md and stats["engine"] in md
    assert re.findall(r"^## Original PDF page .*$", md, re.M) == [
        '## Original PDF page 2 (source label "2"); retained page 2 — OCR'
    ]
    assert "DICTIONARY" in md.upper()

    # Native text is untouched: the OCR page stays empty in the layout file.
    layout = (tmp_path / "pkg" / "mix.layout.md").read_text()
    assert "DICTIONARY" not in layout.upper()
    assert "Physical page 1 marker" in layout

    record = out.record
    assert record["artifacts"]["ocr_text"] == "mix.ocr.md"
    assert record["ocr"] == stats
    assert any("OCR text is a supplement produced from rendered pages" in x for x in record["limitations"])
    assert json.loads((tmp_path / "pkg" / "mix.package.json").read_text()) == record
    assert json.loads((tmp_path / "pkg" / "mix.extraction_stats.json").read_text())["ocr"] == stats
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


@needs_ocr
def test_ocr_all_reads_every_retained_page(mixed_src, tmp_path):
    out = package_pdf(mixed_src, embedded(ranges=((2, 3),), ocr="all"), tmp_path / "pkg", stem="mix")
    stats = out.stats["ocr"]
    assert stats["status"] == "done" and stats["mode"] == "all"
    assert stats["pages"] == [2, 3] and stats["page_count"] == 2
    md = (tmp_path / "pkg" / "mix.ocr.md").read_text()
    assert len(re.findall(r"^## Original PDF page ", md, re.M)) == 2
    assert "retained page 1 — OCR" in md and "retained page 2 — OCR" in md
    assert "marker" in md.lower()  # page 3's native text, read back from pixels


@needs_ocr
def test_ocr_leaves_no_png_behind_and_publishes_nothing_extra(mixed_src, tmp_path):
    package_pdf(mixed_src, standalone(ocr="auto"), tmp_path / "pkg", stem="mix")
    assert not list(tmp_path.rglob("*.png"))


def test_ocr_auto_with_no_empty_pages_is_skipped(src, tmp_path, fake_ocr):
    out = package_pdf(src, standalone(ocr="auto"), tmp_path / "pkg", stem="fs")
    assert len(out.files) == 7
    assert out.stats["ocr"]["status"] == "skipped"
    assert out.stats["ocr"]["pages"] == [] and out.stats["ocr"]["page_count"] == 0
    assert out.record["ocr"] == out.stats["ocr"]
    assert "ocr_text" not in out.record["artifacts"]
    assert fake_ocr == []


@pytest.mark.skipif(not HAS_POPPLER, reason="auto needs pdftotext to know which pages are empty")
@pytest.mark.parametrize("missing", [("tesseract",), ("pdftoppm",), ("pdftoppm", "tesseract")])
def test_ocr_unavailable_still_packages(mixed_src, tmp_path, monkeypatch, missing):
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: None if name in missing else real_which(name, *a, **k)
    )
    mapping = standalone(ocr="all")
    out = package_pdf(mixed_src, mapping, tmp_path / "pkg", stem="mix")

    assert len(out.files) == 7
    ocr = out.stats["ocr"]
    assert ocr["status"] == "unavailable" and ocr["mode"] == "all"
    assert all(name in ocr["reason"] for name in missing)
    assert ocr["pages"] == [] and ocr["page_count"] == 0
    assert out.record["ocr"] == ocr
    assert "ocr_text" not in out.record["artifacts"]
    assert any("unavailable" in x and "no OCR supplement" in x for x in out.record["limitations"])
    assert not (tmp_path / "pkg" / "mix.ocr.md").exists()


def test_ocr_page_cap_truncates_and_records_it(src, tmp_path, monkeypatch, fake_ocr):
    monkeypatch.setattr(packaging, "MAX_OCR_PAGES", 2)
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")

    assert fake_ocr == [[1, 2]]
    stats = out.stats["ocr"]
    assert stats["status"] == "done" and stats["truncated"] is True
    assert stats["pages"] == [1, 2] and stats["page_count"] == 2
    assert stats["truncated_pages"] == [3, 4, 5, 6]
    assert stats["engine"] == "tesseract 9.9.9"
    md = (tmp_path / "pkg" / "fs.ocr.md").read_text()
    assert stats["truncated_reason"] == "page_cap"
    assert "Truncated (page_cap): only the first 2 target pages" in md
    assert len(re.findall(r"^## Original PDF page ", md, re.M)) == 2
    assert len(out.files) == 8


def test_ocr_markdown_fences_text_containing_backticks(src, tmp_path, monkeypatch, fake_ocr):
    monkeypatch.setattr(packaging, "_ocr_pages", lambda *a, **k: (["a ```` b\n"] * 6, False))
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")
    md = (tmp_path / "pkg" / "fs.ocr.md").read_text()
    assert "`````text\na ```` b\n`````" in md
    assert out.stats["ocr"]["characters"] == 6 * len("a ```` b\n")


def test_ocr_name_is_claimed_by_the_request(src, tmp_path):
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "fs.ocr.md").write_text("someone else's")
    with pytest.raises(PackagingError, match="fs.ocr.md"):
        package_pdf(src, standalone(ocr="auto"), pkg, stem="fs")
    # With OCR off the stray file is not ours to care about.
    assert len(package_pdf(src, standalone(), pkg, stem="fs").files) == 7
    assert (pkg / "fs.ocr.md").read_text() == "someone else's"


def test_ocr_failure_publishes_nothing(src, tmp_path, monkeypatch, fake_ocr):
    def boom(*a, **k):
        raise PackagingError("tesseract exited 1 on retained page 1: bad")

    monkeypatch.setattr(packaging, "_ocr_pages", boom)
    with pytest.raises(PackagingError, match="tesseract exited 1"):
        package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")
    assert not (tmp_path / "pkg").exists()
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


@needs_ocr
def test_run_packaging_round_trips_the_ocr_file(mixed_src, tmp_path):
    out = packaging.run_packaging(mixed_src, standalone(ocr="auto"), tmp_path / "iso", stem="mix")

    assert len(out.files) == 8
    assert all(isinstance(f.path, Path) and f.path.parent == tmp_path / "iso" for f in out.files)
    ocr_file = next(f for f in out.files if f.role == "ocr_text")
    assert ocr_file.filename == "mix.ocr.md" and ocr_file.path.is_file()
    assert ocr_file.sha256 == hashlib.sha256(ocr_file.path.read_bytes()).hexdigest()
    assert "DICTIONARY" in ocr_file.path.read_text().upper()
    assert out.stats["ocr"]["status"] == "done" and out.stats["ocr"]["pages"] == [2]
    assert out.record["ocr"] == out.stats["ocr"]
    assert out.record["artifacts"]["ocr_text"] == "mix.ocr.md"
    assert json.loads(out_files(out)["package_record"].read_text()) == out.record
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".packaging-stage-")] == []


def test_run_packaging_rejects_a_bad_ocr_value_as_a_map_error(src, tmp_path):
    with pytest.raises(PackageMapError, match="ocr must be one of"):
        packaging.run_packaging(src, standalone(ocr="sometimes"), tmp_path / "pkg", stem="fs")
    assert not (tmp_path / "pkg").exists()


def test_run_packaging_off_is_still_seven_files(src, tmp_path):
    out = packaging.run_packaging(src, standalone(ocr="off"), tmp_path / "pkg", stem="fs")
    assert len(out.files) == 7 and "ocr" not in out.stats


# -- verification oracle and timings -------------------------------------------------


@pytest.fixture
def counters(monkeypatch):
    """Count pypdf page text extractions and pdftotext runs (by mode and target)."""
    from pypdf._page import PageObject

    count = {"pypdf_extract": 0, "pdftotext": []}
    real_extract = PageObject.extract_text
    real_pdftotext = packaging._pdftotext_pages

    def extract(self, *a, **k):
        count["pypdf_extract"] += 1
        return real_extract(self, *a, **k)

    def pdftotext(exe, pdf, mode, n, timeout=None):
        count["pdftotext"].append((Path(pdf).name, mode))
        return real_pdftotext(exe, pdf, mode, n, timeout=timeout)

    monkeypatch.setattr(PageObject, "extract_text", extract)
    monkeypatch.setattr(packaging, "_pdftotext_pages", pdftotext)
    return count


@needs_poppler
def test_byte_copy_skips_per_page_text_verification(src, tmp_path, counters):
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fs")

    assert counters["pypdf_extract"] == 0
    # Only the retained file is read by Poppler, once per mode: no pass over the source.
    assert counters["pdftotext"] == [("fs.pdf", "layout"), ("fs.pdf", "raw")]
    ver = out.record["verification"]
    assert ver["oracle"] == "sha256"
    assert any("byte equality" in c and "skipped" in c for c in ver["checked"])
    assert not any("extract_text equality" in c or "pdftotext -layout equality" in c for c in ver["checked"])
    assert out.stats["content_verification"].startswith("SHA-256 byte equality")
    assert out.source_sha256 == out.retained_sha256


def test_byte_copy_skips_text_verification_without_poppler_too(src, tmp_path, monkeypatch, counters):
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: None if name == "pdftotext" else real_which(name, *a, **k)
    )
    out = package_pdf(src, standalone(), tmp_path / "pkg", stem="fs")

    assert out.record["verification"]["oracle"] == "sha256"
    # The pypdf fallback extracts for the markdown (layout and raw), never to verify.
    assert counters["pypdf_extract"] == 2 * 6


@needs_poppler
def test_subset_verifies_with_one_poppler_pass_over_the_source(src, tmp_path, counters):
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")

    assert counters["pypdf_extract"] == 0
    assert sorted(counters["pdftotext"]) == sorted(
        [("stmt.pdf", "layout"), ("stmt.pdf", "raw"), ("source.pdf", "layout")]
    )
    ver = out.record["verification"]
    assert ver["oracle"] == "pdftotext"
    assert any("pdftotext -layout equality" in c for c in ver["checked"])
    assert not any("byte equality" in c for c in ver["checked"])
    assert "pdftotext -layout" in out.stats["content_verification"]


def test_subset_falls_back_to_pypdf_equality_without_poppler(src, tmp_path, monkeypatch, counters):
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: None if name == "pdftotext" else real_which(name, *a, **k)
    )
    out = package_pdf(src, embedded(), tmp_path / "pkg", stem="stmt")

    assert counters["pdftotext"] == []
    ver = out.record["verification"]
    assert ver["oracle"] == "pypdf"
    assert any("pypdf extract_text equality" in c for c in ver["checked"])
    # 4 selected pages: two pypdf reads each for verification, plus layout and raw markdown.
    assert counters["pypdf_extract"] == 4 * 2 + 4 * 2
    assert out.stats["content_verification"].startswith("exact pypdf extract_text equality")


@pytest.mark.parametrize("factory", [standalone, embedded])
def test_timings_are_recorded_in_stats_and_record(src, tmp_path, factory):
    out = package_pdf(src, factory(), tmp_path / "pkg", stem="s")

    keys = {"open", "subset_write", "verify", "text_layout", "text_raw", "ocr", "publish", "total"}
    assert tuple(out.stats["timings"]) == packaging.TIMING_PHASES
    assert set(out.stats["timings"]) == keys
    assert out.record["timings"] == out.stats["timings"]
    assert all(isinstance(v, float) and v >= 0 for v in out.stats["timings"].values())
    assert out.stats["timings"]["ocr"] == 0.0  # not requested
    assert out.stats["timings"]["total"] >= out.stats["timings"]["open"]
    assert out.stats["timings"]["total"] >= out.stats["timings"]["publish"] > 0
    # What is on disk is what is returned, publish and total included.
    assert json.loads(out_files(out)["extraction_stats"].read_text()) == out.stats
    assert json.loads(out_files(out)["package_record"].read_text()) == out.record


def test_a_late_publish_failure_rolls_back_the_data_files_too(src, tmp_path, monkeypatch):
    real = packaging._create_exclusive

    def flaky(stage_file, target):
        if target.name == "stmt.package.json":
            raise FileExistsError
        real(stage_file, target)

    monkeypatch.setattr(packaging, "_create_exclusive", flaky)
    with pytest.raises(PackagingError, match="stmt.package.json"):
        package_pdf(src, embedded(), tmp_path / "work" / "pkg", stem="stmt")
    assert not (tmp_path / "work" / "pkg").exists()


def test_default_deadline_is_ten_minutes():
    import inspect

    assert packaging.DEFAULT_DEADLINE_SECONDS == 600
    assert inspect.signature(packaging.run_packaging).parameters["deadline_seconds"].default == 600
    assert packaging._PDFTOTEXT_TIMEOUT_SECONDS == 300  # per-call bound, further capped by the deadline


# -- OCR: deadline reserve and render scale ------------------------------------------


def _fake_ocr_tools(monkeypatch):
    """Replace subprocess.run inside packaging with a pdftoppm that writes a PNG and a
    tesseract that reads it back as text. Returns the list of commands run."""
    commands: list[list[str]] = []
    real_run = subprocess.run

    def run(cmd, **kw):
        tool = Path(cmd[0]).name
        if tool not in ("pdftoppm", "tesseract"):
            return real_run(cmd, **kw)  # pdftotext and friends are the real thing
        commands.append([tool, *cmd[1:]])
        if tool == "pdftoppm":
            Path(cmd[-1] + ".png").write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 0, b"words\n\f", b"")

    monkeypatch.setattr(packaging.subprocess, "run", run)
    return commands


def test_ocr_stops_starting_pages_inside_the_deadline_reserve(tmp_path, monkeypatch):
    commands = _fake_ocr_tools(monkeypatch)
    # Plenty of time while fewer than two pages are done, then less than the reserve.
    def left() -> float:
        done = sum(c[0] == "pdftoppm" for c in commands)
        return 200.0 if done < 2 else packaging.OCR_DEADLINE_RESERVE_SECONDS - 1

    texts, out_of_time = packaging._ocr_pages(
        "pdftoppm", "tesseract", tmp_path / "x.pdf", tmp_path, [1, 2, 3, 4], left, 600
    )

    assert out_of_time is True
    assert texts == ["words\n", "words\n"]
    assert [c[0] for c in commands].count("pdftoppm") == 2
    assert not list(tmp_path.glob("*.png"))


def test_ocr_without_a_deadline_never_stops_early(tmp_path, monkeypatch):
    _fake_ocr_tools(monkeypatch)
    texts, out_of_time = packaging._ocr_pages(
        "pdftoppm", "tesseract", tmp_path / "x.pdf", tmp_path, [1, 2, 3], lambda: None, None
    )
    assert len(texts) == 3 and out_of_time is False


def test_ocr_tool_failure_still_raises_even_with_time_left(tmp_path, monkeypatch):
    def run(cmd, **kw):
        if cmd[0] == "pdftoppm":
            Path(cmd[-1] + ".png").write_bytes(b"png")
            return subprocess.CompletedProcess(cmd, 0, b"", b"")
        return subprocess.CompletedProcess(cmd, 1, b"", b"Error in pixReadStream")

    monkeypatch.setattr(packaging.subprocess, "run", run)
    with pytest.raises(PackagingError, match="tesseract exited 1 on retained page 1: Error in pixReadStream"):
        packaging._ocr_pages("pdftoppm", "tesseract", tmp_path / "x.pdf", tmp_path, [1], lambda: 500.0, 600)
    assert not list(tmp_path.glob("*.png"))


def test_ocr_deadline_stop_publishes_what_was_read_and_says_so(src, tmp_path, monkeypatch, fake_ocr):
    # Two of six pages were read before the reserve was hit.
    monkeypatch.setattr(
        packaging, "_ocr_pages", lambda *a, **k: (["one\n", "two\n"], True)
    )
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")

    assert len(out.files) == 8
    stats = out.stats["ocr"]
    assert stats["status"] == "done" and stats["truncated"] is True
    assert stats["truncated_reason"] == "deadline"
    assert stats["pages"] == [1, 2] and stats["page_count"] == 2
    assert stats["truncated_pages"] == [3, 4, 5, 6]
    assert out.record["ocr"] == stats
    md = (tmp_path / "pkg" / "fs.ocr.md").read_text()
    assert "Truncated (deadline): only the first 2 target pages" in md
    assert len(re.findall(r"^## Original PDF page ", md, re.M)) == 2
    assert any("OCR covers 2 of 6 target pages (stopped: deadline)" in x for x in out.record["limitations"])


def test_ocr_out_of_time_before_the_first_page_is_skipped_not_fatal(src, tmp_path, monkeypatch, fake_ocr):
    monkeypatch.setattr(packaging, "_ocr_pages", lambda *a, **k: ([], True))
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")

    assert len(out.files) == 7
    stats = out.stats["ocr"]
    assert stats["status"] == "skipped" and "deadline" in stats["reason"]
    assert stats["truncated"] is True and stats["truncated_reason"] == "deadline"
    assert stats["truncated_pages"] == [1, 2, 3, 4, 5, 6] and stats["page_count"] == 0
    assert "ocr_text" not in out.record["artifacts"]
    assert not (tmp_path / "pkg" / "fs.ocr.md").exists()
    assert any("OCR covers 0 of 6" in x for x in out.record["limitations"])


def test_a_package_near_its_deadline_still_publishes_with_real_ocr_skipped(src, tmp_path, monkeypatch):
    # Not mocked: with the reserve larger than the whole deadline OCR never starts.
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: f"/fake/{name}" if name in ("pdftoppm", "tesseract") else real_which(name, *a, **k)
    )
    monkeypatch.setattr(packaging, "_tesseract_version", lambda exe: "tesseract 9.9.9")
    monkeypatch.setattr(packaging, "OCR_DEADLINE_RESERVE_SECONDS", 10_000)
    commands = _fake_ocr_tools(monkeypatch)
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs", deadline_seconds=600)

    assert len(out.files) == 7
    assert out.stats["ocr"]["status"] == "skipped"
    assert not [c for c in commands if c[0] in ("pdftoppm", "tesseract")]


def test_pdftoppm_gets_scale_to_only_for_pages_that_need_it(tmp_path, monkeypatch):
    commands = _fake_ocr_tools(monkeypatch)
    packaging._ocr_pages(
        "pdftoppm", "tesseract", tmp_path / "x.pdf", tmp_path, [1, 2], lambda: None, None, {2}
    )

    first, second = [c for c in commands if c[0] == "pdftoppm"]
    assert "-scale-to" not in first and first[first.index("-r") + 1] == "200"
    assert second[second.index("-scale-to") + 1] == "4000" and second[second.index("-r") + 1] == "200"


def _blank_pages(*sizes) -> list:
    writer = PdfWriter()
    for w, h in sizes:
        writer.add_blank_page(w, h)
    return list(writer.pages)


def test_needs_scale_follows_the_rendered_pixel_size():
    letter, a0, wide, big_but_ok = _blank_pages((612, 792), (2384, 3370), (3000, 400), (1440, 1440))
    assert not packaging._needs_scale(letter)  # 2200 px
    assert packaging._needs_scale(a0)  # 9361 px
    assert packaging._needs_scale(wide)  # 8333 px
    assert not packaging._needs_scale(big_but_ok)  # 4000 px exactly: not over
    wide[NameObject("/UserUnit")] = NumberObject(1)
    assert packaging._needs_scale(wide)

    class Broken:
        @property
        def mediabox(self):
            raise ValueError("bad box")

    assert packaging._needs_scale(Broken())  # unreadable: take the safe renderer


def test_ocr_stats_record_the_scale_even_when_unavailable(src, tmp_path, monkeypatch):
    real_which = shutil.which
    monkeypatch.setattr(
        packaging.shutil, "which", lambda name, *a, **k: None if name == "tesseract" else real_which(name, *a, **k)
    )
    out = package_pdf(src, standalone(ocr="all"), tmp_path / "pkg", stem="fs")
    assert out.stats["ocr"]["scale_to"] == 4000 and out.stats["ocr"]["scaled_pages"] == []


@needs_ocr
def test_ocr_scaled_pages_are_recorded_and_ordinary_pages_are_not(mixed_src, tmp_path):
    writer = PdfWriter()
    writer.append(PdfReader(str(mixed_src)), pages=[0, 1])
    writer.add_blank_page(3000, 4000)  # 8333 x 11111 px at 200 dpi
    big = tmp_path / "big.pdf"
    with big.open("wb") as f:
        writer.write(f)

    out = package_pdf(big, standalone(ocr="all"), tmp_path / "pkg", stem="big")

    stats = out.stats["ocr"]
    assert stats["status"] == "done" and stats["pages"] == [1, 2, 3]
    assert stats["scaled_pages"] == [3]
    assert (stats["dpi"], stats["scale_to"]) == (200, 4000)
    md = (tmp_path / "pkg" / "big.ocr.md").read_text()
    assert "Source pages 3 are larger than 4000 px at 200 dpi" in md
    assert "DICTIONARY" in md.upper()  # the unscaled scan page still reads at 200 dpi
