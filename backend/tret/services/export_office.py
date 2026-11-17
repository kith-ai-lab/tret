"""Word (.docx) and PowerPoint (.pptx) rendering for the deliverable exporter.

The assembled deliverable is markdown. It is converted to sanitised HTML by
`render_markdown` (raw HTML is escaped there), and that HTML is walked with the
standard-library `html.parser` into a small neutral block model (headings,
paragraphs, list items, tables, quotes, code, rules). Two renderers then turn
the model into python-docx / python-pptx documents. Nothing is ever written as
markup: all text goes through the libraries' APIs, after control characters
that XML 1.0 forbids are stripped, so a hostile section can at worst appear as
literal text.

The provenance appendix is not re-implemented here: `export._provenance_block`
already produces it as HTML (statuses, "approved (automatic)", the conditional
automatic-approval sentence, energy estimate), so it is parsed with the same
block model and rendered alongside the body in every format.
"""

from __future__ import annotations

import io
import math
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit

from tret.services.export_themes import (
    DEFAULT_ACCENT,
    RendererUnavailable,
    _tint,
    validate_accent,
    validate_theme,
)
from tret.services.html_sanitize import is_safe_url

DOCX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
PPTX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

# ---------------------------------------------------------------------------
# Neutral block model
# ---------------------------------------------------------------------------

# Characters XML 1.0 cannot carry (control chars, lone surrogates, U+FFFE/FFFF).
_XML_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-퟿-�\U00010000-\U0010ffff]")


def clean_text(text: str) -> str:
    """Strip characters that are illegal in XML 1.0."""
    return _XML_ILLEGAL.sub("", text)


@dataclass
class Span:
    text: str
    bold: bool = False
    italic: bool = False
    code: bool = False
    url: str | None = None


@dataclass
class Block:
    kind: str  # heading | para | li | table | quote | code | rule
    runs: list[Span] = field(default_factory=list)
    level: int = 0  # heading level (1-6) or list nesting depth (0-based)
    ordered: bool = False
    list_id: int = 0
    rows: list[list[list[Span]]] = field(default_factory=list)
    header: bool = False
    blocks: list[Block] = field(default_factory=list)
    text: str = ""  # code blocks


_LINK_SCHEMES = ("http", "https", "mailto")


def link_target(url: str | None) -> str | None:
    """The URL if it may become a real hyperlink, else None.

    Only absolute http(s) URLs with a host and mailto: addresses qualify.
    Scheme-relative (`//host/x`), relative, UNC-style and every other scheme
    stay plain text, so an Office document never carries a relationship a
    model chose beyond those three.
    """
    if not url or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        return url if parts.netloc else None
    if scheme == "mailto":
        return url if parts.path else None
    return None


def spans_text(runs: list[Span]) -> str:
    return "".join(r.text for r in runs)


_BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "ul", "ol", "blockquote", "pre", "table"}


class _Parser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root: list[Block] = []
        self.out = self.root
        self._out_stack: list[list[Block]] = []
        self.cur: list[Span] | None = None
        self.kind: str | None = None
        self.meta: dict = {}
        self.bold = self.italic = self.code = 0
        self.urls: list[str | None] = []
        self.anchor_text: list[list[str]] = []
        self.lists: list[bool] = []
        self.list_counter = 0
        self.list_ids: list[int] = []
        self.pre: list[str] | None = None
        self.table: list[list[list[Span]]] | None = None
        self.table_header = False
        self.row: list[list[Span]] | None = None
        self.skip = 0

    # -- block plumbing ----------------------------------------------------
    def _begin(self, kind: str, **meta) -> None:
        self._flush()
        self.kind = kind
        self.meta = meta
        self.cur = []

    def _flush(self) -> None:
        kind, cur = self.kind, self.cur
        self.kind, self.cur = None, None
        if cur is None or kind is None:
            return
        runs = [r for r in cur if r.text]
        if runs:
            runs[0] = replace(runs[0], text=runs[0].text.lstrip())
            runs[-1] = replace(runs[-1], text=runs[-1].text.rstrip())
            runs = [r for r in runs if r.text]
        if kind == "cell":
            if self.row is not None:
                self.row.append(runs)
            return
        if not runs:
            return
        if kind == "heading":
            self.out.append(Block("heading", runs, level=self.meta["level"]))
        elif kind == "li":
            self.out.append(
                Block(
                    "li",
                    runs,
                    level=self.meta["level"],
                    ordered=self.meta["ordered"],
                    list_id=self.meta["list_id"],
                )
            )
        else:
            self.out.append(Block("para", runs))

    # -- html.parser callbacks ---------------------------------------------
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("script", "style"):
            self.skip += 1
            return
        if self.pre is not None:
            return
        if self.table is not None and tag in _BLOCK_TAGS - {"table"}:
            self._space()
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._begin("heading", level=int(tag[1]))
        elif tag == "p":
            if self.kind == "li":
                self._space()
            else:
                self._begin("p")
        elif tag in ("ul", "ol"):
            self._flush()
            if not self.lists:
                self.list_counter += 1
            self.lists.append(tag == "ol")
            self.list_ids.append(self.list_counter)
        elif tag == "li":
            ordered = self.lists[-1] if self.lists else False
            self._begin(
                "li",
                level=max(len(self.lists) - 1, 0),
                ordered=ordered,
                list_id=self.list_ids[0] if self.list_ids else 0,
            )
        elif tag == "blockquote":
            self._flush()
            self._out_stack.append(self.out)
            self.out = []
        elif tag == "pre":
            self._flush()
            self.pre = []
        elif tag == "hr":
            self._flush()
            self.out.append(Block("rule"))
        elif tag == "br":
            self._space()
        elif tag == "table":
            self._flush()
            self.table = []
            self.table_header = False
        elif tag == "tr":
            self._flush()
            self.row = []
        elif tag in ("td", "th"):
            self._flush()
            if tag == "th" and not self.table:
                self.table_header = True
            self.kind = "cell"
            self.meta = {}
            self.cur = []
        elif tag in ("strong", "b"):
            self.bold += 1
        elif tag in ("em", "i"):
            self.italic += 1
        elif tag == "code":
            self.code += 1
        elif tag == "a":
            self.urls.append(a.get("href"))
            self.anchor_text.append([])
        elif tag == "img":
            alt = a.get("alt")
            if alt:
                self.handle_data(alt)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(self.skip - 1, 0)
            return
        if tag == "pre":
            if self.pre is not None:
                text = "".join(self.pre).rstrip("\n")
                self.pre = None
                if text.strip():
                    self.out.append(Block("code", text=clean_text(text)))
            return
        if self.pre is not None:
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush()
        elif tag == "p":
            if self.kind == "p":
                self._flush()
        elif tag == "li":
            self._flush()
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
                self.list_ids.pop()
        elif tag == "blockquote":
            self._flush()
            if self._out_stack:
                inner = self.out
                self.out = self._out_stack.pop()
                if inner:
                    self.out.append(Block("quote", blocks=inner))
        elif tag in ("td", "th"):
            self._flush()
        elif tag == "tr":
            self._flush()
            if self.row is not None and self.table is not None:
                self.table.append(self.row)
            self.row = None
        elif tag == "table":
            self._flush()
            if self.table:
                self.out.append(Block("table", rows=self.table, header=self.table_header))
            self.table = None
        elif tag in ("strong", "b"):
            self.bold = max(self.bold - 1, 0)
        elif tag in ("em", "i"):
            self.italic = max(self.italic - 1, 0)
        elif tag == "code":
            self.code = max(self.code - 1, 0)
        elif tag == "a":
            if self.urls:
                href = self.urls.pop()
                shown = "".join(self.anchor_text.pop()) if self.anchor_text else ""
                # A link that cannot be a real hyperlink keeps its target
                # visible: "text (url)".
                if href and link_target(href) is None and self.cur is not None:
                    if shown.strip() != href.strip():
                        self.cur.append(Span(clean_text(f" ({href})")))

    def _space(self) -> None:
        if self.cur:
            self.cur.append(Span(" "))

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre is not None:
            self.pre.append(data)
            return
        if self.cur is None:
            if not data.strip():
                return
            self._begin("p")
        text = clean_text(re.sub(r"\s+", " ", data))
        if not text:
            return
        if not self.cur and text.startswith(" "):
            text = text[1:]
        if text:
            url = self.urls[-1] if self.urls else None
            if self.anchor_text:
                self.anchor_text[-1].append(text)
            self.cur.append(
                Span(
                    text,
                    bold=bool(self.bold),
                    italic=bool(self.italic),
                    code=bool(self.code),
                    url=url if url and is_safe_url(url) and link_target(url) else None,
                )
            )


