"""ESEF filing fallback: read the regulated annual-report package instead of the PDF.

Some issuers' sustainability statements are reachable only as the ESEF package
the company filed with its national register (and which filings.xbrl.org
mirrors): a ZIP holding one inline-XBRL report (`{name}/reports/{name}.xhtml`)
beside META-INF and taxonomy files. This module is the pure half of that
fallback: no network, no database, standard library only.

* `package_url_from_report_url` turns the report URL a batch target carries into
  the package URL (and refuses anything that is not filings.xbrl.org).
* `select_report_member` / `pick_report_member` choose the report inside the ZIP
  and refuse hostile entry names.
* `extract_filing_text` reads the report once, as a stream, into *units* with
  markers the rest of tret already understands: `## Page N` for a filing that is a
  PDF conversion (one block element per printed page), otherwise
  `## Section N: <heading>`. `ix:header` (the hidden facts block), scripts, styles
  and `display:none` elements are never text.
* `render_unit_blocks` / `render_extract_html` / `html_unit_lines` are what the
  packager writes and re-reads to verify its output.

This file must stay importable on its own: `packaging_worker.py` loads
`packaging.py` by path inside a small child process, and `packaging.py` loads this
file the same way. So: no `tret` imports, ever.

Paged layout detection (see `_find_page_units`), and what it gets wrong, is
documented there; the short version is a signal from the markup (class names such
as `page`/`pf`/`pageView`, a fixed page-height box, or `page-break` CSS on block
elements), then a plausibility test on the result (at least five pages, covering
most of the text, of page-like size). A filing that fails the test is read as
sections, which is always safe: the unit is then a heading, never a guess.
"""
from __future__ import annotations

import codecs
import hashlib
import html as html_lib
import re
import statistics
import unicodedata
import zipfile
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, NamedTuple

FILINGS_HOST = "filings.xbrl.org"

# Same ceiling as `net/fetch/snapshot.py::MAX_EXTRACTED_CHARS` and the upload path.
# Duplicated (this file imports nothing from tret); a test asserts they agree.
MAX_EXTRACTED_CHARS = 2_000_000
# Parsing stops collecting once this much text has been read: a runaway file must not
# exhaust memory before the unit cap is applied. Real 130 MB reports hold a few MB.
MAX_COLLECTED_CHARS = 40_000_000
# Entries in one archive; a real ESEF package holds tens.
MAX_ARCHIVE_ENTRIES = 20_000
# Element nesting beyond this is read flat (no further containers or open elements).
MAX_NEST_DEPTH = 512

LAYOUT_PAGED = "paged"
LAYOUT_SECTIONS = "sections"
UNIT_FILING_PAGE = "filing_page"
UNIT_FILING_SECTION = "filing_section"
FILING_UNITS = (UNIT_FILING_PAGE, UNIT_FILING_SECTION)

MIN_PAGES = 5
MIN_PAGE_COVERAGE = 0.8
MAX_MEDIAN_PAGE_CHARS = 20_000
MIN_SECTIONS = 3
MAX_BOLD_HEADINGS = 1000
MAX_HEADING_CHARS = 200
_CHUNK = 1024 * 1024
_STACK_SCAN = 256

REGULATED_FILING_NOTICE = (
    "regulated ESEF filing, not the issuer's own PDF; units are filing pages/sections"
)


class FilingError(ValueError):
    """The filing cannot be read: hostile archive, no report, no text, bad URL."""


# -- URLs ---------------------------------------------------------------------


def package_url_from_report_url(url: str) -> str:
    """The ESEF package (.zip) URL for a filings.xbrl.org report or package URL.

    `/{LEI}/{period}/ESEF/{country}/{n}/{name}/reports/{file}.xhtml` becomes
    `/{LEI}/{period}/ESEF/{country}/{n}/{name}.zip`; a `.zip` URL is returned as it
    is. Query and fragment are dropped. Anything else raises FilingError.
    """
    from urllib.parse import urlsplit

    parts = urlsplit((url or "").strip())
    if parts.scheme != "https":
        raise FilingError("the filing URL must be an https:// URL")
    if (parts.hostname or "").lower().rstrip(".") != FILINGS_HOST or parts.port not in (None, 443):
        raise FilingError(f"the filing URL must be on {FILINGS_HOST}")
    if parts.username or parts.password:
        raise FilingError("the filing URL must not carry credentials")
    segments = [s for s in parts.path.split("/") if s]
    if any(s in (".", "..") for s in segments):
        raise FilingError("the filing URL path is not a plain path")
    lower = [s.lower() for s in segments]
    if lower and lower[-1].endswith(".zip"):
        base = segments
    elif len(segments) >= 3 and lower[-2] == "reports" and lower[-1].endswith((".xhtml", ".html", ".htm")):
        base = [*segments[:-2]]
        if not base:
            raise FilingError("cannot derive a package URL from this filing URL")
        base[-1] = base[-1] + ".zip"
    else:
        raise FilingError(
            "the filing URL is neither a package (.zip) nor a report (.../reports/<name>.xhtml)"
        )
    if len(base) < 6 or base[2].upper() != "ESEF":
        raise FilingError(
            "the filing URL does not look like /<LEI>/<period>/ESEF/<country>/<n>/<name>"
        )
    return "https://" + FILINGS_HOST + "/" + "/".join(base)


