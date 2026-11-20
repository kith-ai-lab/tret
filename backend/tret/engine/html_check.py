"""Structural check of HTML a chat reply ships as a deliverable (R13).

A deck or page written into a reply is a file a person will open, not prose:
a stray `<td>` in a table row, a `<div>` that never closes, or a document cut
off before `</html>` (the model ran out of output) renders broken and nobody
sees why. `check_reply_html` parses every ```html fence (or the whole reply
when it is itself an HTML document) with the linear scanner in `html_scan` (stdlib `html.parser` is quadratic on hostile input) and reports what a
browser would silently "fix" or give up on. It checks structure only — it
does not judge content, CSS or scripts.

The harness calls `check_reply_html` after the number check and, when there are
issues and the reply was not already truncated, sends one `html_repair_nudge`
asking the model to reissue the corrected file.

HTML allows several end tags to be omitted (`</li>`, `</td>`, `</p>` ...) and
void elements (`<br>`, `<img>`) never close, so none of those are issues.
"""

from __future__ import annotations

import re

from tret.engine.grounding import _HTML_DOC_RE, _fence_spans
from tret.engine.html_scan import END, SELFCLOSE, START, line_count, tokenize

# Elements that never have an end tag.
_VOID = frozenset(
    {
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta",
        "param", "source", "track", "wbr", "command", "keygen",
    }
)
# Elements whose end tag HTML lets an author omit.
_OPTIONAL_END = frozenset(
    {
        "li", "dt", "dd", "p", "tr", "td", "th", "thead", "tbody", "tfoot", "option",
        "optgroup", "colgroup", "rp", "rt", "body", "head",
    }
)
# Opening one of these ends an open <p>.
_CLOSES_P = frozenset(
    {
        "address", "article", "aside", "blockquote", "details", "div", "dl", "fieldset",
        "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "main", "menu", "nav", "ol", "p", "pre", "section", "table", "ul",
    }
)
_MAX_ISSUES = 20
# A block is a document/deliverable (checked) only when it says so or is long; a
# short snippet that explains markup (`<tr><td>..`, a lone `<div>`) is a fragment
# and is not held to document structure.
_DELIVERABLE_RE = re.compile(r"<!doctype|<html|<body", re.IGNORECASE)
_DELIVERABLE_MIN_LINES = 40


def _is_deliverable(text: str) -> bool:
    if _DELIVERABLE_RE.search(text):
        return True
    lines = text.strip("\n").split("\n")
    if lines and lines[-1].strip().startswith("```"):  # the span can include the closing fence line
        lines.pop()
    return len(lines) >= _DELIVERABLE_MIN_LINES
# Deeper than any real page nests; past it the check stops rather than scan a
# hostile tower of open tags on every end tag.
_MAX_DEPTH = 500