def parse_html(html_text: str) -> list[Block]:
    """Sanitised HTML to the block model. Unknown tags degrade to their text."""
    parser = _Parser()
    parser.feed(html_text or "")
    parser.close()
    parser._flush()
    return parser.root


def _split_title(blocks: list[Block], fallback: str) -> tuple[str, list[Block], bool]:
    """(title, blocks, had_leading_h1). The leading h1 is the deliverable title."""
    if blocks and blocks[0].kind == "heading" and blocks[0].level == 1:
        return spans_text(blocks[0].runs), blocks[1:], True
    return fallback.replace("-", " ").replace("_", " ").title(), blocks, False


def _provenance_blocks(sections: list[dict], doctrine_sha: str | None) -> list[Block]:
    # Reuse the HTML exporter's appendix verbatim so the wording lives in one place.
    from tret.services.export import _provenance_block

    return parse_html(_provenance_block(sections, doctrine_sha))


@dataclass(frozen=True)
class _Look:
    report: bool
    accent: str  # RRGGBB, no '#'
    ink: str
    soft: str
    faint: str
    tint: str  # callout / banding fill
    rule: str

    @classmethod
    def build(cls, theme: str, accent: str | None) -> _Look:
        report = theme == "report"
        colour = (accent or DEFAULT_ACCENT) if report else "#000000"
        return cls(
            report=report,
            accent=colour[1:],
            ink="1B1F23" if report else "000000",
            soft="4A525A" if report else "333333",
            faint="7B848C" if report else "666666",
            tint=_tint(colour, 0.92)[1:] if report else "F3F5F6",
            rule="D5D9DD" if report else "BBBBBB",
        )


def _now_date(now: datetime | None) -> str:
    stamp = now or datetime.now(timezone.utc)
    return f"{stamp.day} {stamp:%B %Y}"


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------

_PPR_AFTER_SHD = (
    "w:tabs", "w:suppressAutoHyphens", "w:kinsoku", "w:wordWrap", "w:overflowPunct",
    "w:topLinePunct", "w:autoSpaceDE", "w:autoSpaceDN", "w:bidi", "w:adjustRightInd",
    "w:snapToGrid", "w:spacing", "w:ind", "w:contextualSpacing", "w:mirrorIndents",
    "w:suppressOverlap", "w:jc", "w:textDirection", "w:textAlignment",
    "w:textboxTightWrap", "w:outlineLvl", "w:divId", "w:cnfStyle", "w:rPr",
    "w:sectPr", "w:pPrChange",
)  # fmt: skip
_BODY_FONT = "Calibri"
_MONO_FONT = "Consolas"