def lei_from_filing_url(url: str) -> str | None:
    """The LEI path segment of a filings.xbrl.org URL, upper-cased, or None."""
    from urllib.parse import urlsplit

    segments = [s for s in urlsplit(url or "").path.split("/") if s]
    return segments[0].upper() if segments else None


# -- the archive --------------------------------------------------------------


class ReportChoice(NamedTuple):
    name: str
    size: int
    others: list[str]  # the other candidates, largest first
    ambiguous: bool  # more than one candidate and the largest is not clearly the report


def clip_name(name: str, limit: int = 120) -> str:
    """An archive member name safe to show: printable ASCII escapes for anything else, clipped, quoted."""
    shown = name.encode("unicode_escape").decode("ascii") if not name.isprintable() else name
    return repr(shown if len(shown) <= limit else shown[:limit] + "...")


def _unsafe_reason(info: zipfile.ZipInfo) -> str | None:
    name = info.filename
    if not name:
        return "empty name"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or unicodedata.category(ch) in ("Cc", "Cf", "Zl", "Zp") for ch in name):
        return "control character in name"
    if name.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", name):
        return "absolute path"
    if any(part == ".." for part in re.split(r"[\\/]", name)):
        return "path traversal"
    if (info.external_attr >> 16) & 0o170000 == 0o120000:
        return "symbolic link"
    return None


def select_report_member(zf: zipfile.ZipFile) -> ReportChoice:
    """The report inside an ESEF package, and the other candidates.

    Every entry of the archive is checked first, not only the chosen one: an
    archive carrying an absolute path, `..` or a symlink is refused whole. The
    report is the `.xhtml`/`.html` directly inside a `reports/` directory. With
    several candidates the largest is picked and the rest are returned for the
    record; `ambiguous` says the largest is less than twice the next.
    """
    infos = zf.infolist()
    for info in infos:
        reason = _unsafe_reason(info)
        if reason:
            raise FilingError(f"unsafe archive entry {clip_name(info.filename)} ({reason}); archive refused")
    candidates = []
    for info in infos:
        if info.is_dir():
            continue
        parts = info.filename.split("/")
        if len(parts) >= 2 and parts[-2].lower() == "reports" and parts[-1].lower().endswith(
            (".xhtml", ".html", ".htm")
        ):
            candidates.append(info)
    if not candidates:
        raise FilingError("the package has no report (.xhtml/.html inside a reports/ directory)")
    candidates.sort(key=lambda i: (-i.file_size, i.filename))
    best = candidates[0]
    ambiguous = len(candidates) > 1 and best.file_size < 2 * candidates[1].file_size
    return ReportChoice(best.filename, best.file_size, [c.filename for c in candidates[1:]], ambiguous)


def pick_report_member(zf: zipfile.ZipFile) -> str:
    """Name of the report member (see `select_report_member`)."""
    return select_report_member(zf).name


# Equal to `services/documents.py::MAX_ZIP_UNCOMPRESSED_BYTES` (a test asserts it): this file
# imports nothing from tret.
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 200 * 1024 * 1024


def refuse_oversized_archive(
    zf: zipfile.ZipFile, max_uncompressed: int = MAX_ARCHIVE_UNCOMPRESSED_BYTES
) -> None:
    """The zip-bomb check on the headers' claimed sizes (the member stream checks the real ones)."""
    infos = zf.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise FilingError(f"the archive has {len(infos)} entries, over the {MAX_ARCHIVE_ENTRIES} limit")
    total = sum(info.file_size for info in infos)
    if total > max_uncompressed:
        raise FilingError(f"the archive expands to {total} bytes, over the {max_uncompressed}-byte limit")


class MemberStream:
    """Chunks of one zip member, hashing as they go and refusing to exceed `max_bytes`.

    `file_size` in a zip header is the writer's claim; the cap is enforced on what
    actually comes out of the decompressor.
    """

    def __init__(self, zf: zipfile.ZipFile, name: str, max_bytes: int = MAX_ARCHIVE_UNCOMPRESSED_BYTES):
        info = zf.getinfo(name)
        if info.file_size > max_bytes:
            raise FilingError(f"the report is {info.file_size} bytes uncompressed, over the {max_bytes}-byte limit")
        self._fh = zf.open(info)
        self._max = max_bytes
        self._hash = hashlib.sha256()
        self.size = 0
        self._done = False

    def __iter__(self) -> Iterator[bytes]:
        while True:
            chunk = self._fh.read(_CHUNK)
            if not chunk:
                self._done = True
                self._fh.close()
                return
            self.size += len(chunk)
            if self.size > self._max:
                self._fh.close()
                raise FilingError(f"the report expands past the {self._max}-byte limit")
            self._hash.update(chunk)
            yield chunk

    def drain(self) -> None:
        """Read what a parser that stopped early left, so the hash covers the member."""
        if not self._done:
            for _ in self:
                pass

    @property
    def sha256(self) -> str:
        return self._hash.hexdigest()


