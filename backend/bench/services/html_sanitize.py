"""Safe rendering of model-authored markdown to HTML.

Every deliverable section is markdown written by a model, and that markdown is
derived from third-party uploaded documents — the canonical prompt-injection
surface. Python-Markdown has no sanitizer: it passes raw HTML through verbatim
and it will happily emit `href="javascript:…"` or `src="http://…"` from ordinary
markdown link syntax. The rendered HTML is then served on the app origin
(`GET /api/deliverables/{slug}/export?format=html`) and handed to WeasyPrint, so
neither raw HTML nor attacker-chosen URLs may survive rendering.

Two guards, both applied inside the Markdown pipeline rather than by
post-hoc string surgery on the output:

  `_EscapeRawHtml`   raw HTML blocks and inline tags are escaped to visible
                     text instead of being restored. `<script>` becomes the
                     four visible characters, and no attribute — `onerror`
                     included — can ever reach the document, because raw tags
                     stop being tags.
  `_SafeUrls`        a tree processor over the parsed document that scrubs
                     `href`/`src` to an allowlist of schemes and drops any
                     event-handler or `style` attribute.

Working on the ElementTree (rather than re-parsing the serialized HTML) means
the URL check sees exactly the attribute values that will be emitted, with no
second parser to disagree with the first.

Only the tags Markdown itself generates can appear in the result, so there is
no tag allowlist to maintain: headings, lists, tables, emphasis, code, block
quotes and links all render normally.
"""
from __future__ import annotations

import html
import re
from xml.etree.ElementTree import Element

import markdown as md
from markdown.extensions import Extension
from markdown.postprocessors import RawHtmlPostprocessor
from markdown.treeprocessors import Treeprocessor

# Schemes a deliverable may link to. `data:` is deliberately absent: it is an
# XSS vector in `href` and there is no legitimate use in a drafted section.
ALLOWED_URL_SCHEMES = frozenset({"http", "https", "mailto"})

# Attributes dropped unconditionally wherever they appear.
_EVENT_ATTR = re.compile(r"^on", re.IGNORECASE)
_URL_ATTRS = ("href", "src", "srcset", "action", "formaction", "background", "poster")

# Characters browsers strip from a URL before resolving its scheme, so they
# cannot be used to hide one (`java\tscript:`).
_URL_NOISE = re.compile(r"[\x00-\x20\x7f]")


def is_safe_url(value: str) -> bool:
    """Whether `value` is a URL a deliverable may reference.

    Relative URLs and fragments are fine. Anything with a scheme must name one
    of `ALLOWED_URL_SCHEMES`. HTML entities are decoded first, because the
    browser decodes them too: `&#106;avascript:` is `javascript:`.
    """
    candidate = _URL_NOISE.sub("", html.unescape(value or ""))
    if not candidate:
        return False
    head, sep, _ = candidate.partition(":")
    if not sep:
        return True  # relative path, query or fragment
    if "/" in head or "?" in head or "#" in head:
        return True  # the colon is inside a path segment, not a scheme
    return head.lower() in ALLOWED_URL_SCHEMES


class _EscapeRawHtml(RawHtmlPostprocessor):
    """Restore stashed raw HTML as escaped text rather than as markup."""

    def stash_to_string(self, text) -> str:
        return html.escape(str(text), quote=False)


class _SafeUrls(Treeprocessor):
    def run(self, root: Element) -> None:
        for element in root.iter():
            for name in list(element.attrib):
                if _EVENT_ATTR.match(name) or name.lower() == "style":
                    del element.attrib[name]
                elif name.lower() in _URL_ATTRS and not is_safe_url(element.attrib[name]):
                    # Dropped, not rewritten: the link text or image alt stays
                    # visible, so a reader still sees what was written.
                    del element.attrib[name]


class SafeHtmlExtension(Extension):
    def extendMarkdown(self, md_instance) -> None:  # noqa: N802 (Markdown's API)
        md_instance.postprocessors.deregister("raw_html")
        md_instance.postprocessors.register(_EscapeRawHtml(md_instance), "raw_html", 30)
        # Priority 1: after every inline processor has built its elements.
        md_instance.treeprocessors.register(_SafeUrls(md_instance), "bench_safe_urls", 1)


def render_markdown(markdown_text: str) -> str:
    """Model-authored markdown to HTML that is safe to serve and to print."""
    return md.markdown(markdown_text, extensions=["tables", SafeHtmlExtension()])
