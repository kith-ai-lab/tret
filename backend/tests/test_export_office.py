"""Word and PowerPoint deliverable export (`services/export_office`)."""
from __future__ import annotations

import io
import uuid

import pytest
from docx import Document
from docx.oxml.ns import qn
from pptx import Presentation

from tret.services import export as export_module
from tret.services import export_office
from tret.services.export import (
    EXPORT_FORMATS,
    DeliverableEmpty,
    render_deliverable_bytes,
)
from tret.services.export_office import (
    DOCX_CONTENT_TYPE,
    MAX_BULLETS_PER_SLIDE,
    PPTX_CONTENT_TYPE,
    clean_text,
    parse_html,
    render_docx,
    render_pptx,
)
from tret.services.html_sanitize import render_markdown

SECTIONS = [
    {
        "section": "governance",
        "status": "approved",
        "approval_purpose": "auto",
        "model": "m1",
        "doctrine_sha": "abc",
        "energy_wh": 1.234,
        "co2e_g": 0.5,
    },
    {"section": "strategy", "status": "approved", "model": "m2", "doctrine_sha": "abc"},
]

MARKDOWN = """# Climate Disclosure

_Assembled 2026-11-16 10:00 UTC from 2 approved section(s)._

---

## Governance

Board oversight is **quarterly**, see the [audit page](https://example.com/audit).

- Board
    - Audit committee
- Executive team

Steps follow.

1. First step
2. Second step

| Metric | FY24 | FY25 |
|---|---:|---:|
| Scope 1 | 1,204 | 1,090 |
| Scope 2 | 3,400 | 3,100 |

> Key point: emissions fell 9%.

    x = 1

<script>alert(1)</script>

---

## Strategy

### Detail

Plan text.
"""


def _body(md: str = MARKDOWN) -> str:
    return render_markdown(md)


def _docx(theme=None, accent=None, subtitle=None, md: str = MARKDOWN):
    data = render_docx(_body(md), "climate-disclosure", SECTIONS, "abc123", theme=theme, accent=accent, subtitle=subtitle)
    assert data.startswith(b"PK")
    return Document(io.BytesIO(data))


def _pptx(theme=None, accent=None, subtitle=None, md: str = MARKDOWN):
    data = render_pptx(_body(md), "climate-disclosure", SECTIONS, "abc123", theme=theme, accent=accent, subtitle=subtitle)
    assert data.startswith(b"PK")
    return Presentation(io.BytesIO(data))


def _slide_texts(prs) -> list[str]:
    out = []
    for slide in prs.slides:
        parts = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                parts.append(shape.text_frame.text)
            if getattr(shape, "has_table", False) and shape.has_table:
                for row in shape.table.rows:
                    parts.extend(c.text for c in row.cells)
        out.append("\n".join(parts))
    return out


def _all_docx_text(doc) -> str:
    parts = [p.text for p in doc.paragraphs]
    for table in doc.tables:
        parts.extend(c.text for row in table.rows for c in row.cells)
    return "\n".join(parts)


def _header_fill(doc) -> str:
    tc_pr = doc.tables[0].rows[0].cells[0]._tc.tcPr
    return tc_pr.find(qn("w:shd")).get(qn("w:fill"))


# ---- block model ----------------------------------------------------------


def test_parse_block_model():
    blocks = parse_html(_body())
    kinds = [b.kind for b in blocks]
    assert kinds[0] == "heading" and blocks[0].level == 1
    assert {"para", "li", "table", "quote", "code", "rule"} <= set(kinds)
    table = next(b for b in blocks if b.kind == "table")
    assert table.header and table.rows[1][0][0].text == "Scope 1"
    link = next(s for b in blocks if b.kind == "para" for s in b.runs if s.url)
    assert link.text == "audit page" and link.url == "https://example.com/audit"
    nested = [b.level for b in blocks if b.kind == "li" and not b.ordered]
    assert nested == [0, 1, 0]


def test_unsafe_link_scheme_keeps_text_without_url():
    blocks = parse_html(_body("[click](javascript:alert(1)) and [ok](https://example.com)"))
    runs = blocks[0].runs
    assert [r.url for r in runs if r.text == "click"] == [None]
    assert [r.url for r in runs if r.text == "ok"] == ["https://example.com"]