# -- the model of a filing ----------------------------------------------------


class _Line:
    __slots__ = ("text", "kind", "level", "bold", "cells", "cont", "seg")

    def __init__(self, text: str, kind: str, level: int, bold: bool, cells, cont: int, seg: int):
        self.text = text
        self.kind = kind  # "p" | "row" | "img"
        self.level = level  # h1-h6 -> 1-6, else 0
        self.bold = bold
        self.cells = cells
        self.cont = cont
        self.seg = seg


@dataclass
class FilingUnit:
    number: int
    kind: str  # "page" | "section"
    heading: str | None
    folio: int | None
    lines: list = field(default_factory=list)  # list[_Line]
    images: int = 0
    folio_candidates: tuple = ()

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)

    @property
    def marker(self) -> str:
        if self.kind == "page":
            return f"## Page {self.number}"
        return f"## Section {self.number}: {self.heading or ''}".rstrip()


@dataclass
class FilingText:
    text: str
    layout: str
    units: list[FilingUnit]
    units_total: int
    units_dropped: int
    text_truncated: bool
    folios: dict[int, int]
    structure: dict[str, Any]

    @property
    def unit_count(self) -> int:
        return len(self.units)

    @property
    def unit(self) -> str:
        return UNIT_FILING_PAGE if self.layout == LAYOUT_PAGED else UNIT_FILING_SECTION


# -- the parser ---------------------------------------------------------------

_VOID = frozenset("area base br col embed hr img input link meta param source track wbr".split())
_SKIP_TAGS = frozenset({"script", "style", "noscript", "template", "head", "title", "svg", "canvas"})
_BLOCK = frozenset(
    {
        "p", "div", "section", "article", "li", "ul", "ol", "dl", "dt", "dd", "blockquote", "pre",
        "h1", "h2", "h3", "h4", "h5", "h6", "header", "footer", "nav", "aside", "main", "figure",
        "figcaption", "address", "form", "fieldset", "caption", "body", "html", "center", "table",
    }
)
_HEADINGS = {f"h{i}": i for i in range(1, 7)}
_PAGE_CLASS = re.compile(
    r"^(?:page|pg|pf|awpage|pageview|page-view|pagediv|pdfpage|pdf-page|pdf_page|pagewrap|page-wrap|"
    r"page-wrapper)(?:[-_]?\d+)?$"
)
# Shape detection: classed divs this shallow (stack depth, html and body included) are
# remembered so identical repeated siblings can be read as pages.
MAX_SHAPE_DEPTH = 8
MAX_SHAPE_CONTAINERS = 300_000
MIN_SHAPE_PAGES = 10
MIN_SHAPE_MEDIAN_CHARS = 600
MAX_MEAN_PAGE_CHARS = 15_000
_DISPLAY_NONE = re.compile(r"display\s*:\s*none\b")
_BRK_BEFORE = re.compile(r"(?:page-)?break-before\s*:\s*(?:always|page|left|right)\b")
_BRK_AFTER = re.compile(r"(?:page-)?break-after\s*:\s*(?:always|page|left|right)\b")
_POSITIONED = re.compile(r"position\s*:\s*(?:relative|absolute)\b")
_HEIGHT = re.compile(r"(?:^|[;\s])height\s*:\s*([\d.]+)\s*(pt|px|in|cm|mm)\b")
_BOLD_STYLE = re.compile(r"font-weight\s*:\s*(?:bold|[6-9]00)\b")
_TO_PX = {"px": 1.0, "pt": 4 / 3, "in": 96.0, "cm": 96 / 2.54, "mm": 96 / 25.4}
_PAGE_HEIGHT_PX = (700.0, 2200.0)
_FOLIO = re.compile(r"^(?:page\s+)?(\d{1,4})$", re.IGNORECASE)


class _Stop(Exception):
    """Collection cap reached: stop feeding the parser."""


class _Entry:
    __slots__ = ("tag", "skipped", "container", "heading", "bold", "brk_after")

    def __init__(self, tag, skipped=False, container=False, heading=False, bold=False, brk_after=False):
        self.tag = tag
        self.skipped = skipped
        self.container = container
        self.heading = heading
        self.bold = bold
        self.brk_after = brk_after