def render_docx(
    html_body: str,
    title: str,
    sections: list[dict],
    doctrine_sha: str | None,
    *,
    theme: str | None = None,
    accent: str | None = None,
    subtitle: str | None = None,
    now: datetime | None = None,
) -> bytes:
    """The Word export. `report` adds a cover page, accent colour and a running
    header; `plain` is the same structure in black with page numbers only."""
    theme = validate_theme(theme)
    accent = validate_accent(accent)
    try:
        import docx  # noqa: F401
    except ImportError as e:  # pragma: no cover - declared base dependency
        raise RendererUnavailable("docx export needs the python-docx package, which is not installed") from e
    look = _Look.build(theme, accent)
    blocks = parse_html(html_body)
    doc_title, body, _ = _split_title(blocks, title)
    if not look.report:
        body = blocks  # plain: the title stays in the body as a Heading 1
    prov = _provenance_blocks(sections, doctrine_sha)

    doc = _docx_new(look, doc_title, subtitle, now)
    if look.report:
        _docx_cover(doc, look, doc_title, subtitle, _now_date(now))
    _docx_blocks(doc, body, look)
    _docx_blocks(doc, prov, look, small=True, page_break_first=True)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def _docx_new(look: _Look, doc_title: str, subtitle: str | None, now: datetime | None):
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml.ns import qn
    from docx.shared import Cm, Pt, RGBColor

    doc = Document()
    props = doc.core_properties
    props.title = clean_text(doc_title)
    props.subject = clean_text(subtitle or "") if look.report else ""
    props.author = "Tret"
    props.last_modified_by = "Tret"
    props.comments = ""
    props.created = props.modified = now or datetime.now(timezone.utc)

    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.2)
    sec.top_margin, sec.bottom_margin = Cm(2.5), Cm(2.3)
    sec.header_distance = sec.footer_distance = Cm(1.1)

    def fonts(style, name, size, colour, bold=None, italic=None):
        font = style.font
        font.name = name
        font.size = Pt(size)
        font.color.rgb = RGBColor.from_string(colour)
        rpr = style.element.get_or_add_rPr()
        color = rpr.find(qn("w:color"))
        if color is not None:
            for attr in ("w:themeColor", "w:themeShade", "w:themeTint"):
                color.attrib.pop(qn(attr), None)
        rfonts = rpr.rFonts
        for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
            rfonts.attrib.pop(qn(attr), None)
        rfonts.set(qn("w:eastAsia"), name)
        rfonts.set(qn("w:cs"), name)
        if bold is not None:
            font.bold = bold
        if italic is not None:
            font.italic = italic

    styles = doc.styles
    fonts(styles["Normal"], _BODY_FONT, 10.5, look.ink)
    normal = styles["Normal"].paragraph_format
    normal.space_after, normal.line_spacing = Pt(6), 1.15
    for name, size, colour, before, after in (
        ("Heading 1", 20, look.accent, 18, 8),
        ("Heading 2", 15, look.accent, 16, 6),
        ("Heading 3", 12, look.ink, 12, 4),
    ):
        fonts(styles[name], _BODY_FONT, size, colour, bold=True)
        pf = styles[name].paragraph_format
        pf.space_before, pf.space_after, pf.keep_with_next = Pt(before), Pt(after), True
    fonts(styles["Title"], _BODY_FONT, 34, look.ink, bold=True)
    title_ppr = styles["Title"].element.pPr
    if title_ppr is not None:
        for border in title_ppr.findall(qn("w:pBdr")):
            title_ppr.remove(border)
    styles["Title"].paragraph_format.space_after = Pt(6)
    fonts(styles["Subtitle"], _BODY_FONT, 14, look.soft, bold=False, italic=False)
    for name in ("List Bullet", "List Bullet 2", "List Bullet 3",
                 "List Number", "List Number 2", "List Number 3"):  # fmt: skip
        fonts(styles[name], _BODY_FONT, 10.5, look.ink)
        styles[name].paragraph_format.space_after = Pt(3)

    # Page numbers on every page but the cover; a running title header only in
    # the report theme (plain has no cover and no header).
    footer_par = sec.footer.paragraphs[0]
    footer_par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    for text, instr in (("Page ", "PAGE"), (" of ", "NUMPAGES")):
        _docx_run(footer_par, text, look.faint, 8)
        _docx_field(footer_par, instr, look.faint, 8)
    if look.report:
        sec.different_first_page_header_footer = True
        head = sec.header.paragraphs[0]
        run = _docx_run(head, clean_text(doc_title).upper(), look.faint, 7.5)
        run.font.bold = False
        _docx_border(head, "bottom", look.rule, 4, space=4)
    return doc


def _docx_run(par, text: str, colour: str, size: float):
    from docx.shared import Pt, RGBColor

    run = par.add_run(text)
    run.font.size = Pt(size)
    run.font.color.rgb = RGBColor.from_string(colour)
    return run


def _docx_field(par, instr: str, colour: str, size: float) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    def piece(kind: str):
        run = _docx_run(par, "", colour, size)
        el = OxmlElement("w:fldChar")
        el.set(qn("w:fldCharType"), kind)
        run._r.append(el)
        return run

    piece("begin")
    run = _docx_run(par, "", colour, size)
    instr_el = OxmlElement("w:instrText")
    instr_el.set(qn("xml:space"), "preserve")
    instr_el.text = f" {instr} "
    run._r.append(instr_el)
    piece("separate")
    _docx_run(par, "1", colour, size)
    piece("end")