def test_clean_text_strips_xml_illegal_characters():
    assert clean_text("a\x00b\x0bc\x1fd\ud800e￾") == "abcde"
    assert clean_text("tab\tnl\n") == "tab\tnl\n"


# ---- docx ------------------------------------------------------------------


def test_docx_report_cover_headings_table_lists():
    doc = _docx("report", subtitle="Acme Ltd")
    texts = [p.text for p in doc.paragraphs]
    assert doc.paragraphs[1].style.name == "Title" and texts[1] == "Climate Disclosure"
    assert "Acme Ltd" in texts
    assert doc.core_properties.title == "Climate Disclosure"
    assert doc.core_properties.subject == "Acme Ltd"
    styles = {p.text: p.style.name for p in doc.paragraphs}
    assert styles["Governance"] == "Heading 2"
    assert styles["Detail"] == "Heading 3"
    assert styles["Provenance"] == "Heading 2"
    table = doc.tables[0]
    assert [c.text for c in table.rows[0].cells] == ["Metric", "FY24", "FY25"]
    assert [c.text for c in table.rows[1].cells] == ["Scope 1", "1,204", "1,090"]
    assert table.rows[0]._tr.trPr.find(qn("w:tblHeader")) is not None
    by_text = {p.text: p.style.name for p in doc.paragraphs}
    assert by_text["Board"] == "List Bullet"
    assert by_text["Audit committee"] == "List Bullet 2"
    assert by_text["First step"] == "List Number"
    assert by_text["Key point: emissions fell 9%."] == "Normal"
    assert doc.sections[0].different_first_page_header_footer
    assert "CLIMATE DISCLOSURE" in doc.sections[0].header.paragraphs[0].text
    footer_xml = doc.sections[0].footer._element.xml
    assert "PAGE" in footer_xml and "NUMPAGES" in footer_xml


def test_docx_plain_has_no_cover_and_default_colours():
    doc = _docx(None)
    assert doc.paragraphs[0].style.name == "Heading 1"
    assert doc.paragraphs[0].text == "Climate Disclosure"
    assert not doc.sections[0].different_first_page_header_footer
    assert "NUMPAGES" in doc.sections[0].footer._element.xml
    assert _header_fill(doc) == "F0F0F0"
    heading_colour = doc.styles["Heading 1"].font.color.rgb
    assert str(heading_colour) == "000000"
    assert _docx("plain")  # explicit plain is accepted too


def test_docx_accent_override_changes_header_fill():
    assert _header_fill(_docx("report")) == export_office.DEFAULT_ACCENT[1:]
    assert _header_fill(_docx("report", accent="#AA3311")) == "aa3311"
    assert str(_docx("report", accent="#AA3311").styles["Heading 1"].font.color.rgb).lower() == "aa3311"


def test_docx_hyperlink_is_real():
    doc = _docx("report")
    rels = [r.target_ref for r in doc.part.rels.values() if r.is_external]
    assert "https://example.com/audit" in rels
    assert "audit page" in _all_docx_text(doc)


def test_docx_script_is_literal_text_only():
    doc = _docx("report")
    assert "<script>alert(1)</script>" in _all_docx_text(doc)
    data = render_docx(_body(), "x", SECTIONS, None, theme="report")
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode()
    assert "<script" not in xml and "&lt;script&gt;" in xml


@pytest.mark.parametrize("fn", [_docx, _pptx])
def test_provenance_appendix_says_approved_automatic(fn):
    for theme in (None, "report"):
        obj = fn(theme)
        text = _all_docx_text(obj) if fn is _docx else "\n".join(_slide_texts(obj))
        assert "approved (automatic)" in text
        assert "Provenance" in text
        assert "1 of 2 sections were approved automatically" in text
        assert "not reviewed by a person" in text


def test_provenance_without_auto_uses_named_reviewer_sentence():
    data = render_docx(_body(), "x", [SECTIONS[1]], None)
    text = _all_docx_text(Document(io.BytesIO(data)))
    assert "approved by a named reviewer" in text
    assert "approved (automatic)" not in text