class _Container:
    """A block element that might be one page. `page` containers announce themselves
    (class name, page-height box, page-break style); the rest are shallow classed
    `div`s recorded by `key` (their class string) so repeated identical siblings can
    be recognised as pages by shape. `pdepth` counts page-kind ancestors only."""

    __slots__ = ("parent", "depth", "page", "key", "pdepth")

    def __init__(self, parent: int, depth: int, page: bool, key: str, pdepth: int):
        self.parent = parent
        self.depth = depth
        self.page = page
        self.key = key
        self.pdepth = pdepth


def _page_box(style: str) -> bool:
    if not _POSITIONED.search(style):
        return False
    m = _HEIGHT.search(style)
    if not m:
        return False
    px = float(m.group(1)) * _TO_PX[m.group(2)]
    return _PAGE_HEIGHT_PX[0] <= px <= _PAGE_HEIGHT_PX[1]


class _Walker(HTMLParser):
    def __init__(self, max_collect_chars: int):
        super().__init__(convert_charrefs=True)
        self.lines: list[_Line] = []
        self.containers: list[_Container] = []
        self.capped = False
        self.parse_error: str | None = None
        self._max_collect = max_collect_chars
        self._collected = 0
        self._stack: list[_Entry] = []
        self._skip = 0
        self._cont_stack: list[int] = []
        self._seg = 0
        self._heads: list[int] = []
        self._bold = 0
        self._buf: list[str] = []
        self._buf_level = 0
        self._buf_chars = 0
        self._buf_bold_chars = 0
        self._tdepth = 0
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    # -- line assembly --
    def _emit(self, line: _Line) -> None:
        if self.capped:
            return
        self._collected += len(line.text)
        if self._collected > self._max_collect:
            self.capped = True
            raise _Stop
        self.lines.append(line)

    def _cur_cont(self) -> int:
        return self._cont_stack[-1] if self._cont_stack else -1

    def _flush(self) -> None:
        if not self._buf:
            return
        text = " ".join("".join(self._buf).split())
        level, chars, bold_chars = self._buf_level, self._buf_chars, self._buf_bold_chars
        self._buf.clear()
        self._buf_level = self._buf_chars = self._buf_bold_chars = 0
        if text:
            self._emit(
                _Line(text, "p", level, 0 < chars == bold_chars and len(text) <= 100, None,
                      self._cur_cont(), self._seg)
            )

    def _boundary(self) -> None:
        if self._cell is not None:
            self._cell.append(" ")
        else:
            self._flush()

    def _close_cell(self) -> None:
        if self._cell is None:
            return
        text = " ".join("".join(self._cell).split())
        self._cell = None
        if self._row is None:
            self._row = []
        self._row.append(text)

    def _close_row(self) -> None:
        self._close_cell()
        row, self._row = self._row, None
        if not row:
            return
        while row and not row[-1]:
            row.pop()
        if row:
            self._emit(_Line(" | ".join(row), "row", 0, False, tuple(row), self._cur_cont(), self._seg))

    # -- tag handling --
    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _VOID:
            if self._skip:
                return
            if tag == "img":
                self._emit(_Line("", "img", 0, False, None, self._cur_cont(), self._seg))
            elif tag in ("br", "hr"):
                self._boundary()
            return
        if len(self._stack) >= MAX_NEST_DEPTH:
            # Pathologically deep markup is read flat: the text is kept, the structure is not.
            if not self._skip and tag in _BLOCK:
                self._boundary()
            return
        a = {k: (v or "") for k, v in attrs}
        style = a.get("style", "").lower()
        hidden = "hidden" in a or bool(style and _DISPLAY_NONE.search(style))
        if self._skip or tag in _SKIP_TAGS or tag.endswith(":header") or hidden:
            self._stack.append(_Entry(tag, skipped=True))
            self._skip += 1
            return

        if tag in _BLOCK:
            self._boundary()
        brk_before = bool(style and _BRK_BEFORE.search(style)) and tag not in _HEADINGS
        brk_after = bool(style and _BRK_AFTER.search(style)) and tag not in _HEADINGS
        container = False
        is_page = False
        class_key = ""
        if tag in ("div", "section", "article"):
            classes = a.get("class", "").lower().split()
            # Digits are folded so per-page style variants (`a0_ a0_02`, `a1_ a1_25`) share a key.
            class_key = re.sub(r"\d+", "#", " ".join(classes))
            is_page = (
                any(_PAGE_CLASS.match(c) for c in classes) or _page_box(style) or brk_before or brk_after
            )
            container = is_page or (
                bool(class_key)
                and len(self._stack) <= MAX_SHAPE_DEPTH
                and len(self.containers) < MAX_SHAPE_CONTAINERS
            )
        if brk_before:
            self._flush()
            self._seg += 1
        entry = _Entry(tag, container=container, brk_after=brk_after)
        if container:
            self._flush()
            parent = self._cont_stack[-1] if self._cont_stack else -1
            pdepth = 0
            for ci in reversed(self._cont_stack):  # nearest page-kind ancestor
                if self.containers[ci].page:
                    pdepth = self.containers[ci].pdepth + 1
                    break
            self.containers.append(
                _Container(parent, len(self._cont_stack), is_page, class_key, pdepth)
            )
            self._cont_stack.append(len(self.containers) - 1)
        if tag in _HEADINGS:
            self._heads.append(_HEADINGS[tag])
            entry.heading = True
        if tag in ("b", "strong") or (style and _BOLD_STYLE.search(style)):
            self._bold += 1
            entry.bold = True
        self._stack.append(entry)
        if tag == "table":
            self._tdepth += 1
        elif tag == "tr" and self._tdepth == 1:
            self._close_row()
            self._row = []
        elif tag in ("td", "th") and self._tdepth == 1:
            self._close_cell()
            if self._row is None:
                self._row = []
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID:
            return
        stack = self._stack
        for i in range(len(stack) - 1, max(-1, len(stack) - 1 - _STACK_SCAN), -1):
            if stack[i].tag == tag:
                while len(stack) > i:
                    self._pop(stack.pop())
                return

    def _pop(self, e: _Entry) -> None:
        if e.skipped:
            self._skip -= 1
            return
        if e.tag in _BLOCK:
            self._boundary()
        if e.container:
            self._flush()
            self._cont_stack.pop()
        if e.heading:
            self._heads.pop()
        if e.bold:
            self._bold -= 1
        if e.tag in ("td", "th") and self._tdepth == 1:
            self._close_cell()
        elif e.tag == "tr" and self._tdepth == 1:
            self._close_row()
        elif e.tag == "table":
            if self._tdepth == 1:
                self._close_row()
            self._tdepth -= 1
        if e.brk_after:
            self._flush()
            self._seg += 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._cell is not None:
            self._cell.append(data)
            return
        n = len(data.strip())
        if not n:
            if self._buf:
                self._buf.append(" ")
            return
        if not self._buf_chars:
            self._buf_level = self._heads[-1] if self._heads else 0
        self._buf.append(data)
        self._buf_chars += n
        if self._bold:
            self._buf_bold_chars += n

    def finish(self) -> None:
        try:
            while self._stack:
                self._pop(self._stack.pop())
            self._close_row()
            self._flush()
        except _Stop:
            pass