def _docx_border(par, side: str, colour: str, size_eighths: int, space: int = 4) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    ppr = par._p.get_or_add_pPr()
    bdr = ppr.find(qn("w:pBdr"))
    if bdr is None:
        bdr = OxmlElement("w:pBdr")
        ppr.insert_element_before(bdr, "w:shd", *_PPR_AFTER_SHD)
    el = OxmlElement(f"w:{side}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), str(size_eighths))
    el.set(qn("w:space"), str(space))
    el.set(qn("w:color"), colour)
    bdr.append(el)


def _docx_shade(par, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    par._p.get_or_add_pPr().insert_element_before(shd, *_PPR_AFTER_SHD)


def _docx_cover(doc, look: _Look, doc_title: str, subtitle: str | None, date: str) -> None:
    from docx.enum.text import WD_BREAK
    from docx.shared import Cm, Pt

    eyebrow = doc.add_paragraph()
    eyebrow.paragraph_format.space_before = Pt(150)
    run = _docx_run(eyebrow, "REPORT", look.accent, 9)
    run.font.bold = True
    doc.add_paragraph(clean_text(doc_title), style="Title")
    rule = doc.add_paragraph()
    rule.paragraph_format.right_indent = Cm(12.2)
    rule.paragraph_format.space_before = Pt(6)
    rule.paragraph_format.space_after = Pt(12)
    _docx_border(rule, "bottom", look.accent, 24, space=1)
    if subtitle:
        doc.add_paragraph(clean_text(subtitle), style="Subtitle")
    last = doc.add_paragraph()
    _docx_run(last, date, look.faint, 10)
    last.add_run().add_break(WD_BREAK.PAGE)


def _docx_spans(par, spans: list[Span], look: _Look, size: float | None = None, bold: bool = False):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.shared import Pt, RGBColor
    from docx.text.run import Run as DocxRun

    for sp in spans:
        text = clean_text(sp.text)
        if not text:
            continue
        target = link_target(sp.url)
        if target:
            rid = par.part.relate_to(target, RT.HYPERLINK, is_external=True)
            link = OxmlElement("w:hyperlink")
            link.set(qn("r:id"), rid)
            link.set(qn("w:history"), "1")
            r_el = OxmlElement("w:r")
            link.append(r_el)
            par._p.append(link)
            run = DocxRun(r_el, par)
            run.text = text
            run.font.underline = True
            run.font.color.rgb = RGBColor.from_string(look.accent if look.report else "0563C1")
        else:
            run = par.add_run(text)
        if sp.bold or bold:
            run.font.bold = True
        if sp.italic:
            run.font.italic = True
        if sp.code:
            run.font.name = _MONO_FONT
            r_fonts = run._r.get_or_add_rPr().rFonts
            r_fonts.set(qn("w:eastAsia"), _MONO_FONT)
            run.font.size = Pt((size or 10.5) - 1.5)
        elif size:
            run.font.size = Pt(size)


def _docx_blocks(doc, blocks: list[Block], look: _Look, *, small: bool = False,
                 page_break_first: bool = False) -> None:  # fmt: skip
    from docx.shared import Cm, Pt, RGBColor

    num_ids: dict[int, int] = {}
    size = 8.5 if small else None
    first = True
    for i, blk in enumerate(blocks):
        if blk.kind == "heading":
            par = doc.add_paragraph(style=f"Heading {min(blk.level, 3)}")
            _docx_spans(par, blk.runs, look)
            if page_break_first and first:
                par.paragraph_format.page_break_before = True
        elif blk.kind == "para":
            par = doc.add_paragraph()
            _docx_spans(par, blk.runs, look, size=size)
            if small:
                for run in par.runs:
                    run.font.color.rgb = RGBColor.from_string(look.soft)
            if page_break_first and first:
                par.paragraph_format.page_break_before = True
        elif blk.kind == "li":
            _docx_list_item(doc, blk, look, num_ids)
        elif blk.kind == "table":
            _docx_table(doc, blk, look, small)
        elif blk.kind == "quote":
            _docx_quote(doc, blk, look, small)
        elif blk.kind == "code":
            par = doc.add_paragraph()
            lines = blk.text.split("\n")
            for n, line in enumerate(lines):
                run = par.add_run(clean_text(line))
                run.font.name = _MONO_FONT
                run.font.size = Pt(9)
                if n < len(lines) - 1:
                    run.add_break()
            par.paragraph_format.left_indent = Cm(0.2)
            par.paragraph_format.right_indent = Cm(0.2)
            _docx_shade(par, "F3F5F6")
            _docx_border(par, "left", look.rule, 12, space=6)
        elif blk.kind == "rule":
            nxt = blocks[i + 1] if i + 1 < len(blocks) else None
            if nxt is not None and nxt.kind != "heading":
                par = doc.add_paragraph()
                par.paragraph_format.space_before = Pt(6)
                _docx_border(par, "bottom", look.rule, 4, space=1)
        first = False


def _docx_new_num(doc, style_name: str) -> int | None:
    try:
        numbering = doc.part.numbering_part.numbering_definitions._numbering
        style = doc.styles[style_name]
        num_id = style.element.pPr.numPr.numId.val
        abstract = numbering.num_having_numId(num_id).abstractNumId.val
        new = numbering.add_num(abstract)
        new.add_lvlOverride(ilvl=0).add_startOverride(1)
        return int(new.numId)
    except Exception:  # restart is cosmetic; numbering still works without it
        return None


def _docx_list_item(doc, blk: Block, look: _Look, num_ids: dict[int, int]) -> None:
    level = min(blk.level, 2)
    base = "List Number" if blk.ordered else "List Bullet"
    par = doc.add_paragraph(style=base if level == 0 else f"{base} {level + 1}")
    if blk.ordered and level == 0:
        if blk.list_id not in num_ids:
            new_id = _docx_new_num(doc, "List Number")
            if new_id is not None:
                num_ids[blk.list_id] = new_id
        if blk.list_id in num_ids:
            num_pr = par._p.get_or_add_pPr().get_or_add_numPr()
            num_pr.get_or_add_ilvl().val = 0
            num_pr.get_or_add_numId().val = num_ids[blk.list_id]
    _docx_spans(par, blk.runs, look)


def _docx_quote(doc, blk: Block, look: _Look, small: bool = False, depth: int = 0) -> None:
    """A quote as shaded callout paragraphs. Nested quotes are indented one more
    step; a table inside a quote follows the callout as an ordinary table."""
    from docx.shared import Cm, Pt

    for inner in blk.blocks:
        if inner.kind == "quote":
            _docx_quote(doc, inner, look, small, depth + 1)
            continue
        if inner.kind == "table":
            _docx_table(doc, inner, look, small)
            continue
        if inner.kind in ("para", "heading", "li"):
            runs = inner.runs
            if inner.kind == "li":
                runs = [Span("•  ")] + runs
            if inner.kind == "heading":
                runs = [replace(r, bold=True) for r in runs]
        elif inner.kind == "code":
            runs = [Span(inner.text, code=True)]
        else:
            continue
        par = doc.add_paragraph()
        _docx_spans(par, runs, look)
        pf = par.paragraph_format
        extra = Cm(0.6 * depth) + (Cm(0.5 * inner.level) if inner.kind == "li" else 0)
        pf.left_indent, pf.right_indent = Cm(0.5) + extra, Cm(0.3)
        pf.space_before, pf.space_after = Pt(3), Pt(3)
        _docx_shade(par, look.tint)
        _docx_border(par, "left", look.accent, 24, space=8)


def _docx_cell_shade(cell, fill: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    tc_pr = cell._tc.get_or_add_tcPr()
    for old in tc_pr.findall(qn("w:shd")):
        tc_pr.remove(old)
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tc_pr.insert_element_before(shd, "w:noWrap", "w:tcMar", "w:textDirection",
                                "w:tcFitText", "w:vAlign", "w:hideMark")  # fmt: skip


_NUMERIC_CELL = re.compile(r"[~≈<>±+\-−–]?\s?[$€£]?\s?\d[\d,]*(\.\d+)?\s?(%|[kKmMbB]|x|×)?")
_TEXT_WIDTH_CM = 16.6  # A4 less the 2.2 cm side margins set in `_docx_new`


def _docx_column_plan(blk: Block, ncols: int):
    """(column widths in EMU summing to the text width, numeric-column flags).

    Widths follow the longest cell in each column (clamped), with numeric-looking
    columns kept narrow; the total always equals the full text width."""
    from docx.shared import Cm

    numeric: list[bool] = []
    weights: list[float] = []
    for c in range(ncols):
        cells = [spans_text(r[c]).strip() if c < len(r) else "" for r in blk.rows]
        body = cells[1:] if blk.header else cells
        filled = [t for t in body if t and t not in ("—", "-", "–")]
        is_num = bool(filled) and all(_NUMERIC_CELL.fullmatch(t) for t in filled)
        numeric.append(is_num)
        longest = max((len(t) for t in cells), default=0)
        weights.append(float(min(max(longest, 6), 12 if is_num else 48)))
    total = sum(weights) or 1.0
    full = int(Cm(_TEXT_WIDTH_CM))
    widths = [int(full * w / total) for w in weights]
    widths[-1] += full - sum(widths)
    return widths, numeric


def _docx_table(doc, blk: Block, look: _Look, small: bool) -> None:
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor

    rows = blk.rows
    ncols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.autofit = False  # fixed layout: the column widths below are honoured
    size = 8 if small else 9.5
    widths, numeric = _docx_column_plan(blk, ncols)
    for c_idx, w in enumerate(widths):
        table.columns[c_idx].width = w

    tbl_pr = table._tbl.tblPr
    width = tbl_pr.find(qn("w:tblW"))
    if width is None:
        width = OxmlElement("w:tblW")
        tbl_pr.append(width)
    width.set(qn("w:type"), "pct")
    width.set(qn("w:w"), "5000")
    borders = OxmlElement("w:tblBorders")
    for side in ("top", "bottom", "insideH"):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), look.rule)
        borders.append(el)
    tbl_pr.insert_element_before(borders, "w:shd", "w:tblLayout", "w:tblCellMar", "w:tblLook")
    margins = OxmlElement("w:tblCellMar")
    for side, val in (("top", 50), ("left", 100), ("bottom", 50), ("right", 100)):
        el = OxmlElement(f"w:{side}")
        el.set(qn("w:w"), str(val))
        el.set(qn("w:type"), "dxa")
        margins.append(el)
    tbl_pr.insert_element_before(margins, "w:tblLook")

    for r_idx, row in enumerate(rows):
        is_head = blk.header and r_idx == 0
        tr_pr = table.rows[r_idx]._tr.get_or_add_trPr()
        tr_pr.append(OxmlElement("w:cantSplit"))
        if is_head:
            tr_pr.append(OxmlElement("w:tblHeader"))
        for c_idx in range(ncols):
            cell = table.cell(r_idx, c_idx)
            cell.width = widths[c_idx]
            par = cell.paragraphs[0]
            par.paragraph_format.space_after = Pt(0)
            par.paragraph_format.line_spacing = 1.0
            if numeric[c_idx]:
                par.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            spans = row[c_idx] if c_idx < len(row) else []
            if is_head:
                _docx_spans(par, spans, look, size=size, bold=True)
                _docx_cell_shade(cell, look.accent if look.report else "F0F0F0")
                if look.report:
                    for run in par.runs:
                        run.font.color.rgb = RGBColor(255, 255, 255)
            else:
                _docx_spans(par, spans, look, size=size)
                if look.report and r_idx % 2 == 0:
                    _docx_cell_shade(cell, look.tint)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------

MAX_BULLETS_PER_SLIDE = 8
MAX_CHARS_PER_SLIDE = 1000
MAX_CHARS_PER_BULLET = 400
MAX_TABLE_COLS = 6
MAX_CELL_CHARS = 60
MIN_FONT_PT = 14
_BODY_BUDGET_PT = 330.0
_SLIDE_W_IN, _SLIDE_H_IN = 13.333, 7.5
_MARGIN_IN = 0.6
_BODY_W_IN = _SLIDE_W_IN - 2 * _MARGIN_IN
_BODY_TOP_IN = 1.55
_TABLE_NOTE = "Table shortened to fit the slide. Export as Word or PDF for the full table."
_CODE_SHORTENED = "… (shortened; see Word or PDF export)"
MAX_CODE_CHARS = 3000


@dataclass
class _Slide:
    title: str
    items: list[tuple] = field(default_factory=list)  # ("bullet", runs, level, ordered) | ("box", kind, runs) | ("table", rows, header, note)
    cost: float = 0.0
    chars: int = 0
    bullets: int = 0


def _bullet_font(level: int) -> int:
    return max(MIN_FONT_PT, 18 - 2 * level)


def _cpl(width_in: float, pt: float) -> int:
    return max(int(width_in * 72 / (pt * 0.55)), 8)


def _bullet_cost(chars: int, level: int) -> float:
    pt = _bullet_font(level)
    lines = max(math.ceil(chars / _cpl(_BODY_W_IN - 0.5 - 0.4 * level, pt)), 1)
    return lines * pt * 1.2 + 6


def _box_cost(chars: int, pt: int = 16) -> float:
    lines = max(math.ceil(chars / _cpl(_BODY_W_IN - 0.8, pt)), 1)
    return lines * pt * 1.2 + 26


def _chunk_spans(spans: list[Span], limit: int) -> list[list[Span]]:
    """Split spans at word boundaries into pieces of at most `limit` chars,
    keeping formatting; a single over-long word is hard-split."""
    chunks: list[list[Span]] = []
    cur: list[Span] = []
    n = 0
    for sp in spans:
        for tok in re.findall(r"\S+\s*|\s+", sp.text):
            while len(tok) > limit:
                head, tok = tok[:limit], tok[limit:]
                if cur:
                    chunks.append(cur)
                    cur, n = [], 0
                chunks.append([replace(sp, text=head)])
            if cur and n + len(tok) > limit:
                chunks.append(cur)
                cur, n = [], 0
            cur.append(replace(sp, text=tok))
            n += len(tok)
    if cur:
        chunks.append(cur)
    out = []
    for chunk in chunks:
        chunk[0] = replace(chunk[0], text=chunk[0].text.lstrip())
        chunk[-1] = replace(chunk[-1], text=chunk[-1].text.rstrip())
        chunk = [s for s in chunk if s.text]
        if chunk:
            out.append(chunk)
    return out


def _trunc(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


class _Deck:
    """Flow document blocks into slide specs under capacity limits."""

    def __init__(self) -> None:
        self.slides: list[_Slide] = []
        self.heading = ""
        self.cur: _Slide | None = None
        self.used: set[str] = set()

    def start(self, title: str) -> None:
        self.flush()
        self.heading = _trunc(title, 90)
        self.cur = None

    def _slide(self) -> _Slide:
        if self.cur is None:
            title = self.heading
            if title in self.used:
                title = f"{title} (cont.)"
            self.used.add(self.heading)
            self.cur = _Slide(title)
        return self.cur

    def flush(self) -> None:
        if self.cur is not None and self.cur.items:
            self.slides.append(self.cur)
        self.cur = None

    def _room(self, cost: float, chars: int, bullet: bool) -> bool:
        s = self._slide()
        if not s.items:
            return True
        if bullet and s.bullets >= MAX_BULLETS_PER_SLIDE:
            return False
        return s.cost + cost <= _BODY_BUDGET_PT and s.chars + chars <= MAX_CHARS_PER_SLIDE

    def bullet(self, spans: list[Span], level: int, ordered: bool) -> None:
        for chunk in _chunk_spans(spans, MAX_CHARS_PER_BULLET):
            chars = len(spans_text(chunk))
            cost = _bullet_cost(chars, level)
            if not self._room(cost, chars, True):
                self.flush()
            s = self._slide()
            s.items.append(("bullet", chunk, level, ordered))
            s.cost += cost
            s.chars += chars
            s.bullets += 1

    def box(self, kind: str, spans: list[Span]) -> None:
        for chunk in _chunk_spans(spans, 500):
            chars = len(spans_text(chunk))
            cost = _box_cost(chars)
            if not self._room(cost, chars, False):
                self.flush()
            s = self._slide()
            s.items.append(("box", kind, chunk))
            s.cost += cost
            s.chars += chars
            s.bullets += 1

    def table(self, blk: Block, paginate: bool) -> None:
        self.flush()
        # A paginating table (the provenance appendix) never shortens a cell:
        # identifiers such as a doctrine SHA or a model id must stay whole, so
        # long values wrap and the table runs onto more slides instead.
        cell_limit = None if paginate else MAX_CELL_CHARS
        rows = [
            [
                [Span(spans_text(c) if cell_limit is None else _trunc(spans_text(c), cell_limit))]
                for c in r[:MAX_TABLE_COLS]
            ]
            for r in blk.rows
        ]
        cut_cols = any(len(r) > MAX_TABLE_COLS for r in blk.rows)
        cut_cells = cell_limit is not None and any(
            len(spans_text(c)) > cell_limit for r in blk.rows for c in r[:MAX_TABLE_COLS]
        )
        header = rows[0] if blk.header else None
        body = rows[1:] if blk.header else rows
        pages: list[list[list[list[Span]]]] = []
        page: list[list[list[Span]]] = []
        used = _table_row_cost(header) if header else 0.0
        truncated = False
        for row in body:
            cost = _table_row_cost(row)
            if page and used + cost > _BODY_BUDGET_PT - 10:
                if not paginate:
                    truncated = True
                    break
                pages.append(page)
                page, used = [], _table_row_cost(header) if header else 0.0
            page.append(row)
            used += cost
        if page or not pages:
            pages.append(page)
        for n, rows_on_page in enumerate(pages):
            title = self.heading
            if title in self.used or n:
                title = f"{title} (cont.)"
            self.used.add(self.heading)
            last = n == len(pages) - 1
            note = (truncated or cut_cols or cut_cells) and last
            self.slides.append(
                _Slide(title, [("table", ([header] if header else []) + rows_on_page, bool(header), bool(note))])
            )


def _table_row_cost(row: list[list[Span]] | None) -> float:
    if not row:
        return 0.0
    col_w = _BODY_W_IN / max(len(row), 1)
    cpl = _cpl(col_w - 0.2, MIN_FONT_PT)
    lines = max(math.ceil(len(spans_text(c)) / cpl) for c in row) if row else 1
    return max(lines, 1) * MIN_FONT_PT * 1.2 + 14


def _plan_quote(deck: _Deck, blk: Block, paginate_tables: bool) -> None:
    """A quote as one callout box. Nested quotes are folded into the same box in
    reading order; a table inside a quote closes the box and gets its own slide."""
    runs: list[Span] = []

    def emit() -> None:
        if runs:
            deck.box("quote", list(runs))
            runs.clear()

    def add(more: list[Span]) -> None:
        runs.extend(([Span(" ")] if runs else []) + more)

    def walk(quote: Block) -> None:
        for inner in quote.blocks:
            if inner.kind == "heading":
                add([replace(r, bold=True) for r in inner.runs])
            elif inner.kind == "para":
                add(inner.runs)
            elif inner.kind == "li":
                add([Span("• ")] + inner.runs)
            elif inner.kind == "code":
                add([Span(inner.text, code=True)])
            elif inner.kind == "quote":
                walk(inner)
            elif inner.kind == "table":
                emit()
                deck.table(inner, paginate_tables)

    walk(blk)
    emit()


def _plan_slides(blocks: list[Block], overview_title: str, *, paginate_tables: bool = False,
                 deck: _Deck | None = None) -> _Deck:  # fmt: skip
    deck = deck or _Deck()
    if deck.heading == "":
        deck.heading = overview_title
    pending_rule = False
    for blk in blocks:
        if blk.kind == "rule":
            pending_rule = True
            continue
        if blk.kind == "heading" and blk.level <= 2:
            deck.start(spans_text(blk.runs))
            pending_rule = False
            continue
        if pending_rule:
            deck.start("Notes")
            pending_rule = False
        if blk.kind == "heading":
            deck.bullet([replace(r, bold=True) for r in blk.runs], 0, False)
        elif blk.kind == "para":
            deck.bullet(blk.runs, 0, False)
        elif blk.kind == "li":
            deck.bullet(blk.runs, min(blk.level, 2), blk.ordered)
        elif blk.kind == "quote":
            _plan_quote(deck, blk, paginate_tables)
        elif blk.kind == "code":
            text = re.sub(r"\s+", " ", blk.text)
            shortened = len(text) > MAX_CODE_CHARS
            deck.box("code", [Span(text[:MAX_CODE_CHARS].rstrip(), code=True)])
            if shortened:
                deck.bullet([Span(_CODE_SHORTENED, italic=True)], 0, False)
        elif blk.kind == "table":
            deck.table(blk, paginate_tables)
    deck.flush()
    return deck


def render_pptx(
    html_body: str,
    title: str,
    sections: list[dict],
    doctrine_sha: str | None,
    *,
    theme: str | None = None,
    accent: str | None = None,
    subtitle: str | None = None,
    now: datetime | None = None,
) -> bytes:
    """The PowerPoint export (16:9). Each level-1/2 heading starts a slide;
    long sections continue on "(cont.)" slides, tables get their own slide."""
    theme = validate_theme(theme)
    accent = validate_accent(accent)
    try:
        import pptx  # noqa: F401
    except ImportError as e:  # pragma: no cover - declared base dependency
        raise RendererUnavailable("pptx export needs the python-pptx package, which is not installed") from e
    look = _Look.build(theme, accent)
    blocks = parse_html(html_body)
    doc_title, body, _ = _split_title(blocks, title)
    prov = _provenance_blocks(sections, doctrine_sha)

    deck = _plan_slides(body, "Overview")
    prov_deck = _Deck()
    prov_deck.heading = "Provenance"
    # Provenance paragraphs/table: the heading block itself is "Provenance".
    _plan_slides([b for b in prov if not (b.kind == "heading")], "Provenance", paginate_tables=True, deck=prov_deck)
    slides = deck.slides + prov_deck.slides
    return _pptx_build(slides, look, doc_title, subtitle, _now_date(now))


def _pptx_build(slides: list[_Slide], look: _Look, doc_title: str, subtitle: str | None, date: str) -> bytes:
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width, prs.slide_height = Inches(_SLIDE_W_IN), Inches(_SLIDE_H_IN)
    props = prs.core_properties
    props.title = clean_text(doc_title)
    props.subject = clean_text(subtitle or "")
    props.author = "Tret"
    props.last_modified_by = "Tret"

    _pptx_title_slide(prs, look, doc_title, subtitle, date)
    for spec in slides:
        _pptx_content_slide(prs, look, spec)
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def _rgb(hex6: str):
    from pptx.dml.color import RGBColor

    return RGBColor.from_string(hex6)


def _pptx_rect(slide, look_fill: str, x: float, y: float, w: float, h: float):
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Inches

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y), Inches(w), Inches(h))
    shape.fill.solid()
    shape.fill.fore_color.rgb = _rgb(look_fill)
    shape.line.fill.background()
    shape.shadow.inherit = False
    return shape


