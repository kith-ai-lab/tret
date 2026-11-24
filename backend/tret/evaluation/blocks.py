"""Deterministic block segmentation of a normalised report, and the verbatim cutter.

A normalised report (`tret/services/normalise.py`) is Markdown with `## Page N` markers
(physical page numbers; ESEF filings also `## Section N: heading`). Here each page (or
section) is split into blocks:

* a paragraph is a run of non-blank lines (blank lines separate paragraphs),
* a Markdown table (consecutive lines starting with `|`) is ONE block,
* a heading line (`#` to `######`) is its own block.

A block's id is `p{page}.b{n}` (`s{n}.b{k}` in a section unit), `n` counting from 1 in the
unit. Segmentation is a pure function of the Markdown, so the same file always yields the same
ids. The model is shown `annotate()` (each block prefixed `[p12.b3]`) and answers with block
ids only; `cut()` then returns the original text of the named blocks, never anything the model
wrote. `windows()` splits a report that is too big for one call into page-aligned windows.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

PAGE_RE = re.compile(r"^## Page (\d+)[ \t]*$")
SECTION_RE = re.compile(r"^## Section (\d+)(?::.*)?[ \t]*$")
HEADING_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S")
BLOCK_ID_RE = re.compile(r"^([ps]\d+)\.b(\d+)$")
PREAMBLE_KEY = "p0"  # text before the first marker, if any

KIND_PARAGRAPH = "paragraph"
KIND_TABLE = "table"
KIND_HEADING = "heading"


class BlockError(ValueError):
    """A block id that does not exist, or a range that runs backwards."""


@dataclass(frozen=True)
class Block:
    id: str
    unit: str  # `p12` or `s3`
    n: int  # 1-based position in the unit
    kind: str
    text: str
    index: int  # position in the whole document


@dataclass
class Unit:
    key: str  # `p12` / `s3`
    number: int
    marker: str  # the marker line as written (`## Page 12`)
    blocks: list[Block] = field(default_factory=list)

    @property
    def is_page(self) -> bool:
        return self.key.startswith("p")


@dataclass
class Segmentation:
    units: list[Unit]
    blocks: list[Block]
    by_id: dict[str, Block]

    def block(self, block_id: str) -> Block:
        b = self.by_id.get(block_id.strip() if isinstance(block_id, str) else block_id)
        if b is None:
            raise BlockError(f"unknown block id {block_id!r}")
        return b

    def page_of(self, block_id: str) -> int | None:
        """The physical page number of a block; None for a block in an ESEF section unit."""
        b = self.block(block_id)
        return int(b.unit[1:]) if b.unit.startswith("p") else None

    def section_of(self, block_id: str) -> int | None:
        """The ESEF section number of a block; None for a block on a page."""
        b = self.block(block_id)
        return int(b.unit[1:]) if b.unit.startswith("s") else None


@dataclass
class Window:
    index: int
    units: list[str]
    text: str  # annotated
    est_tokens: int
    first_index: int = 0  # document positions of its first and last block (inclusive)
    last_index: int = -1
    oversized: bool = False  # one block alone is larger than the window limit

    def contains(self, block: "Block") -> bool:
        return self.first_index <= block.index <= self.last_index


def estimate_tokens(text: str) -> int:
    from tret.engine.context import estimate_tokens as _estimate

    return _estimate(text)


def _split_unit(lines: list[str]) -> list[tuple[str, str]]:
    """(kind, text) for each block of one unit's lines."""
    out: list[tuple[str, str]] = []
    cur: list[str] = []
    mode: str | None = None  # paragraph | table

    def flush() -> None:
        nonlocal cur, mode
        if cur and mode:
            out.append((mode, "\n".join(cur)))
        cur, mode = [], None

    for line in lines:
        if not line.strip():
            flush()
        elif line.lstrip().startswith("|"):
            if mode != KIND_TABLE:
                flush()
                mode = KIND_TABLE
            cur.append(line)
        elif HEADING_RE.match(line):
            flush()
            out.append((KIND_HEADING, line))
        else:
            if mode != KIND_PARAGRAPH:
                flush()
                mode = KIND_PARAGRAPH
            cur.append(line)
    flush()
    return out


