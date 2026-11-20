"""A linear-time HTML tokenizer and visible-text extractor.

The stdlib `html.parser` re-scans from the same position whenever a tag is
incomplete, so hostile input such as `"<a " * n` costs seconds at tens of KB.
This module replaces it for the two places that read HTML the app did not
write: document extraction (`services/documents.py::_extract_html`) and the
structural check of a reply's HTML (`engine/html_check.py`).

Every scanning step either advances past what it examined or ends the scan, so
the cost is O(len(html)) whatever the input: a failed `find` (no `>` anywhere
after a `<a`, no `-->`, no closing `</script`) means nothing later can succeed
either, and the remainder is dropped. It is deliberately not a conforming HTML5
tokenizer: it understands tags, comments, declarations, raw-text elements and
text, which is all either caller needs. A tag ends at the first `>`.

`tokenize` yields plain tuples `(kind, value, line, attrs)`:

  TEXT       value = unescaped text, attrs None
  START      value = lower-case tag name; attrs = {"colspan": .., "rowspan": ..}
             for td/th (raw strings, only the keys present), else None
  END        value = lower-case tag name
  SELFCLOSE  value = lower-case tag name (`<br/>`, `<path />`)

`line` is the 1-based line of the token's first character. `script` and `style`
are raw-text elements: their content is never tokenized (a START and an END are
still yielded). `line_count` gives the number of lines in the input.
"""

from __future__ import annotations

import html as _html
import re
from typing import Iterator

TEXT, START, END, SELFCLOSE = 0, 1, 2, 3

Token = tuple[int, str, int, "dict[str, str] | None"]

_NAME_RE = re.compile(r"[A-Za-z][^\s/>]*")
_SPAN_RE = re.compile(r"""\b(colspan|rowspan)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>"']*))""", re.IGNORECASE)
_RAW_TEXT = frozenset({"script", "style"})
_RAW_CLOSE = {name: re.compile(r"</" + name, re.IGNORECASE) for name in _RAW_TEXT}

# Elements whose content is not visible text.
SKIP_TAGS = frozenset({"script", "style", "template", "noscript"})
# Elements that start (and end) a line of visible text.
BLOCK_TAGS = frozenset(
    {
        "p", "div", "section", "article", "main", "aside", "header", "footer", "nav", "br", "hr",
        "blockquote", "pre", "table", "ul", "ol", "dl", "dt", "dd", "figure", "figcaption",
        "h1", "h2", "h3", "h4", "h5", "h6", "form", "fieldset", "tr", "li", "title",
    }
)


def line_count(html: str) -> int:
    return html.count("\n") + 1


def tokenize(s: str) -> Iterator[Token]:
    n = len(s)
    pos = 0  # start of the pending text run
    i = 0  # scan position
    line_no, line_pos = 1, 0

    def line_at(idx: int) -> int:
        nonlocal line_no, line_pos
        line_no += s.count("\n", line_pos, idx)
        line_pos = idx
        return line_no

    while True:
        i = s.find("<", i)
        if i < 0:
            break
        nxt = s[i + 1 : i + 2]
        if nxt == "!" or nxt == "?":
            if s.startswith("<!--", i):
                end = s.find("-->", i + 4)
                width = 3
            else:
                end = s.find(">", i + 2)
                width = 1
            if pos < i:
                yield TEXT, _html.unescape(s[pos:i]), line_at(pos), None
            if end < 0:  # unterminated: the rest is comment/declaration
                return
            i = pos = end + width
            continue
        closing = nxt == "/"
        m = _NAME_RE.match(s, i + (2 if closing else 1))
        if m is None:
            if closing:  # `</ x>` / `</>`: a bogus comment up to `>`
                end = s.find(">", i + 2)
                if pos < i:
                    yield TEXT, _html.unescape(s[pos:i]), line_at(pos), None
                if end < 0:
                    return
                i = pos = end + 1
            else:  # a lone `<` is text
                i += 1
            continue
        end = s.find(">", m.end())
        if pos < i:
            yield TEXT, _html.unescape(s[pos:i]), line_at(pos), None
        if end < 0:  # an incomplete tag at the end: nothing after it can be a tag
            return
        name = m.group(0).lower()
        tag_line = line_at(i)
        if closing:
            yield END, name, tag_line, None
            i = pos = end + 1
            continue
        attrs = None
        if name in ("td", "th"):
            found = {
                sm.group(1).lower(): next(g for g in sm.groups()[1:] if g is not None)
                for sm in _SPAN_RE.finditer(s, m.end(), end)
            }
            attrs = found or None
        if s[end - 1] == "/":
            yield SELFCLOSE, name, tag_line, attrs
            i = pos = end + 1
            continue
        yield START, name, tag_line, attrs
        i = pos = end + 1
        if name in _RAW_TEXT:
            close = _RAW_CLOSE[name].search(s, i)
            if close is None:  # unterminated: everything after is the element's content
                return
            gt = s.find(">", close.end())
            yield END, name, line_at(close.start()), None
            if gt < 0:
                return
            i = pos = gt + 1
    if pos < n:
        tail = s[pos:]
        yield TEXT, _html.unescape(tail), line_at(pos), None


def visible_text(html: str) -> str:
    """Visible text for a model to read: headings keep a `#` marker, list items
    a `- `, table cells are joined with ` | `, block elements break lines.
    script/style/template/noscript content never appears (not even when an end
    tag is missing: the skip is a depth counter on the element's own name)."""
    out: list[str] = []
    skip: str | None = None
    depth = 0

    def brk() -> None:
        if out and out[-1] != "\n":
            out.append("\n")

    for kind, value, _line, _attrs in tokenize(html):
        if kind == TEXT:
            if skip:
                continue
            collapsed = re.sub(r"\s+", " ", value)
            if collapsed.strip():
                out.append(collapsed if out and out[-1] not in ("\n", " | ") else collapsed.lstrip())
        elif kind == START:
            if skip:
                if value == skip:
                    depth += 1
                continue
            if value in SKIP_TAGS:
                skip, depth = value, 1
                continue
            if value in BLOCK_TAGS:
                brk()
            if len(value) == 2 and value[0] == "h" and value[1] in "123456":
                out.append("#" * int(value[1]) + " ")
            elif value == "li":
                out.append("- ")
            elif value in ("td", "th"):
                if out and out[-1] not in ("\n", " | "):
                    out.append(" | ")
        elif kind == SELFCLOSE:
            if not skip and value in BLOCK_TAGS:
                brk()
        else:  # END
            if skip:
                if value == skip:
                    depth -= 1
                    if depth <= 0:
                        skip, depth = None, 0
                continue
            if value in BLOCK_TAGS:
                brk()
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in "".join(out).split("\n")]
    text = "\n".join(ln for ln in lines if ln and ln not in ("|", "-"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()