def test_docx_survives_hostile_input():
    long_word = "A" * 6000
    md = f"# T\n\n## S\n\nbefore\x00after \x0b {long_word}\n\n| a | b |\n|---|---|\n| {long_word} | x\x01y |\n"
    for theme in (None, "report"):
        doc = _docx(theme, md=md)
        assert "beforeafter" in _all_docx_text(doc)
    prs = _pptx("report", md=md)
    assert len(prs.slides) >= 3


# ---- pptx ------------------------------------------------------------------


def test_pptx_title_slide_and_structure():
    prs = _pptx("report", subtitle="Acme Ltd")
    assert prs.slide_width / prs.slide_height == pytest.approx(16 / 9, rel=0.01)
    texts = _slide_texts(prs)
    assert len(prs.slides) >= 3
    assert "Climate Disclosure" in texts[0] and "Acme Ltd" in texts[0]
    titles = [s.shapes.title.text_frame.text for s in prs.slides]
    assert "Governance" in titles and "Strategy" in titles
    assert "Provenance" in titles
    joined = "\n".join(texts)
    assert "Board oversight is quarterly" in joined
    assert "Audit committee" in joined and "First step" in joined
    assert "Key point: emissions fell 9%." in joined
    assert "<script>alert(1)</script>" in joined


def test_pptx_table_slide_and_header_fill():
    prs = _pptx("report", accent="#AA3311")
    tables = [sh for s in prs.slides for sh in s.shapes if sh.has_table]
    assert tables
    table = tables[0].table
    assert [c.text for c in table.rows[0].cells] == ["Metric", "FY24", "FY25"]
    assert [c.text for c in table.rows[1].cells] == ["Scope 1", "1,204", "1,090"]
    assert str(table.cell(0, 0).fill.fore_color.rgb) == "AA3311"
    plain = _pptx(None)
    plain_table = next(sh for s in plain.slides for sh in s.shapes if sh.has_table).table
    assert str(plain_table.cell(0, 0).fill.fore_color.rgb) == "F0F0F0"


def test_pptx_slide_numbers_except_title():
    prs = _pptx("report")
    for index, slide in enumerate(prs.slides):
        has_field = "slidenum" in slide.shapes._spTree.xml
        assert has_field == (index > 0)


def test_pptx_long_section_continues_and_respects_caps():
    items = "\n".join(f"- point number {i} with some explanatory words" for i in range(40))
    prose = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    md = f"# Big\n\n## Long\n\n{items}\n\n{prose}\n"
    prs = _pptx("plain", md=md)
    titles = [s.shapes.title.text_frame.text for s in prs.slides]
    assert "Long" in titles and "Long (cont.)" in titles
    assert sum(t.startswith("Long (cont.)") for t in titles) >= 4
    assert not any("(cont.) (cont.)" in t for t in titles)
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape != slide.shapes.title:
                paragraphs = [p for p in shape.text_frame.paragraphs if p.text.strip()]
                assert len(paragraphs) <= MAX_BULLETS_PER_SLIDE
                for p in paragraphs:
                    for run in p.runs:
                        if run.font.size is not None:
                            assert run.font.size.pt >= 14


def test_pptx_wide_long_table_is_truncated_with_note():
    cols = " | ".join(f"c{i}" for i in range(9))
    sep = " | ".join("---" for _ in range(9))
    rows = "\n".join("| " + " | ".join(f"r{r}c{i}" + "x" * 90 for i in range(9)) + " |" for r in range(30))
    md = f"# T\n\n## Wide\n\n| {cols} |\n| {sep} |\n{rows}\n"
    prs = _pptx("report", md=md)
    slide = next(s for s in prs.slides if any(sh.has_table for sh in s.shapes))
    table = next(sh for sh in slide.shapes if sh.has_table).table
    assert len(table.columns) == export_office.MAX_TABLE_COLS
    assert len(table.rows) < 31
    assert export_office._TABLE_NOTE in "\n".join(
        sh.text_frame.text for sh in slide.shapes if sh.has_text_frame
    )