def _pptx_title(slide, text: str, x: float, y: float, w: float, h: float, size: int, colour: str):
    from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
    from pptx.util import Inches, Pt

    shape = slide.shapes.title
    shape.left, shape.top, shape.width, shape.height = Inches(x), Inches(y), Inches(w), Inches(h)
    tf = shape.text_frame
    tf.word_wrap = True
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    par = tf.paragraphs[0]
    par.alignment = PP_ALIGN.LEFT
    run = par.add_run()
    run.text = clean_text(text)
    run.font.size, run.font.bold, run.font.name = Pt(size), True, "Calibri"
    run.font.color.rgb = _rgb(colour)
    return shape


def _pptx_textbox(slide, x: float, y: float, w: float, h: float):
    from pptx.enum.text import MSO_AUTO_SIZE
    from pptx.util import Inches

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    box.text_frame.word_wrap = True
    box.text_frame.auto_size = MSO_AUTO_SIZE.NONE
    return box


def _pptx_runs(par, spans: list[Span], size: int, colour: str, look: _Look, bold: bool = False) -> None:
    from pptx.util import Pt

    for sp in spans:
        text = clean_text(sp.text)
        if not text:
            continue
        run = par.add_run()
        run.text = text
        run.font.size = Pt(size)
        run.font.name = "Consolas" if sp.code else "Calibri"
        run.font.bold = bool(sp.bold or bold)
        run.font.italic = bool(sp.italic)
        run.font.color.rgb = _rgb(colour)
        target = link_target(sp.url)
        if target:
            run.hyperlink.address = target
            run.font.underline = True