def segment(markdown: str) -> Segmentation:
    """Split `markdown` into units and blocks. Deterministic."""
    # A marker only starts a unit when its number is one more than the previous accepted marker of
    # its kind (pages from 1, sections from 1), as `services/page_check.split_pages` reads the same
    # file. Any other `## Page N` line is ordinary text of the current unit, so a unit is always
    # contiguous and a repeated or backwards marker (for instance inside a quoted passage) cannot
    # reopen an earlier page.
    raw: list[tuple[str, int, str, list[str]]] = []  # key, number, marker, lines
    key, number, marker, lines = PREAMBLE_KEY, 0, "", []
    pages_seen = sections_seen = 0
    for line in markdown.splitlines():
        k = None
        m = PAGE_RE.match(line)
        if m is not None and int(m.group(1)) == pages_seen + 1:
            pages_seen += 1
            k = f"p{pages_seen}"
        else:
            m = SECTION_RE.match(line)
            if m is not None and int(m.group(1)) == sections_seen + 1:
                sections_seen += 1
                k = f"s{sections_seen}"
        if k is not None:
            raw.append((key, number, marker, lines))
            key, number, marker, lines = k, int(k[1:]), line, []
        else:
            lines.append(line)
    raw.append((key, number, marker, lines))

    units: list[Unit] = []
    by_key: dict[str, Unit] = {}
    blocks: list[Block] = []
    by_id: dict[str, Block] = {}
    for key, number, marker, lines in raw:
        pieces = _split_unit(lines)
        if key == PREAMBLE_KEY and not pieces:
            continue
        unit = by_key.get(key)
        if unit is None:  # a repeated marker continues the same unit, so ids stay unique
            unit = Unit(key=key, number=number, marker=marker)
            by_key[key] = unit
            units.append(unit)
        for kind, text in pieces:
            n = len(unit.blocks) + 1
            b = Block(id=f"{key}.b{n}", unit=key, n=n, kind=kind, text=text, index=len(blocks))
            unit.blocks.append(b)
            blocks.append(b)
            by_id[b.id] = b
    return Segmentation(units=units, blocks=blocks, by_id=by_id)


def _render_block(b: Block) -> str:
    sep = "\n" if b.kind == KIND_TABLE else " "
    return f"[{b.id}]{sep}{b.text}"


def _render_unit(u: Unit) -> str:
    parts = [u.marker] if u.marker else []
    parts.extend(_render_block(b) for b in u.blocks)
    return "\n\n".join(parts)


def annotate(markdown: str, pages: list[str] | None = None, *, seg: Segmentation | None = None) -> str:
    """The text shown to the model: each block prefixed with its id (`[p12.b3] ...`; a table's
    id sits on the line above it so the table stays valid Markdown). `pages` limits the output
    to those unit keys (`p12`, `s3`); None = every unit."""
    seg = seg or segment(markdown)
    wanted = None if pages is None else set(pages)
    return "\n\n".join(_render_unit(u) for u in seg.units if wanted is None or u.key in wanted) + "\n"


def cut(markdown: str, start_id: str, end_id: str, *, seg: Segmentation | None = None) -> str:
    """The original text of blocks `start_id` to `end_id` inclusive, in document order and
    across pages, blocks separated by one blank line; no ids, no page markers. Raises
    `BlockError` for an unknown id or `start` after `end`."""
    seg = seg or segment(markdown)
    a, b = seg.block(start_id), seg.block(end_id)
    if a.index > b.index:
        raise BlockError(f"span {start_id}..{end_id} runs backwards")
    return "\n\n".join(x.text for x in seg.blocks[a.index : b.index + 1])


def windows(markdown: str, max_tokens: int, *, seg: Segmentation | None = None) -> list[Window]:
    """Page-aligned windows of at most `max_tokens` estimated tokens (chars / 4) of annotated
    text each, covering every block exactly once in document order. A unit larger than the limit
    is split at block boundaries into several windows (each shows the unit's marker, the
    continuations marked `(continued)`); a single block that alone exceeds the limit becomes a
    window flagged `oversized`, which the extractor fails with a clear issue."""
    seg = seg or segment(markdown)
    limit = max(1, int(max_tokens))
    out: list[Window] = []
    cur: list[tuple[Unit, list[Block], bool]] = []  # (unit, its blocks in this window, continued)
    cur_tokens = 0

    def render(parts) -> str:
        chunks = []
        for u, bs, continued in parts:
            head = [f"{u.marker} (continued)" if continued and u.marker else u.marker] if u.marker else []
            chunks.append("\n\n".join([*head, *(_render_block(b) for b in bs)]))
        return "\n\n".join(chunks) + "\n"

    def close(oversized: bool = False) -> None:
        nonlocal cur, cur_tokens
        if cur:
            blocks_ = [b for _, bs, _ in cur for b in bs]
            text = render(cur)
            keys: list[str] = []
            for u, _, _ in cur:
                if u.key not in keys:
                    keys.append(u.key)
            out.append(Window(len(out), keys, text, estimate_tokens(text), blocks_[0].index, blocks_[-1].index, oversized))
        cur, cur_tokens = [], 0

    for u in seg.units:
        t = estimate_tokens(_render_unit(u)) + 2
        if cur and cur_tokens + t > limit:
            close()
        if t <= limit:
            cur.append((u, list(u.blocks), False))
            cur_tokens += t
            continue
        # a unit bigger than the window: split it at block boundaries
        close()
        piece: list[Block] = []
        piece_tokens = estimate_tokens(u.marker) + 8
        continued = False
        for b in u.blocks:
            bt = estimate_tokens(_render_block(b)) + 2
            if bt + piece_tokens > limit and not piece:
                cur.append((u, [b], continued))
                close(oversized=True)
                continued = True
                piece_tokens = estimate_tokens(u.marker) + 8
                continue
            if piece and piece_tokens + bt > limit:
                cur.append((u, piece, continued))
                close()
                continued = True
                piece, piece_tokens = [], estimate_tokens(u.marker) + 8
            piece.append(b)
            piece_tokens += bt
        if piece:
            cur.append((u, piece, continued))
            close()
    close()
    return out