def test_pptx_provenance_table_paginates_instead_of_truncating():
    many = [{"section": f"sec{i}", "status": "approved", "model": "m", "doctrine_sha": "abc"} for i in range(40)]
    data = render_pptx(_body(), "x", many, "abc", theme="report")
    prs = Presentation(io.BytesIO(data))
    text = "\n".join(_slide_texts(prs))
    assert "sec0" in text and "sec39" in text
    assert export_office._TABLE_NOTE not in text


# ---- validation and dispatch -----------------------------------------------


@pytest.mark.parametrize("fn", [render_docx, render_pptx])
@pytest.mark.parametrize("bad", ["red;}", "#fff", "123456", "#123456\n", ""])
def test_bad_accent_and_theme_rejected(fn, bad):
    with pytest.raises(ValueError, match="accent"):
        fn(_body(), "d", SECTIONS, None, theme="report", accent=bad)
    with pytest.raises(ValueError, match="theme"):
        fn(_body(), "d", SECTIONS, None, theme="neon")


def test_export_formats_tuple():
    assert EXPORT_FORMATS == ("markdown", "html", "pdf", "docx", "pptx")


async def test_dispatch_returns_bytes_and_content_type(monkeypatch):
    async def fake_assemble(db, project_id, slug, include_draft=False):
        return {"markdown": MARKDOWN, "html": _body(), "sections": SECTIONS}

    monkeypatch.setattr(export_module, "assemble_deliverable", fake_assemble)
    pid = uuid.uuid4()
    data, ctype = await render_deliverable_bytes(None, pid, "d", "docx", theme="report")
    assert ctype == DOCX_CONTENT_TYPE and data.startswith(b"PK")
    Document(io.BytesIO(data))
    data, ctype = await render_deliverable_bytes(None, pid, "d", "pptx", theme="report", accent="#123456")
    assert ctype == PPTX_CONTENT_TYPE and data.startswith(b"PK")
    Presentation(io.BytesIO(data))
    with pytest.raises(ValueError, match="docx"):
        await render_deliverable_bytes(None, pid, "d", "rtf")


@pytest.mark.parametrize("fmt", ["docx", "pptx"])
async def test_empty_deliverable_behaves_like_other_formats(monkeypatch, fmt):
    async def empty(db, project_id, slug, include_draft=False):
        return {"markdown": "", "sections": [], "html": ""}

    monkeypatch.setattr(export_module, "assemble_deliverable", empty)
    with pytest.raises(DeliverableEmpty):
        await render_deliverable_bytes(None, uuid.uuid4(), "d", fmt)
    with pytest.raises(ValueError, match="theme"):
        await render_deliverable_bytes(None, None, "d", fmt, theme="neon")


# ---- links: only http(s) with a host and mailto become relationships -----------


def _rels(data: bytes, prefix: str) -> list[str]:
    import zipfile

    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return [
            z.read(n).decode()
            for n in z.namelist()
            if n.startswith(prefix) and n.endswith(".rels")
        ]


LINK_MD = """# T

## S

[ok](https://example.com/a) and [mail](mailto:a@example.com) and [proto](//attacker.example/x:y) and [rel](./a:b) and [unc](\\\\host\\share) and [frag](#top).
"""


@pytest.mark.parametrize("kind", ["docx", "pptx"])
def test_only_web_and_mailto_links_become_relationships(kind):
    body = _body(LINK_MD)
    if kind == "docx":
        data = render_docx(body, "t", SECTIONS, None)
        rels = "\n".join(_rels(data, "word/_rels/document"))
        text = _all_docx_text(Document(io.BytesIO(data)))
    else:
        data = render_pptx(body, "t", SECTIONS, None)
        rels = "\n".join(_rels(data, "ppt/slides/_rels/"))
        text = "\n".join(_slide_texts(Presentation(io.BytesIO(data))))
    assert "https://example.com/a" in rels
    assert "mailto:a@example.com" in rels
    assert "attacker.example" not in rels
    assert "a:b" not in rels and "host" not in rels
    # Not links, but the target stays visible as "text (url)".
    assert "proto (//attacker.example/x:y)" in text
    assert "rel (./a:b)" in text