def _pptx_bullet(par, level: int, ordered: bool, none: bool = False) -> None:
    from pptx.oxml.ns import qn
    from pptx.util import Inches

    ppr = par._p.get_or_add_pPr()
    for tag in ("a:buNone", "a:buChar", "a:buAutoNum"):
        for old in ppr.findall(qn(tag)):
            ppr.remove(old)
    if none:
        ppr.set("marL", "0")
        ppr.set("indent", "0")
        ppr.append(ppr.makeelement(qn("a:buNone"), {}))
        return
    ppr.set("marL", str(int(Inches(0.35 + 0.4 * level))))
    ppr.set("indent", str(-int(Inches(0.3))))
    if ordered:
        ppr.append(ppr.makeelement(qn("a:buAutoNum"), {"type": "arabicPeriod"}))
    else:
        ppr.append(ppr.makeelement(qn("a:buChar"), {"char": "•" if level == 0 else "–"}))


def _pptx_title_slide(prs, look: _Look, doc_title: str, subtitle: str | None, date: str) -> None:
    from pptx.util import Pt

    slide = prs.slides.add_slide(prs.slide_layouts[5])  # Title Only
    if look.report:
        _pptx_rect(slide, look.accent, 0, 0, _SLIDE_W_IN, 0.55)
        _pptx_rect(slide, look.accent, 0.9, 4.25, 1.6, 0.06)
    title = _trunc(doc_title, 120)
    _pptx_title(slide, title, 0.9, 1.9, _SLIDE_W_IN - 1.8, 2.2, 40 if len(title) <= 50 else 32, look.ink)
    y = 4.5
    if subtitle:
        box = _pptx_textbox(slide, 0.9, y, _SLIDE_W_IN - 1.8, 0.9)
        run = box.text_frame.paragraphs[0].add_run()
        run.text = clean_text(_trunc(subtitle, 160))
        run.font.size, run.font.name = Pt(22), "Calibri"
        run.font.color.rgb = _rgb(look.soft)
        y += 0.95
    box = _pptx_textbox(slide, 0.9, y, _SLIDE_W_IN - 1.8, 0.5)
    run = box.text_frame.paragraphs[0].add_run()
    run.text = date
    run.font.size, run.font.name = Pt(MIN_FONT_PT), "Calibri"
    run.font.color.rgb = _rgb(look.faint)