def _decoder_for(first: bytes):
    if first.startswith(codecs.BOM_UTF8):
        enc = "utf-8-sig"
    elif first.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        enc = "utf-16"
    else:
        enc = "utf-8"
        m = re.search(rb"""(?:encoding|charset)\s*=\s*["']?([A-Za-z0-9_.:-]+)""", first[:2048])
        if m:
            try:
                enc = codecs.lookup(m.group(1).decode("ascii")).name
            except (LookupError, UnicodeDecodeError):
                enc = "utf-8"
    return codecs.getincrementaldecoder(enc)(errors="replace")


def _chunks(source: bytes | bytearray | memoryview | Iterable[bytes]) -> Iterator[bytes]:
    if isinstance(source, (bytes, bytearray, memoryview)):
        view = memoryview(source)
        for i in range(0, len(view), _CHUNK):
            yield bytes(view[i : i + _CHUNK])
        return
    yield from source


# -- units --------------------------------------------------------------------


def _median(values: list[int]) -> float:
    return float(statistics.median(values)) if values else 0.0


def _find_page_units(w: _Walker, lines: list[_Line], total: int) -> tuple[list[int], str, int] | None:
    """(unit index per line, signal, unit count) for a paged filing; None if not paged.

    `lines` are all of the walker's lines, image markers included (they have no
    text; they are placed so the images are counted on the page they sit on).
    Two signals are tried, strongest first.

    1. Containers: block elements that are one page each, recognised by class name
       (`page`, `pf`, `pageView`, ...), by a positioned box of page height, or by
       a `page-break` style on the element itself. They can nest (a `page`
       wrapper around `pf` pages), so the shallowest depth with at least five
       containers is used. Accepted when containers with text number at least
       five, hold at least 80% of the text, and the median page is at most
       20,000 characters. Text outside a container joins the page before it (or the
       first page, if it comes first). Containers without text still count as
       pages: the numbering is the filing's.
    2. Breaks: `page-break` style on separator or content elements, headings
       excluded. Accepted by the same size tests; text between two breaks is a page
       and empty stretches are dropped.

    Weak spots: a converter that marks pages only with a stylesheet rule (class
    names outside the list, no inline style) is read as sections; a free-flowing
    document that puts `page-break-before` inline on five or more short chapters
    is read as paged; a filing whose pages are split in two columns of
    absolutely positioned boxes may fragment text within a page.
    """
    conts = w.containers
    n_conts = len(conts)

    def assign(member) -> tuple[list[int], list[int]]:
        """(unit per line, chars per member) with `member(i)` true for the page containers."""
        ids = [i for i in range(n_conts) if member(i)]
        rank = {cid: n for n, cid in enumerate(ids)}
        anc = [-1] * n_conts
        for i, c in enumerate(conts):
            anc[i] = i if member(i) else (anc[c.parent] if c.parent >= 0 else -1)
        per_unit = [-1] * len(lines)
        counts = [0] * len(ids)
        cur = -1
        for k, line in enumerate(lines):
            a = anc[line.cont] if line.cont >= 0 else -1
            if a >= 0:
                cur = rank[a]
            per_unit[k] = cur
            if cur >= 0:
                counts[cur] += len(line.text)
        return per_unit, counts

    def page_like(sizes: list[int]) -> bool:
        return _median(sizes) <= MAX_MEDIAN_PAGE_CHARS and sum(sizes) / len(sizes) <= MAX_MEAN_PAGE_CHARS

    if n_conts and total:
        # Subtree text per container, for a cheap pre-check before any line pass.
        subtree = [0] * n_conts
        for line in lines:
            if line.cont >= 0:
                subtree[line.cont] += len(line.text)
        for i in range(n_conts - 1, -1, -1):
            if conts[i].parent >= 0:
                subtree[conts[i].parent] += subtree[i]
        by_depth: dict[int, list[int]] = {}
        for i, c in enumerate(conts):
            if c.page:
                by_depth.setdefault(c.pdepth, []).append(i)
        for depth in sorted(by_depth):
            ids = by_depth[depth]
            if len(ids) < MIN_PAGES:
                continue
            sizes = [subtree[i] for i in ids if subtree[i]]
            if len(sizes) >= MIN_PAGES and sum(sizes) >= MIN_PAGE_COVERAGE * total and page_like(sizes):
                per_unit, counts = assign(lambda i, d=depth: conts[i].page and conts[i].pdepth == d)
                return [max(u, 0) for u in per_unit], f"containers@depth{depth}:{len(ids)}", len(ids)
        # Shape: identical classed siblings at one depth (obfuscated class names such as
        # `A0_02` / `stl_02` from PDF converters). Shallowest qualifying group wins.
        groups: dict[tuple[int, str], list[int]] = {}
        for i, c in enumerate(conts):
            if not c.page:
                groups.setdefault((c.depth, c.key), []).append(i)
        for (depth, key), ids in sorted(groups.items(), key=lambda kv: (kv[0][0], -len(kv[1]))):
            if len(ids) < MIN_SHAPE_PAGES:
                continue
            sizes = [subtree[i] for i in ids if subtree[i]]
            if (
                len(sizes) >= MIN_SHAPE_PAGES
                and sum(sizes) >= MIN_PAGE_COVERAGE * total
                and _median(sizes) >= MIN_SHAPE_MEDIAN_CHARS
                and page_like(sizes)
            ):
                per_unit, counts = assign(
                    lambda i, d=depth, k=key: not conts[i].page and conts[i].depth == d and conts[i].key == k
                )
                return [max(u, 0) for u in per_unit], f"shape@depth{depth}:{len(ids)}", len(ids)
    if total:
        seg_chars: dict[int, int] = {}
        for line in lines:
            if line.kind != "img":
                seg_chars[line.seg] = seg_chars.get(line.seg, 0) + len(line.text)
        order = sorted(seg_chars)
        if len(order) >= MIN_PAGES and page_like(list(seg_chars.values())):
            rank = {sg: n for n, sg in enumerate(order)}
            per_unit = []
            cur = 0
            for line in lines:
                cur = rank.get(line.seg, cur)
                per_unit.append(cur)
            return per_unit, f"page-breaks:{len(order)}", len(order)
    return None


