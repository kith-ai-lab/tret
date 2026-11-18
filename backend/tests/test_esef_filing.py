"""`services/esef_filing.py`: the pure half of the ESEF filing fallback.

Every fixture is built here, in memory, with `zipfile`; nothing touches the network.
`make_paged_report`, `make_section_report` and `make_esef_zip` are reused by the
capture and packaging tests.
"""
from __future__ import annotations

import io
import zipfile

import pytest

from tret.net.fetch.extract import html_to_text
from tret.net.fetch.snapshot import MAX_EXTRACTED_CHARS as SNAPSHOT_MAX_EXTRACTED_CHARS
from tret.services import esef_filing as ef

XHTML_HEAD = (
    '<?xml version="1.0" encoding="utf-8"?>'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">'
    "<head><title>Annual report</title><style>.page{}</style></head><body>"
    '<div style="display:none"><ix:header><ix:hidden><ix:nonNumeric>SECRET-FACT</ix:nonNumeric>'
    "</ix:hidden></ix:header></div>"
)


def make_paged_report(pages: int = 6, *, container: str = '<div class="page">') -> bytes:
    body = "".join(
        f"{container}<p>Body of page {i} about emissions</p>"
        f"<table><tr><td>Scope 1</td><td>{i * 100}</td><td></td></tr></table><p>{i}</p></div>"
        for i in range(1, pages + 1)
    )
    return (XHTML_HEAD + body + "</body></html>").encode()


def make_section_report(sections: int = 5) -> bytes:
    body = "".join(
        f"<h{1 if i == 1 else 2}>Chapter {i} title</h{1 if i == 1 else 2}>"
        f"<p>Text of chapter {i}. It is about topic {i}.</p>"
        f"<h3>Sub {i}.1</h3><p>Details {i}.1</p>"
        for i in range(1, sections + 1)
    )
    return (XHTML_HEAD + body + "</body></html>").encode()