def _pptx_slide_number(slide, look: _Look, number: int) -> None:
    from pptx.enum.text import PP_ALIGN
    from pptx.oxml import parse_xml
    from pptx.oxml.ns import nsdecls

    box = _pptx_textbox(slide, _SLIDE_W_IN - 1.5, 6.95, 0.9, 0.35)
    par = box.text_frame.paragraphs[0]
    par.alignment = PP_ALIGN.RIGHT
    fld = parse_xml(
        f'<a:fld {nsdecls("a")} id="{{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}}" type="slidenum">'
        f'<a:rPr lang="en-US" sz="{MIN_FONT_PT * 100}"><a:solidFill><a:srgbClr val="{look.faint}"/></a:solidFill>'
        f'<a:latin typeface="Calibri"/></a:rPr><a:t>{number}</a:t></a:fld>'
    )
    par._p.append(fld)


def _pptx_content_slide(prs, look: _Look, spec: _Slide) -> None:
    from pptx.util import Pt

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    title = _trunc(spec.title, 90)
    _pptx_title(slide, title, _MARGIN_IN, 0.4, _BODY_W_IN, 0.95, 30 if len(title) <= 45 else 26, look.ink)
    if look.report:
        _pptx_rect(slide, look.accent, _MARGIN_IN, 1.38, _BODY_W_IN, 0.04)
    y = _BODY_TOP_IN
    items = spec.items
    i = 0
    while i < len(items):
        kind = items[i][0]
        if kind == "bullet":
            group = []
            while i < len(items) and items[i][0] == "bullet":
                group.append(items[i])
                i += 1
            cost = sum(_bullet_cost(len(spans_text(g[1])), g[2]) for g in group)
            height = cost / 72 + 0.15
            box = _pptx_textbox(slide, _MARGIN_IN, y, _BODY_W_IN, height)
            for n, (_, spans, level, ordered) in enumerate(group):
                par = box.text_frame.paragraphs[0] if n == 0 else box.text_frame.add_paragraph()
                par.space_after = Pt(6)
                _pptx_runs(par, spans, _bullet_font(level), look.ink, look)
                _pptx_bullet(par, level, ordered)
            y += height + 0.05
        elif kind == "box":
            _, flavour, spans = items[i]
            i += 1
            height = _box_cost(len(spans_text(spans))) / 72 + 0.1
            fill = look.tint if flavour == "quote" else "F3F5F6"
            _pptx_rect(slide, fill, _MARGIN_IN, y, _BODY_W_IN, height)
            if flavour == "quote":
                _pptx_rect(slide, look.accent if look.report else "888888", _MARGIN_IN, y, 0.08, height)
            box = _pptx_textbox(slide, _MARGIN_IN + 0.25, y + 0.05, _BODY_W_IN - 0.5, height - 0.1)
            par = box.text_frame.paragraphs[0]
            _pptx_runs(par, spans, 16, look.ink, look)
            _pptx_bullet(par, 0, False, none=True)
            y += height + 0.12
        else:
            _, rows, header, note = items[i]
            i += 1
            _pptx_table(slide, look, rows, header, note, y)
    _pptx_slide_number(slide, look, len(prs.slides))


