"""HTML to plain text, using only the standard library.

Deliberately no new dependency. A readability-style extractor would produce
prettier text, and it would also be a third parser running over hostile bytes
inside the request path — tret already has two of those (pypdf, python-docx)
and treats them as the risk they are (`api/documents.py::_extract_bounded`).
`html.parser` is the one that ships with Python.

What it does: drop the elements whose text is never content (`script`, `style`,
`nav`, `header`, `footer`, `form`, `svg`), keep the `<title>`, insert line breaks
at block boundaries so headings and paragraphs do not run together, and collapse
runs of whitespace. What it does not do: interpret layout, follow frames, or run
anything. A page whose text only exists after JavaScript comes back nearly empty,
and the caller says so rather than pretending otherwise.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser

# Text inside these is chrome, boilerplate or code — never the page's content.
_SKIP_ELEMENTS = frozenset(
    {"script", "style", "noscript", "svg", "canvas", "nav", "header", "footer", "form", "template"}
)
# Elements after which a line break belongs, so a heading does not weld itself
# to the paragraph below it.
_BLOCK_ELEMENTS = frozenset(
    {
        "p", "div", "section", "article", "br", "hr", "li", "tr", "td", "th",
        "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "table", "ul", "ol",
    }
)

_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACE = re.compile(r"[ \t]+\n")
_RUNS_OF_SPACE = re.compile(r"[ \t]{2,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._parts: list[str] = []
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in _SKIP_ELEMENTS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_ELEMENTS:
            # Never below zero: a stray `</script>` with no opener is common in
            # real pages, and letting the counter go negative would suppress the
            # rest of the document.
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_ELEMENTS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        if data.strip():
            self._parts.append(data)

    def text(self) -> str:
        joined = "".join(self._parts)
        joined = _RUNS_OF_SPACE.sub(" ", joined)
        joined = _TRAILING_SPACE.sub("\n", joined)
        return _BLANK_LINES.sub("\n\n", joined).strip()


def html_to_text(html: str) -> tuple[str, str]:
    """(text, title). Malformed markup yields what could be read, never an error."""
    parser = _TextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # a parser giving up on hostile markup is not a run failure
        pass
    return parser.text(), parser.title.strip()[:300]