def test_link_target_rules():
    lt = export_office.link_target
    assert lt("https://example.com/x") and lt("http://example.com") and lt("mailto:a@b.co")
    for bad in ("https:///nohost", "//h/x:y", "./a:b", "\\\\h\\s", "ftp://h/x", "javascript:alert(1)", "", None, "https://a b", "mailto:"):
        assert lt(bad) is None, bad


# ---- nothing approved goes missing ------------------------------------------

NESTED_MD = """# Everything

## Containers

Intro paragraphwordone.

> Outerquote alpha beta.
>
> > Innerquote gamma delta.
> >
> > > Deepquote epsilon.
>
> | Qh1 | Qh2 |
> |---|---|
> | qcellone | qcelltwo |
>
> - quotelistitem one
> - quotelistitem two
>
>         quotecode zeta

- listitem outer
    - listitem inner

      Listparagraph theta.

          listcode iota kappa

- listitem second

| Th1 | Th2 |
|---|---|
| aftertable lambda | aftertable mu |

Closing paragraphword.
"""


def _words(md: str) -> set[str]:
    import re

    text = re.sub(r"[|>`#*\-]", " ", md)
    return {w for w in re.findall(r"[A-Za-z0-9]{3,}", text)}


def test_every_word_of_nested_markdown_reaches_docx_and_pptx():
    docx_text = _all_docx_text(_docx(md=NESTED_MD))
    pptx_text = "\n".join(_slide_texts(_pptx(md=NESTED_MD)))
    for word in _words(NESTED_MD):
        assert word in docx_text, f"docx lost {word!r}"
        assert word in pptx_text, f"pptx lost {word!r}"


def test_docx_nested_quote_is_indented_and_quoted_table_follows():
    doc = _docx(md="# T\n\n## S\n\n> outer\n>\n> > inner\n>\n> | a | b |\n> |---|---|\n> | 1 | 2 |\n")
    paras = {p.text: p for p in doc.paragraphs}
    assert paras["inner"].paragraph_format.left_indent > paras["outer"].paragraph_format.left_indent
    assert [c.text for c in doc.tables[0].rows[1].cells] == ["1", "2"]


# ---- pptx truthfulness -------------------------------------------------------


def test_pptx_provenance_keeps_long_identifiers_whole():
    sha = "f" * 64
    model = "vendor/very-long-model-identifier-" + "x" * 50
    sections = [{"section": "s", "status": "approved", "model": model, "doctrine_sha": sha}]
    data = render_pptx(_body(), "x", sections, sha, theme="report")
    text = "\n".join(_slide_texts(Presentation(io.BytesIO(data))))
    assert sha in text and model in text
    assert "…" not in text


def test_pptx_long_code_block_is_flagged_when_shortened():
    code = "\n".join(f"    line{i} = {i}" for i in range(600))
    prs = _pptx(md=f"# T\n\n## S\n\n{code}\n")
    text = "\n".join(_slide_texts(prs))
    assert export_office._CODE_SHORTENED in text
    short = _pptx(md="# T\n\n## S\n\n    x = 1\n")
    assert export_office._CODE_SHORTENED not in "\n".join(_slide_texts(short))


# ---- docx polish ---------------------------------------------------------------


def test_docx_tables_span_the_text_width_and_right_align_numbers():
    doc = _docx("report")
    table = doc.tables[0]
    widths = [c.width for c in table.columns]
    from docx.shared import Cm

    assert abs(sum(widths) - int(Cm(16.6))) < 1000  # gridCol is stored in twips
    assert widths[0] > widths[1]  # the label column is wider than the numeric ones
    tblw = table._tbl.tblPr.find(qn("w:tblW"))
    assert tblw.get(qn("w:type")) == "pct" and tblw.get(qn("w:w")) == "5000"
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    assert table.rows[1].cells[1].paragraphs[0].alignment == WD_ALIGN_PARAGRAPH.RIGHT
    assert table.rows[1].cells[0].paragraphs[0].alignment is None


def test_docx_plain_theme_does_not_write_the_workspace_name_as_subject():
    assert _docx(None, subtitle="Acme Ltd").core_properties.subject == ""
    assert _docx("report", subtitle="Acme Ltd").core_properties.subject == "Acme Ltd"