class _Checker:
    def __init__(self, block: int, base_line: int) -> None:
        self.block = block
        self.base_line = base_line
        self.issues: list[dict] = []
        self.stack: list[tuple[str, int]] = []
        self.tables: list[dict] = []
        self.saw_html_open = False
        self.saw_html_close = False
        self.too_deep = False
        self.total_lines = 1

    # ── helpers ──
    def _issue(self, kind: str, detail: str, line: int) -> None:
        if len(self.issues) >= _MAX_ISSUES:
            return
        self.issues.append({"block": self.block, "kind": kind, "detail": detail, "line": line})

    def _top(self) -> str | None:
        return self.stack[-1][0] if self.stack else None

    def _implicit_close(self, tag: str) -> None:
        if tag in ("td", "th"):
            if self._top() in ("td", "th"):
                self.stack.pop()
        elif tag == "tr":
            while self._top() in ("td", "th", "tr"):
                self.stack.pop()
        elif tag in ("thead", "tbody", "tfoot"):
            while self._top() in ("td", "th", "tr", "thead", "tbody", "tfoot"):
                self.stack.pop()
        elif tag == "li":
            while self._top() == "p":
                self.stack.pop()
            if self._top() == "li":
                self.stack.pop()
        elif tag in ("dt", "dd"):
            while self._top() in ("p", "dt", "dd"):
                self.stack.pop()
        elif tag == "option":
            if self._top() == "option":
                self.stack.pop()
        if tag in _CLOSES_P and self._top() == "p":
            self.stack.pop()

    # ── tables ──
    def _close_row(self, table: dict) -> None:
        row = table["row"]
        if row is not None:
            table["rows"].append(row)
            table["row"] = None

    def _finish_table(self, table: dict) -> None:
        self._close_row(table)
        rows = table["rows"]
        if len(rows) < 2 or table["rowspan"]:
            return
        header = rows[0]["cells"]
        for row in rows[1:]:
            if row["cells"] != header:
                self._issue(
                    "row_cell_count",
                    f"table row has {row['cells']} cells but the header row has {header} "
                    "(a stray or missing <td>)",
                    row["line"],
                )

    # ── token handlers; `line` is already a reply line ──
    def feed(self, text: str) -> None:
        for kind, tag, rel_line, attrs in tokenize(text):
            if kind == START:
                self.handle_starttag(tag, attrs, self.base_line + rel_line - 1)
            elif kind == END:
                self.handle_endtag(tag, self.base_line + rel_line - 1)
            elif kind == SELFCLOSE:
                self.handle_startendtag(tag)
            else:
                continue
            if self.too_deep:
                break
        self.total_lines = line_count(text)

    def handle_starttag(self, tag, attrs, line):
        if tag == "html":
            self.saw_html_open = True
        if tag in _VOID:
            return
        if tag == "table":
            self._implicit_close(tag)
            self.tables.append({"row": None, "rows": [], "rowspan": False, "line": line})
        elif tag == "tr":
            if not self.tables:
                self._issue("stray_row", "<tr> outside a <table>", line)
            else:
                self._close_row(self.tables[-1])
                self.tables[-1]["row"] = {"cells": 0, "line": line}
        elif tag in ("td", "th"):
            attr = attrs or {}
            span = 1
            try:
                span = max(1, int(attr.get("colspan") or 1))
            except ValueError:
                pass
            if attr.get("rowspan") not in (None, "", "1") and self.tables:
                self.tables[-1]["rowspan"] = True
            if not self.tables or self.tables[-1]["row"] is None:
                self._issue("stray_cell", f"<{tag}> outside a table row", line)
            else:
                self.tables[-1]["row"]["cells"] += span
        self._implicit_close(tag)
        self.stack.append((tag, line))
        if len(self.stack) > _MAX_DEPTH:
            self.too_deep = True
            self._issue("unclosed_tag", f"elements are nested more than {_MAX_DEPTH} deep", line)

    def handle_startendtag(self, tag):
        # `<path />`, `<div />`: self-closing, never pushed.
        if tag == "html":
            self.saw_html_open = True

    def handle_endtag(self, tag, line):
        if tag == "html":
            self.saw_html_close = True
        if tag in _VOID:
            return
        if tag == "table" and self.tables:
            self._finish_table(self.tables.pop())
        elif tag == "tr" and self.tables:
            self._close_row(self.tables[-1])
        idx = next((i for i in range(len(self.stack) - 1, -1, -1) if self.stack[i][0] == tag), None)
        if idx is None:
            if tag not in _OPTIONAL_END:
                self._issue("stray_end_tag", f"</{tag}> has no matching <{tag}>", line)
            return
        for name, opened in self.stack[idx + 1 :]:
            if name not in _OPTIONAL_END:
                self._issue(
                    "unclosed_tag",
                    f"<{name}> opened at line {opened} is not closed before </{tag}>",
                    opened,
                )
        del self.stack[idx:]

    def finish(self) -> list[dict]:
        for table in self.tables:
            self._finish_table(table)
        if self.saw_html_open and not self.saw_html_close:
            self._issue(
                "missing_end_html",
                "the document has <html> but no </html> — it looks cut off",
                self.base_line + max(0, self.total_lines - 1),
            )
        for name, opened in self.stack:
            if name in _OPTIONAL_END or name == "html":
                continue
            self._issue("unclosed_tag", f"<{name}> opened at line {opened} is never closed", opened)
        return self.issues


def _html_blocks(reply: str) -> list[tuple[str, int]]:
    """`(html text, 1-based reply line of its first line)` for each block."""
    if _HTML_DOC_RE.match(reply):
        lead = len(reply) - len(reply.lstrip())
        return [(reply[lead:], reply.count("\n", 0, lead) + 1)]
    blocks = []
    for _start, body_start, end, lang in _fence_spans(reply):
        if lang in ("html", "htm"):
            blocks.append((reply[body_start:end], reply.count("\n", 0, body_start) + 1))
    return blocks


def check_reply_html(reply: str) -> dict:
    """`{"status": "ok"|"issues"|"skipped", "blocks": int, "fragments": int,
    "issues": [{"block", "kind", "detail", "line"}]}`. "skipped" when the reply
    has no HTML or every block is a fragment (see `_is_deliverable`); `line` is a
    1-based line of the reply."""
    blocks = _html_blocks(reply or "")
    if not blocks:
        return {"status": "skipped", "blocks": 0, "fragments": 0, "issues": []}
    issues: list[dict] = []
    fragments = 0
    for n, (text, base_line) in enumerate(blocks, start=1):
        if not _is_deliverable(text):
            fragments += 1
            continue
        checker = _Checker(n, base_line)
        checker.feed(text)
        issues.extend(checker.finish())
    if fragments == len(blocks):
        return {"status": "skipped", "blocks": len(blocks), "fragments": fragments, "issues": []}
    issues = issues[:_MAX_ISSUES]
    return {
        "status": "issues" if issues else "ok",
        "blocks": len(blocks),
        "fragments": fragments,
        "issues": issues,
    }


def html_repair_nudge(result: dict) -> str:
    """The message that asks the model to reissue a corrected file."""
    issues = result.get("issues") or []
    lines = [f"- line {i.get('line')}: {i.get('detail')}" for i in issues[:8]]
    more = f"\n- ... and {len(issues) - 8} more" if len(issues) > 8 else ""
    cut_off = any(i.get("kind") == "missing_end_html" for i in issues)
    extra = (
        " The file looks cut off, so write it in smaller pieces: save it with draft_file "
        "(mode append), one section at a time, then share the saved file."
        if cut_off
        else ""
    )
    return (
        "HTML check: the HTML in your reply has structural problems that will make it render "
        "wrongly:\n"
        + "\n".join(lines)
        + more
        + "\nReissue the complete corrected file in a single ```html block. Fix only these "
        "problems; keep the content, figures and styling as they are." + extra
    )


__all__ = ["check_reply_html", "html_repair_nudge"]