def _heading_indexes(lines: list[_Line]) -> tuple[list[int], str]:
    for cutoff, signal in ((3, "h1-h3"), (4, "h1-h4")):
        idx = [i for i, ln in enumerate(lines) if ln.kind == "p" and 1 <= ln.level <= cutoff]
        if len(idx) >= MIN_SECTIONS:
            return idx, signal
    bold = [i for i, ln in enumerate(lines) if ln.kind == "p" and ln.bold and ln.level == 0]
    if MIN_SECTIONS <= len(bold) <= MAX_BOLD_HEADINGS:
        return bold, "bold-lines"
    return [], "none"


def _folio_in(ln: _Line) -> int | None:
    """A printed page number carried by one line: the whole line, or, for a table row
    (running headers and footers are often rows), its first or last non-empty cell
    when the row has some text (a running header or footer). Numbers read from
    table rows are noisy, so `_plausible_folios` filters the page's result. Years (1900-2100) are not folios."""
    if ln.kind == "p":
        cells = (ln.text,)
    elif ln.kind == "row" and ln.cells:
        filled = [c for c in ln.cells if c]
        if len(filled) < 2 or not any(len(c) >= 8 and not c.isdigit() for c in filled):
            return None
        cells = (filled[-1], filled[0])
    else:
        return None
    for cell in cells:
        m = _FOLIO.match(cell.strip())
        if m and not 1900 <= int(m.group(1)) <= 2100:
            return int(m.group(1))
    return None


