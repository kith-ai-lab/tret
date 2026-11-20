"""Degenerate-output detection for a streaming reply (R10).

Some models, usually past their context window or on a bad endpoint, start
emitting the same short token forever (`[PAD][PAD][PAD]...`, a repeated word, a
looping phrase). Left alone the run pays for thousands of tokens of nothing and
ships them as the answer. `DegenerateDetector` watches the text as it streams
and trips as soon as a short unit repeats back to back often enough that no
real reply would do it; the harness then aborts the stream, strips the tail and
retries the turn once on another model.

What counts (deliberately narrow so ordinary long text is never flagged):

* a unit of 2-16 characters that contains a letter, repeated back to back at
  least ``MIN_REPEATS`` (50) times (`[PAD]` x 50, `ha ` x 50); a unit of 4+
  characters needs ``MIN_REPEATS_LONG`` (200) unless it is a bracketed
  special token (`[PAD]`, `<pad>`, `<|endoftext|>`). Data that merely looks
  repetitive is not flagged by this rule: a unit of only digits, punctuation and
  whitespace (`0, ` x 60), an HTML entity (`&nbsp;`) or empty HTML tags
  (`<td></td>`);
* any unit of 1-16 characters (including a single character or pure
  punctuation/whitespace such as a markdown rule) repeated back to back over
  more than ``MAX_SPAN_CHARS`` (2,000) characters.

A 60-character table rule (`------`) or a wall of spaces is therefore fine; a
2,001-character one is not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MIN_REPEATS = 50
MIN_REPEATS_LONG = 200
_LONG_UNIT_CHARS = 4
MAX_SPAN_CHARS = 2000
MAX_UNIT_CHARS = 16
# The window must hold the longest pattern that can still be undetected:
# MAX_SPAN_CHARS plus one unit, with slack for a partial unit at the front.
_WINDOW_CHARS = MAX_SPAN_CHARS + 4 * MAX_UNIT_CHARS + 64
# Re-scan every this many new characters (and once more at the end of the stream).
_CHECK_EVERY_CHARS = 64

_REPEAT_RE = re.compile(r"(.{1,16}?)\1{%d,}" % (MIN_REPEATS - 1), re.DOTALL)
# One pattern per unit length for the span rule: a unit of L characters must
# repeat enough times to cover more than MAX_SPAN_CHARS characters.
_SPAN_RES = [
    (length, re.compile(r"(.{%d})\1{%d,}" % (length, -(-(MAX_SPAN_CHARS + 1) // length) - 1), re.DOTALL))
    for length in range(1, MAX_UNIT_CHARS + 1)
]


_BRACKETED_TOKEN_RE = re.compile(r"\[[^\[\]]+\]|<[^<>]+>")
_ENTITY_RE = re.compile(r"(?:&#?\w+;)+")
_HTML_TAGS = (
    "a|b|i|u|p|br|hr|li|ul|ol|td|th|tr|tbody|thead|tfoot|table|div|span|em|strong|code|pre|"
    "h[1-6]|img|section|article|svg|path|rect|g|text|tspan"
)
_EMPTY_TAGS_RE = re.compile(rf"(?:</?(?:{_HTML_TAGS})(?:\s[^<>]*)?/?>)+", re.IGNORECASE)


def _is_degenerate_unit(unit: str, count: int) -> bool:
    """Does `unit` repeated `count` times back to back count as degenerate
    output under the short-unit rule (see the module docstring)?"""
    if len(unit) < 2 or not any(ch.isalpha() for ch in unit):
        return False  # digits/punctuation/whitespace only: data, not a loop
    if _ENTITY_RE.fullmatch(unit) or _EMPTY_TAGS_RE.fullmatch(unit):
        return False
    if len(unit) >= _LONG_UNIT_CHARS and not _BRACKETED_TOKEN_RE.fullmatch(unit.strip()):
        return count >= MIN_REPEATS_LONG
    return count >= MIN_REPEATS


@dataclass(frozen=True)
class DegenerateHit:
    """A detected degenerate run."""

    pattern: str  # the repeating unit
    count: int  # how many times it repeated back to back (as far as seen)
    start: int  # offset in the streamed text where the repetition begins


def find_degenerate(text: str) -> DegenerateHit | None:
    """The first degenerate repetition in `text`, or None. Pure; offsets are
    relative to `text`."""
    for m in _REPEAT_RE.finditer(text):
        unit = m.group(1)
        count = len(m.group(0)) // len(unit)
        if _is_degenerate_unit(unit, count):
            return DegenerateHit(unit, count, m.start())
    # Long runs of any short unit (single characters, rules, whitespace).
    if len(text) > MAX_SPAN_CHARS:
        best: DegenerateHit | None = None
        for length, rx in _SPAN_RES:
            m = rx.search(text)
            if m is not None and (best is None or m.start() < best.start):
                best = DegenerateHit(m.group(1), len(m.group(0)) // length, m.start())
        return best
    return None


class DegenerateDetector:
    """Incremental wrapper over `find_degenerate` for a stream of text deltas."""

    def __init__(self) -> None:
        self._window = ""
        self._window_offset = 0  # absolute offset of self._window[0]
        self._total = 0
        self._since_check = 0
        self.hit: DegenerateHit | None = None

    def feed(self, delta: str) -> DegenerateHit | None:
        """Add a delta; return the hit the first time one is found (and on every
        later call), with `start` as an absolute offset into the streamed text."""
        if self.hit is not None:
            return self.hit
        if not delta:
            return None
        self._window += delta
        self._total += len(delta)
        self._since_check += len(delta)
        if len(self._window) > _WINDOW_CHARS:
            drop = len(self._window) - _WINDOW_CHARS
            self._window = self._window[drop:]
            self._window_offset += drop
        if self._since_check >= _CHECK_EVERY_CHARS:
            return self._scan()
        return None

    def finish(self) -> DegenerateHit | None:
        """Final scan of whatever arrived since the last periodic check."""
        if self.hit is not None:
            return self.hit
        return self._scan()

    def _scan(self) -> DegenerateHit | None:
        self._since_check = 0
        found = find_degenerate(self._window)
        if found is not None:
            self.hit = DegenerateHit(found.pattern, found.count, self._window_offset + found.start)
        return self.hit


def strip_degenerate_tail(text: str, hit: DegenerateHit) -> str:
    """`text` up to where the repetition began (trailing whitespace trimmed)."""
    return text[: max(0, hit.start)].rstrip()