def make_esef_zip(report: bytes, name: str = "acme-2025", extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("META-INF/taxonomyPackage.xml", "<tp/>")
        zf.writestr("www.example.com/taxonomy.xsd", "<xsd/>")
        zf.writestr(f"{name}/reports/{name}.xhtml", report)
        for member, data in (extra or {}).items():
            zf.writestr(member, data)
    return buf.getvalue()


def _zip(entries: dict[str, bytes]) -> zipfile.ZipFile:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return zipfile.ZipFile(io.BytesIO(buf.getvalue()))


# ── member picking ────────────────────────────────────────────────────────────
def test_the_report_is_found_when_the_top_folder_differs_from_the_package_name():
    # An XBRL Report Package: {top}/reports/{file}, whatever the .xbri/.zip file is called.
    zf = zipfile.ZipFile(io.BytesIO(make_esef_zip(make_paged_report(), name="5QK37QC7NWOJ8D7WVQ45-2025-12-31-1-es")))
    assert ef.pick_report_member(zf) == (
        "5QK37QC7NWOJ8D7WVQ45-2025-12-31-1-es/reports/5QK37QC7NWOJ8D7WVQ45-2025-12-31-1-es.xhtml")


def test_xbri_package_urls_are_accepted_as_they_are():
    url = "https://filings.xbrl.org/5QK37QC7NWOJ8D7WVQ45/2025-12-31/ESEF/ES/0/5QK37QC7NWOJ8D7WVQ45-2025-12-31.xbri"
    assert ef.package_url_from_report_url(url) == url


def test_picks_the_report_member():
    zf = zipfile.ZipFile(io.BytesIO(make_esef_zip(make_paged_report())))
    assert ef.pick_report_member(zf) == "acme-2025/reports/acme-2025.xhtml"
    choice = ef.select_report_member(zf)
    assert choice.others == [] and not choice.ambiguous


def test_html_extension_and_case_are_accepted():
    zf = _zip({"X/Reports/X.HTML": b"<p>a</p>", "META-INF/a.xml": b"<a/>"})
    assert ef.pick_report_member(zf) == "X/Reports/X.HTML"


@pytest.mark.parametrize("name", ["../evil/reports/e.xhtml", "/abs/reports/e.xhtml", "C:/x/reports/e.xhtml"])
def test_unsafe_entry_names_refuse_the_archive(name):
    zf = _zip({"ok/reports/ok.xhtml": b"<p>x</p>", name: b"<p>y</p>"})
    with pytest.raises(ef.FilingError, match="unsafe archive entry"):
        ef.pick_report_member(zf)


def test_symlink_entries_are_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok/reports/ok.xhtml", b"<p>x</p>")
        link = zipfile.ZipInfo("ok/link")
        link.external_attr = 0o120777 << 16
        zf.writestr(link, "/etc/passwd")
    with pytest.raises(ef.FilingError, match="symbolic link"):
        ef.pick_report_member(zipfile.ZipFile(io.BytesIO(buf.getvalue())))


def test_several_candidates_pick_the_largest_and_record_the_rest():
    zf = _zip(
        {
            "a/reports/small.xhtml": b"<p>x</p>",
            "a/reports/big.xhtml": b"<p>" + b"y" * 500 + b"</p>",
            "a/reports/mid.html": b"<p>" + b"z" * 300 + b"</p>",
        }
    )
    choice = ef.select_report_member(zf)
    assert choice.name == "a/reports/big.xhtml"
    assert choice.others == ["a/reports/mid.html", "a/reports/small.xhtml"]
    assert choice.ambiguous  # 500 is under twice 300
    clear = ef.select_report_member(_zip({"a/reports/big.xhtml": b"y" * 900, "a/reports/s.xhtml": b"y" * 100}))
    assert not clear.ambiguous


def test_a_package_without_a_report_is_refused():
    with pytest.raises(ef.FilingError, match="no report"):
        ef.pick_report_member(_zip({"META-INF/a.xml": b"<a/>", "x/other/e.xhtml": b"<p/>"}))


def test_zip_bombs_are_refused_on_the_claimed_and_the_streamed_size():
    zf = _zip({"a/reports/a.xhtml": b"<p>" + b"a" * 5000 + b"</p>"})
    with pytest.raises(ef.FilingError, match="expands to"):
        ef.refuse_oversized_archive(zf, max_uncompressed=1000)
    with pytest.raises(ef.FilingError, match="uncompressed"):
        ef.MemberStream(zf, "a/reports/a.xhtml", max_bytes=1000)
    stream = ef.MemberStream(zf, "a/reports/a.xhtml")
    assert b"".join(stream).startswith(b"<p>") and stream.size == 5007


def test_member_hash_covers_the_member_even_when_parsing_stops_early():
    report = make_paged_report()
    zf = zipfile.ZipFile(io.BytesIO(make_esef_zip(report)))
    stream = ef.MemberStream(zf, "acme-2025/reports/acme-2025.xhtml")
    ef.extract_filing_text(stream, max_collect_chars=50)  # stops after a couple of lines
    stream.drain()
    import hashlib

    assert stream.sha256 == hashlib.sha256(report).hexdigest()


# ── report -> package URL ─────────────────────────────────────────────────────
PKG = "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme-2025.zip"
REPORT = "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/acme-2025/reports/acme-2025.xhtml"


def test_package_url_from_a_report_url():
    assert ef.package_url_from_report_url(REPORT) == PKG
    assert ef.package_url_from_report_url(REPORT.replace(".xhtml", ".html") + "?x=1#f") == PKG
    assert ef.package_url_from_report_url(PKG) == PKG
    assert ef.lei_from_filing_url(PKG) == "529900T8BM49AURSDO55"


@pytest.mark.parametrize(
    "url",
    [
        REPORT.replace("filings.xbrl.org", "evil.example.com"),
        REPORT.replace("filings.xbrl.org", "filings.xbrl.org.evil.example.com"),
        REPORT.replace("https:", "http:"),
        "https://user:pw@filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/a.zip",
        "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/a/reports/../x/reports/a.xhtml",
        "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/ESEF/DE/0/a.pdf",
        "https://filings.xbrl.org/529900T8BM49AURSDO55/2025-12-31/XYZ/DE/0/a/reports/a.xhtml",
        "https://filings.xbrl.org/reports/a.xhtml",
        "",
    ],
)
def test_package_url_refuses_other_hosts_and_shapes(url):
    with pytest.raises(ef.FilingError):
        ef.package_url_from_report_url(url)


# ── paged layout ──────────────────────────────────────────────────────────────
def test_paged_filing_emits_page_markers_folios_and_skips_hidden_content():
    ft = ef.extract_filing_text(make_paged_report(6))
    assert ft.layout == "paged" and ft.unit == "filing_page" and ft.unit_count == 6
    assert [u.number for u in ft.units] == [1, 2, 3, 4, 5, 6]
    for n in range(1, 7):
        assert f"## Page {n}\nBody of page {n} about emissions\n" in ft.text
    assert "SECRET-FACT" not in ft.text  # ix:header inside a display:none div
    assert ft.folios == {n: n for n in range(1, 7)}
    assert not ft.text_truncated and ft.units_dropped == 0
    assert ft.structure["signal"].startswith("containers@depth0")


def test_table_rows_stay_on_their_own_lines_with_cells_separated():
    ft = ef.extract_filing_text(make_paged_report(6))
    assert "\nScope 1 | 300\n" in ft.text  # the trailing empty cell is dropped
    page3 = ft.units[2]
    assert [ln.kind for ln in page3.lines] == ["p", "row", "p"]
    assert page3.lines[1].cells == ("Scope 1", "300")


def test_ix_header_without_a_hidden_wrapper_and_hidden_attribute_are_skipped():
    html = (
        b"<html><body><ix:header><ix:references>REF-A</ix:references></ix:header>"
        b"<p>Visible</p><p hidden>Not shown</p><span style='color:red;display: none'>Gone</span>"
        b"<h1>Title</h1><h1>Two</h1><h1>Three</h1></body></html>"
    )
    ft = ef.extract_filing_text(html)
    assert "REF-A" not in ft.text and "Not shown" not in ft.text and "Gone" not in ft.text
    assert "Visible" in ft.text


def test_nested_page_wrapper_uses_the_depth_that_holds_the_pages():
    inner = "".join(f'<div class="pf"><p>Inner page {i}</p></div>' for i in range(1, 7))
    html = f'<html><body><div class="page">{inner}</div></body></html>'.encode()
    ft = ef.extract_filing_text(html)
    assert ft.layout == "paged" and ft.unit_count == 6
    assert ft.structure["signal"].startswith("containers@depth1")
    assert "## Page 6\nInner page 6" in ft.text


def test_text_outside_page_containers_joins_the_page_before_it():
    pages = "".join(f'<div class="page"><p>Page {i} {"filler " * 20}</p></div>' for i in range(1, 7))
    html = f"<html><body><p>Cover words</p>{pages}<p>Tail words</p></body></html>".encode()
    ft = ef.extract_filing_text(html)
    assert ft.layout == "paged" and ft.unit_count == 6
    assert ft.units[0].text.startswith("Cover words")
    assert ft.units[5].text.endswith("Tail words")


def test_empty_page_containers_still_count_as_pages():
    html = "<html><body>" + '<div class="page"><img src="a.png"></div>' + "".join(
        f'<div class="page"><p>Text {i}</p></div>' for i in range(1, 7)
    ) + "</body></html>"
    ft = ef.extract_filing_text(html.encode())
    assert ft.unit_count == 7 and ft.units[0].text == "" and ft.units[0].images == 1
    assert "## Page 1\n\n## Page 2\nText 1" in ft.text


def test_page_break_styles_between_blocks_make_pages():
    blocks = "".join(
        f'<p>Block {i} words</p><div style="page-break-after: always"></div>' for i in range(1, 8)
    )
    ft = ef.extract_filing_text(f"<html><body>{blocks}</body></html>".encode())
    assert ft.layout == "paged" and ft.unit_count == 7
    assert ft.structure["signal"].startswith("page-breaks")


def test_page_break_on_headings_is_a_chapter_break_not_a_page():
    chapters = "".join(
        f'<h1 style="page-break-before: always">Chapter {i}</h1><p>Words {i}</p>' for i in range(1, 8)
    )
    ft = ef.extract_filing_text(f"<html><body>{chapters}</body></html>".encode())
    assert ft.layout == "sections" and ft.unit_count == 7


def test_fixed_height_positioned_boxes_count_as_pages():
    box = '<div style="position:relative;width:595pt;height:842pt"><p>Boxed {i}</p></div>'
    html = "<html><body>" + "".join(box.format(i=i) for i in range(6)) + "</body></html>"
    ft = ef.extract_filing_text(html.encode())
    assert ft.layout == "paged" and ft.unit_count == 6


def test_fewer_than_five_pages_is_not_paged():
    ft = ef.extract_filing_text(make_paged_report(4))
    assert ft.layout == "sections"


def test_giant_pages_are_not_paged():
    big = "".join(f'<div class="page"><p>{"w " * 12_000}</p></div>' for _ in range(6))
    ft = ef.extract_filing_text(f"<html><body>{big}</body></html>".encode())
    assert ft.layout == "sections"  # median page far over a page's worth of text


# ── sections ──────────────────────────────────────────────────────────────────
def test_sections_use_h1_to_h3_headings():
    ft = ef.extract_filing_text(make_section_report(5))
    assert ft.layout == "sections" and ft.unit == "filing_section"
    assert ft.structure["signal"] == "h1-h3"
    assert ft.unit_count == 10  # five chapters, five sub-headings
    assert ft.text.startswith("## Section 1: Chapter 1 title\nText of chapter 1. It is about topic 1.")
    assert "\n\n## Section 2: Sub 1.1\nDetails 1.1\n\n## Section 3: Chapter 2 title" in ft.text
    assert "SECRET-FACT" not in ft.text and ft.folios == {}


def test_text_before_the_first_heading_becomes_a_front_matter_unit():
    html = b"<html><body><p>Cover</p><h1>A</h1><p>a</p><h2>B</h2><p>b</p><h3>C</h3><p>c</p></body></html>"
    ft = ef.extract_filing_text(html)
    assert ft.units[0].heading == "(start of filing)" and ft.units[0].text == "Cover"
    assert [u.heading for u in ft.units[1:]] == ["A", "B", "C"]


def test_h4_then_bold_lines_are_the_fallbacks():
    h4 = b"<html><body><h4>One</h4><p>a</p><h4>Two</h4><p>b</p><h4>Three</h4><p>c</p></body></html>"
    assert ef.extract_filing_text(h4).structure["signal"] == "h1-h4"
    bold = (
        b"<html><body><p><b>Alpha</b></p><p>a</p><p><strong>Beta</strong></p><p>b</p>"
        b"<p style='font-weight:700'>Gamma</p><p>c <b>partly</b></p></body></html>"
    )
    ft = ef.extract_filing_text(bold)
    assert ft.structure["signal"] == "bold-lines"
    assert [u.heading for u in ft.units] == ["Alpha", "Beta", "Gamma"]
    assert ft.units[2].text == "c partly"


def test_no_structure_at_all_is_one_unit():
    ft = ef.extract_filing_text(b"<html><body><p>Just text.</p><p>More text.</p></body></html>")
    assert ft.unit_count == 1 and ft.text == "## Section 1: Filing text\nJust text.\nMore text."
    assert ft.structure["signal"] == "none"


def test_a_body_line_that_looks_like_a_marker_is_escaped():
    html = b"<html><body><h1>A</h1><p>## Page 3</p><h1>B</h1><h1>C</h1></body></html>"
    ft = ef.extract_filing_text(html)
    assert "\n## Page 3" not in ft.text and "\\## Page 3" in ft.text


def test_empty_reports_are_refused():
    with pytest.raises(ef.FilingError, match="no readable text"):
        ef.extract_filing_text(b'<html><body><div style="display:none">x</div></body></html>')


def test_streams_and_encodings():
    report = make_section_report(3)
    chunks = [report[i : i + 7] for i in range(0, len(report), 7)]
    assert ef.extract_filing_text(iter(chunks)).text == ef.extract_filing_text(report).text
    latin = '<?xml version="1.0" encoding="ISO-8859-1"?><html><body><h1>Café</h1><h1>B</h1><h1>C</h1></body></html>'
    assert "Café" in ef.extract_filing_text(latin.encode("latin-1")).text


# ── the 2,000,000-character cap, by whole units ───────────────────────────────
def test_the_cap_matches_the_snapshot_cap():
    assert ef.MAX_EXTRACTED_CHARS == SNAPSHOT_MAX_EXTRACTED_CHARS == 2_000_000


def test_truncation_keeps_whole_units_and_counts_the_rest():
    ft = ef.extract_filing_text(make_paged_report(8), max_chars=260)
    assert ft.text_truncated and ft.units_total == 8 and 0 < ft.unit_count < 8
    assert ft.units_dropped == 8 - ft.unit_count
    assert len(ft.text) <= 260
    full = ef.extract_filing_text(make_paged_report(8))
    assert ft.text == ef.render_unit_blocks(full.units[: ft.unit_count])  # never cut mid-unit
    assert not full.text_truncated


def test_a_first_unit_over_the_cap_is_cut_at_a_line():
    html = b"<html><body>" + b"".join(b"<p>line number %d</p>" % i for i in range(100)) + b"</body></html>"
    ft = ef.extract_filing_text(html, max_chars=200)
    assert ft.unit_count == 1 and ft.text_truncated and ft.structure["first_unit_cut"]
    assert len(ft.text) <= 200 and ft.text.endswith(tuple("0123456789"))


# ── html_to_text (the research reader) learned the same two skips ──────────────
def test_html_to_text_skips_ix_header_and_display_none():
    html = (
        "<html><body><p>Keep</p><ix:header><ix:hidden>FACT</ix:hidden></ix:header>"
        '<div style="display:none"><div>inner</div><p>HIDDEN</p></div><p>After</p>'
        '<br style="display:none"><p>Last</p></body></html>'
    )
    text, _ = html_to_text(html)
    assert "FACT" not in text and "HIDDEN" not in text and "inner" not in text
    assert text.split() == ["Keep", "After", "Last"]
    assert html_to_text("<p>Plain</p><span style='display:block'>shown</span>")[0].split() == ["Plain", "shown"]


# ── what the packager writes and reads back ─────────────────────────────────────
def test_extract_html_is_attribute_free_and_reads_back():
    ft = ef.extract_filing_text(make_paged_report(6))
    kept = [ft.units[1], ft.units[3]]
    document = ef.render_extract_html("A <title> & more", kept, source_note="note")
    assert "<script" not in document and "href=" not in document and "<img" not in document
    units, foreign = ef.html_unit_lines(document)
    assert foreign == [] and sorted(units) == [2, 4]
    assert units[2] == [ln.text for ln in kept[0].lines]
    assert units[4][1] == "Scope 1 | 400"
    assert "&lt;title&gt;" in document and 'data-folio="2"' in document
    _, foreign = ef.html_unit_lines("<p>x</p><script>1</script><a href='x'>y</a>")
    assert foreign == ["script", "a"]


# ── layouts met in real filings (regressions; fixtures are small synthetic copies) ──
def test_awpage_class_is_a_page_container():
    pages = "".join(f'<div class="awdiv awpage shw1"><p>Pagina {i} {"testo " * 30}</p></div>' for i in range(1, 8))
    ft = ef.extract_filing_text(f"<html><body>{pages}</body></html>".encode())
    assert ft.layout == "paged" and ft.unit_count == 7


def test_identical_classed_siblings_with_digit_variants_are_pages_by_shape():
    # PDF converters with obfuscated names: `a0_ a0_02`, `a1_ a1_25` ... one div per page.
    body = "".join(
        f'<div class="a{i % 3}_ a{i % 3}_{i + 10}"><div class="a0_view"><p>Page {i} {"words " * 150}</p></div></div>'
        for i in range(12)
    )
    ft = ef.extract_filing_text(f"<html><body>{body}</body></html>".encode())
    assert ft.layout == "paged" and ft.unit_count == 12 and ft.structure["signal"].startswith("shape@")


def test_repeated_paragraph_divs_are_not_pages():
    body = "".join(f'<div class="para"><span>Short paragraph {i}</span></div>' for i in range(200))
    assert ef.extract_filing_text(f"<html><body>{body}</body></html>".encode()).layout == "sections"


def test_a_few_giant_chapters_with_page_breaks_are_not_pages_even_if_the_median_is_small():
    # Median ~ page-sized but a handful of enormous chapters: the mean rejects it.
    small = "".join(f'<p style="page-break-before:always">{"s " * 300}</p>' for _ in range(7))
    big = "".join(f'<p style="page-break-before:always">{"b " * 200_000}</p>' for _ in range(3))
    ft = ef.extract_filing_text(f"<html><body>{small}{big}</body></html>".encode())
    assert ft.layout == "sections"


def test_folios_are_read_from_header_rows_and_noise_is_dropped():
    pages = "".join(
        f"<div class='page'><table><tr><td>ACME ANNUAL REPORT</td><td></td><td>{i + 2}</td></tr></table>"
        f"<p>{'body ' * 30}</p><table><tr><td>Metric alpha</td><td>1</td><td>{i * 7}</td></tr></table></div>"
        for i in range(1, 9)
    )
    ft = ef.extract_filing_text(f"<html><body>{pages}</body></html>".encode())
    assert ft.folios == {i: i + 2 for i in range(1, 9)}
    noisy = "".join(
        f"<div class='page'><p>{'body ' * 30}</p><table><tr><td>Metric alpha</td><td>{i * 37 % 91}</td></tr></table></div>"
        for i in range(1, 9)
    )
    assert ef.extract_filing_text(f"<html><body>{noisy}</body></html>".encode()).folios == {}


def test_a_larger_text_budget_keeps_late_units():
    html = make_section_report(40)
    small = ef.extract_filing_text(html, max_chars=2000)
    big = ef.extract_filing_text(html, max_chars=8_000_000)
    assert small.units_dropped > 0 and big.units_dropped == 0 and big.unit_count == 80
    from tret.config import Settings

    assert Settings().esef_filing_max_chars == 8_000_000


def test_filing_documents_get_a_larger_chunk_ceiling():
    from tret.services import retrieval

    text = "\n\n".join(f"## Page {i}\n" + ("lorem ipsum dolor " * 150) for i in range(1, 2600))
    plain = retrieval.chunk_document("x.pdf", text, {})
    filing = retrieval.chunk_document("x.zip", text, {"provenance": "esef_filing"})
    assert len(plain) == retrieval.MAX_CHUNKS_PER_DOCUMENT + 1
    assert len(filing) > len(plain) and len(filing) <= retrieval.MAX_CHUNKS_PER_FILING + 1


# ── html_to_text: hidden skipping is for inline XBRL only, and survives omitted end tags ──
def test_html_to_text_ordinary_pages_keep_the_old_behaviour_with_omitted_end_tags():
    for html in (
        '<ul><li style="display:none">Skip<li>Home</ul><h1>Title</h1><p>Body text</p>',
        '<p style="display:none">x<div>After</div><p>Body text</p>',
    ):
        text, _ = html_to_text(html)
        assert "Body text" in text and ("Title" in text or "After" in text)
    # display:none is NOT honoured on an ordinary page: nothing is lost.
    assert "Skip" in html_to_text('<p style="display:none">Skip</p><p>Rest</p>')[0]


def test_html_to_text_ixbrl_hidden_regions_close_on_the_parents_end_tag():
    ix = '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>'
    text, _ = html_to_text(ix + '<ul><li style="display:none">Skip<li>Home</ul><h1>Title</h1><p>Body</p></body></html>')
    # `Home` sits inside the unclosed hidden <li>, but `</ul>` ends it: the rest survives.
    assert "Skip" not in text and text.split() == ["Title", "Body"]
    text, _ = html_to_text(ix + '<div><p style="display:none">x<div>inner</div></div><p>After</p></body></html>')
    assert text.split() == ["After"]
    text, _ = html_to_text(ix + "<ix:header><ix:hidden>F</ix:hidden></ix:header><p>Keep</p></body></html>")
    assert text.split() == ["Keep"]


# ── hardening: control characters, entry counts, pathological nesting ───────────────
@pytest.mark.parametrize("name", ["a/reports/x\ny.xhtml", "a/reports/x\x7fy.xhtml", "a/reports/x\u202ey.xhtml",
                                  "a/reports/x\u2028y.xhtml"])
def test_control_characters_in_member_names_refuse_the_archive(name):
    with pytest.raises(ef.FilingError, match="control character"):
        ef.pick_report_member(_zip({"ok/reports/ok.xhtml": b"<p>x</p>", name: b"<p>y</p>"}))


def test_member_names_are_clipped_and_escaped_for_display():
    shown = ef.clip_name("a\nb" + "x" * 500)
    assert "\n" not in shown and len(shown) < 140 and "nb" in shown and shown.endswith("...'")


def test_too_many_entries_are_refused():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(ef.MAX_ARCHIVE_ENTRIES + 1):
            zf.writestr(f"f{i}", b"")
    with pytest.raises(ef.FilingError, match="entries"):
        ef.refuse_oversized_archive(zipfile.ZipFile(io.BytesIO(buf.getvalue())))


def test_the_duplicated_constants_agree_with_their_sources():
    from tret.net import policy
    from tret.services.documents import MAX_ZIP_UNCOMPRESSED_BYTES

    assert ef.MAX_ARCHIVE_UNCOMPRESSED_BYTES == MAX_ZIP_UNCOMPRESSED_BYTES
    assert policy.FILING_HOSTS == frozenset({ef.FILINGS_HOST})


def test_pathological_nesting_finishes_fast():
    import time

    n = 20_000
    nested = '<div class="pf">' * n + "<p>deep text</p>" + "</div>" * n
    stray = "</span>" * 50_000
    started = time.monotonic()
    ft = ef.extract_filing_text(f"<html><body>{nested}{stray}<p>tail</p></body></html>".encode())
    assert time.monotonic() - started < 10
    assert "deep text" in ft.text and "tail" in ft.text
    shallow = "".join(f'<div class="pf"><p>Page {i} {"w " * 40}</p></div>' for i in range(300))
    started = time.monotonic()
    ef.extract_filing_text(f'<html><body>{"<div class=pf>" * 400}{shallow}</body></html>'.encode())
    assert time.monotonic() - started < 10