def _folio_candidates(lines: list[_Line]) -> tuple[int, ...]:
    """Printed numbers the page's last line (a footer) and first line (a header) carry."""
    out: list[int] = []
    for ln in (lines[-1], lines[0]) if lines else ():
        n = _folio_in(ln)
        if n is not None and n not in out:
            out.append(n)
    return tuple(out)


def _plausible_folios(candidates: dict[int, tuple[int, ...]]) -> dict[int, int]:
    """One folio per page, chosen to agree with the filing's own page offset.

    Printed numbers run in step with filing pages, give or take a few (cover
    pages, split spreads), so `page - folio` clusters. A page takes the candidate
    nearest the commonest offset, within 6; figures in tables fall outside it. If the
    commonest offset covers under a quarter of the pages that have candidates (and
    3 pages), the filing has no readable folios and none are kept.
    """
    if not candidates:
        return {}
    offsets: dict[int, int] = {}
    for page, cands in candidates.items():
        for folio in cands:
            offsets[page - folio] = offsets.get(page - folio, 0) + 1
    mode, count = max(offsets.items(), key=lambda kv: (kv[1], -abs(kv[0])))
    if count < max(3, 0.25 * len(candidates)):
        return {}
    out: dict[int, int] = {}
    for page, cands in candidates.items():
        best = min(cands, key=lambda f: abs((page - f) - mode))
        if abs((page - best) - mode) <= 6:
            out[page] = best
    return out


def _build_units(w: _Walker) -> tuple[list[FilingUnit], str, str]:
    """(units, layout, signal)."""
    lines = w.lines
    total = sum(len(ln.text) for ln in lines)

    paged = _find_page_units(w, lines, total)
    if paged is not None:
        per_unit, signal, n_units = paged
        units = [FilingUnit(n + 1, "page", None, None) for n in range(n_units)]
        for k, ln in enumerate(lines):
            if ln.kind == "img":
                units[per_unit[k]].images += 1
            else:
                units[per_unit[k]].lines.append(ln)
        for u in units:
            u.folio_candidates = _folio_candidates(u.lines)
        return units, LAYOUT_PAGED, signal

    heads, signal = _heading_indexes(lines)
    if not heads:
        unit = FilingUnit(1, "section", "Filing text", None)
        for ln in lines:
            if ln.kind == "img":
                unit.images += 1
            else:
                unit.lines.append(ln)
        return [unit], LAYOUT_SECTIONS, signal
    head_set = set(heads)
    units = []
    current: FilingUnit | None = None
    for k, ln in enumerate(lines):
        if k in head_set:
            current = FilingUnit(len(units) + 1, "section", ln.text[:MAX_HEADING_CHARS], None)
            units.append(current)
            continue
        if current is None:
            if ln.kind == "img":
                continue  # an image before any text opens no unit
            current = FilingUnit(1, "section", "(start of filing)", None)
            units.append(current)
        if ln.kind == "img":
            current.images += 1
        else:
            current.lines.append(ln)
    return units, LAYOUT_SECTIONS, signal


def _escape_line(text: str) -> str:
    """A body line that looks like a marker must not be read as one."""
    return "\\" + text if text.startswith("#") else text


def render_block(unit: FilingUnit) -> str:
    body = "\n".join(_escape_line(ln.text) for ln in unit.lines)
    return f"{unit.marker}\n{body}" if body else unit.marker


def render_unit_blocks(units: Iterable[FilingUnit]) -> str:
    return "\n\n".join(render_block(u) for u in units)