def _pptx_table(slide, look: _Look, rows: list[list[list[Span]]], header: bool, note: bool, y: float) -> None:
    from pptx.enum.text import MSO_ANCHOR
    from pptx.util import Emu, Inches, Pt

    ncols = max(len(r) for r in rows)
    weights = [
        max(6, min(40, max((len(spans_text(r[c])) if c < len(r) else 0) for r in rows))) for c in range(ncols)
    ]
    total = sum(weights)
    heights = [Pt(_table_row_cost(r)) for r in rows]
    frame = slide.shapes.add_table(
        len(rows), ncols, Inches(_MARGIN_IN), Inches(y), Inches(_BODY_W_IN), sum(heights, Emu(0))
    )
    table = frame.table
    table.first_row = bool(header)
    table.horz_banding = False
    for c in range(ncols):
        table.columns[c].width = int(Inches(_BODY_W_IN) * weights[c] / total)
    for r_idx, row in enumerate(rows):
        table.rows[r_idx].height = heights[r_idx]
        is_head = header and r_idx == 0
        for c_idx in range(ncols):
            cell = table.cell(r_idx, c_idx)
            cell.vertical_anchor = MSO_ANCHOR.TOP
            cell.fill.solid()
            if is_head:
                fill = look.accent if look.report else "F0F0F0"
                colour = "FFFFFF" if look.report else "000000"
            else:
                fill = look.tint if (look.report and r_idx % 2 == 0) else "FFFFFF"
                colour = look.ink
            cell.fill.fore_color.rgb = _rgb(fill)
            par = cell.text_frame.paragraphs[0]
            _pptx_runs(par, row[c_idx] if c_idx < len(row) else [], MIN_FONT_PT, colour, look, bold=is_head)
    if note:
        box = _pptx_textbox(slide, _MARGIN_IN, 6.5, _BODY_W_IN, 0.4)
        run = box.text_frame.paragraphs[0].add_run()
        run.text = _TABLE_NOTE
        run.font.size, run.font.italic, run.font.name = Pt(MIN_FONT_PT), True, "Calibri"
        run.font.color.rgb = _rgb(look.faint)