def extract_filing_text(
    source: bytes | bytearray | memoryview | Iterable[bytes],
    *,
    max_chars: int = MAX_EXTRACTED_CHARS,
    max_collect_chars: int = MAX_COLLECTED_CHARS,
) -> FilingText:
    """Read an inline-XBRL report (bytes, or an iterable of byte chunks) once.

    The text comes back as marked units (`## Page N` or `## Section N: heading`,
    see the module docstring). When the marked text would exceed `max_chars`, whole
    units are kept up to the cap and the rest are counted in `units_dropped`; the
    first unit alone is cut at a line boundary if it is over the cap by itself
    (`structure["first_unit_cut"]`). Raises FilingError when the report holds no text.
    """
    walker = _Walker(max_collect_chars)
    decoder = None
    try:
        for chunk in _chunks(source):
            if decoder is None:
                decoder = _decoder_for(chunk)
            walker.feed(decoder.decode(chunk))
        if decoder is not None:
            walker.feed(decoder.decode(b"", final=True))
        walker.close()
    except _Stop:
        pass
    except Exception as exc:  # hostile markup: keep what was read
        walker.parse_error = f"{type(exc).__name__}: {exc}"
    walker.finish()

    units, layout, signal = _build_units(walker)
    if not any(ln.kind != "img" for ln in walker.lines):
        raise FilingError("the report contains no readable text")

    kept: list[FilingUnit] = []
    used = 0
    first_cut = False
    for u in units:
        block_len = len(render_block(u)) + (2 if kept else 0)
        if used + block_len > max_chars:
            if not kept:
                # One unit bigger than the whole cap: cut at a line boundary.
                trimmed: list[_Line] = []
                size = len(u.marker)
                for ln in u.lines:
                    size += len(ln.text) + 1
                    if size > max_chars:
                        break
                    trimmed.append(ln)
                u.lines = trimmed
                kept.append(u)
                first_cut = True
            break
        kept.append(u)
        used += block_len
    dropped = len(units) - len(kept)
    folios = (
        _plausible_folios({u.number: u.folio_candidates for u in kept if u.folio_candidates})
        if layout == LAYOUT_PAGED
        else {}
    )
    for u in kept:
        u.folio = folios.get(u.number)
    sizes = [len(u.text) for u in kept]
    structure = {
        "signal": signal,
        "images": sum(u.images for u in units),
        "median_unit_chars": int(_median(sizes)),
        "source_text_capped": walker.capped,
        "parse_error": walker.parse_error,
        "first_unit_cut": first_cut,
    }
    return FilingText(
        text=render_unit_blocks(kept),
        layout=layout,
        units=kept,
        units_total=len(units),
        units_dropped=dropped,
        text_truncated=bool(dropped or first_cut),
        folios=folios,
        structure=structure,
    )


# -- the packager's output and its verification --------------------------------


def render_extract_html(title: str, units: Iterable[FilingUnit], *, source_note: str) -> str:
    """The kept units as one self-contained, attribute-free HTML document.

    It is regenerated from the parsed units, not copied from the filing: there are
    no scripts, styles, links, images, `ix:` elements or attributes other than
    `data-*` unit markers, so nothing in it can load a resource or run. Images are
    dropped (they are counted in the extraction stats); the original markup stays
    in the stored ZIP. Table rows keep their cells; each run of rows is one table.
    """
    esc = html_lib.escape
    out = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        f"<title>{esc(title)}</title></head><body>",
        f"<p data-role=\"note\">{esc(source_note)}</p>",
    ]
    for u in units:
        attrs = f'data-unit="{u.kind}" data-number="{u.number}"'
        if u.folio is not None:
            attrs += f' data-folio="{u.folio}"'
        out.append(f"<section {attrs}>")
        out.append(f'<h2 data-role="marker">{esc(u.marker[3:])}</h2>')
        in_table = False
        for ln in u.lines:
            if ln.kind == "row":
                if not in_table:
                    out.append("<table>")
                    in_table = True
                out.append("<tr>" + "".join(f"<td>{esc(c)}</td>" for c in ln.cells) + "</tr>")
                continue
            if in_table:
                out.append("</table>")
                in_table = False
            out.append(f"<p>{esc(ln.text)}</p>")
        if in_table:
            out.append("</table>")
        out.append("</section>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


class _ExtractReader(HTMLParser):
    """Reads back what `render_extract_html` wrote: {unit number: [line text]}."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.units: dict[int, list[str]] = {}
        self._unit: int | None = None
        self._mode: str | None = None  # "p" | "td" | "skip"
        self._text: list[str] = []
        self._row: list[str] | None = None
        self.foreign: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        a = dict(attrs)
        if tag in ("script", "style", "img", "a", "link", "iframe", "object", "embed") or tag.endswith(":header"):
            self.foreign.append(tag)
        if tag == "section" and "data-number" in a:
            self._unit = int(a["data-number"])
            self.units[self._unit] = []
        elif tag == "h2" and a.get("data-role") == "marker":
            self._mode = "skip"
        elif tag == "p" and a.get("data-role") != "note" and self._unit is not None:
            self._mode, self._text = "p", []
        elif tag == "p":
            self._mode = "skip"
        elif tag == "tr":
            self._row = []
        elif tag == "td":
            self._mode, self._text = "td", []

    def handle_endtag(self, tag: str) -> None:
        if tag == "p" and self._mode == "p" and self._unit is not None:
            self.units[self._unit].append("".join(self._text))
        elif tag == "td" and self._row is not None:
            self._row.append("".join(self._text))
        elif tag == "tr" and self._row is not None and self._unit is not None:
            self.units[self._unit].append(" | ".join(self._row))
            self._row = None
        if tag in ("p", "td", "h2"):
            self._mode = None

    def handle_data(self, data: str) -> None:
        if self._mode in ("p", "td"):
            self._text.append(data)


def html_unit_lines(document: str) -> tuple[dict[int, list[str]], list[str]]:
    """({unit number: lines}, foreign tags found) read back from `render_extract_html` output."""
    reader = _ExtractReader()
    reader.feed(document)
    reader.close()
    return reader.units, reader.foreign
