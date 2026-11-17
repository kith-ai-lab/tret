"""Document themes for the deliverable exporter (html + pdf).

Two themes exist: ``plain`` (the original output, byte-for-byte; it lives in
`services/export.py` and is not touched here) and ``report`` (a cover page,
running header/footer, a typographic scale, styled tables and callouts).

Everything is inline: no fonts are bundled or fetched, no external URLs, no
JavaScript. The production image ships only DejaVu fonts, so each font stack
lists nicer system faces first and ends in a DejaVu family, which is the
guaranteed member. The accent colour is the only caller-supplied value that
reaches the stylesheet and is validated against a strict hex pattern first.
"""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

EXPORT_THEMES = ("plain", "report")

DEFAULT_ACCENT = "#1f5f4a"
_ACCENT_RE = re.compile(r"#[0-9a-fA-F]{6}")


class ExportOptionError(ValueError):
    """A caller-supplied export option (format, theme, accent) is invalid.

    A `ValueError` subclass so existing `except ValueError` sites keep working;
    the export route maps exactly this class to a 422 with its message, and
    treats any other `ValueError` from rendering as an internal failure.
    """


class RendererUnavailable(ValueError):
    """An optional renderer's package is missing in this environment (the route
    answers 501, like `PdfUnavailable`)."""


def validate_accent(accent: str | None) -> str | None:
    """Return a safe `#rrggbb` accent (lower-cased) or None when not given.

    Raises ExportOptionError (a ValueError) for anything else: the value is interpolated into CSS,
    so nothing but a six-digit hex colour may pass (`fullmatch`, so a trailing
    newline or `;}` payload cannot ride along).
    """
    if accent is None:
        return None
    if not isinstance(accent, str) or not _ACCENT_RE.fullmatch(accent):
        raise ExportOptionError("accent must be a #rrggbb hex colour")
    return accent.lower()


def validate_theme(theme: str | None) -> str:
    """Normalise `theme` (None means plain); ValueError on an unknown name."""
    if theme is None:
        return "plain"
    if theme not in EXPORT_THEMES:
        raise ExportOptionError(f"theme must be one of {'|'.join(EXPORT_THEMES)}, got {theme!r}")
    return theme


def _tint(hex_colour: str, amount: float) -> str:
    """Mix a validated `#rrggbb` toward white by `amount` (0..1)."""
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    mix = lambda c: round(c + (255 - c) * amount)  # noqa: E731
    return f"#{mix(r):02x}{mix(g):02x}{mix(b):02x}"


_REPORT_CSS = """
/* ---- tokens: override these to brand the document ---- */
:root {
  --accent: @ACCENT@;
  --accent-soft: @ACCENT_SOFT@;
  --ink: #1b1f23;
  --ink-soft: #4a525a;
  --ink-faint: #7b848c;
  --rule: #d5d9dd;
  --fill: #f3f5f6;
  --font-serif: 'Charter', 'Iowan Old Style', 'Palatino Linotype', 'DejaVu Serif', Georgia, serif;
  --font-sans: 'Inter', 'Helvetica Neue', 'Segoe UI', 'DejaVu Sans', Arial, sans-serif;
  --font-mono: 'SF Mono', Menlo, 'DejaVu Sans Mono', monospace;
}

/* ---- page setup (paged media: PDF and print) ---- */
@page {
  size: A4;
  margin: 3cm 2.2cm 2.5cm 2.2cm;
  @top-left { content: string(doctitle); font: 7.5pt 'Inter', 'Helvetica Neue', 'DejaVu Sans', Arial, sans-serif;
              color: #7b848c; letter-spacing: 0.04em; text-transform: uppercase;
              vertical-align: bottom; padding-bottom: 12pt; }
  @bottom-right { content: "Page " counter(page) " of " counter(pages);
                  font: 7.5pt 'Inter', 'Helvetica Neue', 'DejaVu Sans', Arial, sans-serif; color: #7b848c; }
}
@page :first {
  margin: 0;
  @top-left { content: none; }
  @bottom-right { content: none; }
}

body {
  margin: 0;
  color: var(--ink);
  font: 10pt/1.62 var(--font-serif);
  orphans: 3;
  widows: 3;
  hyphens: manual;
}

/* ---- cover ---- */
.cover {
  box-sizing: border-box;
  height: 296mm;
  padding: 64mm 2.6cm 0;
  break-after: page;
  page-break-after: always;
  border-top: 14mm solid var(--accent);
}
.cover .cover-body { margin: 0; }
.cover .eyebrow { font: 600 8.5pt var(--font-sans); letter-spacing: 0.16em;
                  text-transform: uppercase; color: var(--accent); margin: 0 0 14pt; }
.cover h1.doc-title { string-set: doctitle content(); font: 700 34pt/1.12 var(--font-sans);
                      letter-spacing: -0.01em; color: var(--ink); margin: 0; border: none; padding: 0;
                      break-before: auto; page-break-before: auto; }
.cover .rule { width: 44mm; height: 3pt; background: var(--accent); margin: 22pt 0 18pt; }
.cover .subtitle { font: 400 14pt/1.35 var(--font-sans); color: var(--ink-soft); margin: 0 0 6pt; }
.cover .date { font: 400 10pt var(--font-sans); color: var(--ink-faint); margin: 0; }

/* ---- headings ---- */
h1, h2, h3, h4 { font-family: var(--font-sans); color: var(--ink); break-after: avoid;
                 page-break-after: avoid; break-inside: avoid; }
h1 { font-size: 22pt; line-height: 1.2; font-weight: 700; margin: 0 0 14pt;
     break-before: page; page-break-before: always;
     padding-bottom: 8pt; border-bottom: 2pt solid var(--accent); }
h2 { font-size: 15pt; line-height: 1.25; font-weight: 700; margin: 30pt 0 9pt; color: var(--accent); }
h3 { font-size: 11.5pt; line-height: 1.3; font-weight: 700; margin: 18pt 0 6pt; }
h4 { font-size: 9.5pt; font-weight: 700; margin: 14pt 0 4pt; letter-spacing: 0.04em;
     text-transform: uppercase; color: var(--ink-soft); }
h1 + h2, h2 + h3 { margin-top: 10pt; }

/* ---- text ---- */
p { margin: 0 0 9pt; }
em { color: var(--ink-soft); }
strong { font-weight: 700; }
a { color: var(--accent); text-decoration: underline; text-decoration-thickness: 0.5pt; }
hr { border: none; border-top: 0.5pt solid var(--rule); margin: 20pt 0 0; }
ul, ol { margin: 0 0 10pt; padding-left: 1.4em; }
li { margin: 0 0 3pt; }
li::marker { color: var(--accent); }

/* ---- callouts ---- */
blockquote { margin: 12pt 0; padding: 9pt 14pt; background: var(--accent-soft);
             border-left: 3pt solid var(--accent); color: var(--ink);
             break-inside: avoid; page-break-inside: avoid; }
blockquote > :last-child { margin-bottom: 0; }

/* ---- code ---- */
code { font: 8.6pt var(--font-mono); background: var(--fill); padding: 0.5pt 3pt; border-radius: 2pt; }
pre { font: 8.2pt/1.5 var(--font-mono); background: var(--fill); border: 0.5pt solid var(--rule);
      border-radius: 3pt; padding: 8pt 10pt; margin: 10pt 0; white-space: pre-wrap;
      overflow-wrap: anywhere; break-inside: avoid; page-break-inside: avoid; }
pre code { background: none; padding: 0; font-size: inherit; }

/* ---- tables ---- */
table { width: 100%; border-collapse: collapse; margin: 12pt 0; font: 8.8pt/1.4 var(--font-sans);
        font-variant-numeric: tabular-nums; }
thead { display: table-header-group; }
tr { break-inside: avoid; page-break-inside: avoid; }
th { background: var(--accent); color: #fff; font-weight: 600; text-align: left;
     padding: 5pt 8pt; border: none; }
td { padding: 4.5pt 8pt; border-bottom: 0.5pt solid var(--rule); vertical-align: top; }
tbody tr:nth-child(even) td { background: var(--fill); }

/* ---- provenance appendix ---- */
.provenance { break-before: page; page-break-before: always; }
.provenance h2 { font-size: 12pt; margin-top: 0; color: var(--ink); border-bottom: 0.5pt solid var(--rule);
                 padding-bottom: 4pt; }
.provenance p { font: 8.4pt/1.5 var(--font-sans); color: var(--ink-soft); }
.provenance table { font-size: 7.6pt; font-family: var(--font-mono); }
.provenance th { background: var(--fill); color: var(--ink); border-bottom: 1pt solid var(--ink-faint); }

/* ---- screen: a centred, readable column ---- */
@media screen {
  body { background: var(--fill); font-size: 17px; }
  .doc { max-width: 44rem; margin: 0 auto; padding: 0 1.25rem 4rem; background: #fff;
         box-shadow: 0 0 0 1px var(--rule); }
  .cover { height: auto; padding: 0 0 2rem; border-top-width: 14px; break-after: auto; }
  .cover .cover-body { position: static; padding-top: 4.5rem; }
  .cover h1.doc-title { font-size: 2.4rem; }
  h1 { break-before: auto; margin-top: 3rem; }
  h2 { margin-top: 2.2rem; }
  .provenance { margin-top: 3rem; }
  pre, code { font-size: 0.85em; }
  table { display: block; overflow-x: auto; }
}
"""


def report_css(accent: str | None = None) -> str:
    """The report theme stylesheet, with the (validated) accent applied."""
    colour = validate_accent(accent) or DEFAULT_ACCENT
    return _REPORT_CSS.replace("@ACCENT_SOFT@", _tint(colour, 0.92)).replace("@ACCENT@", colour)


_LEADING_H1 = re.compile(r"\s*<h1[^>]*>(.*?)</h1>", re.DOTALL)
_TAGS = re.compile(r"<[^>]+>")


def build_report_document(
    html_body: str,
    title: str,
    provenance_html: str,
    *,
    accent: str | None = None,
    subtitle: str | None = None,
    now: datetime | None = None,
) -> str:
    """A complete, self-contained HTML document in the report theme.

    `html_body` is already-sanitised deliverable HTML; its leading `<h1>` (the
    deliverable title) moves onto the cover. `provenance_html` is the appendix
    block, built (and escaped) by the exporter. `subtitle` and the fallback
    title are escaped here.
    """
    css = report_css(accent)
    match = _LEADING_H1.match(html_body)
    if match:
        title_html = match.group(1)
        body = html_body[match.end():]
    else:
        title_html = html.escape(title.replace("-", " ").title())
        body = html_body
    plain_title = _TAGS.sub("", title_html)
    stamp = now or datetime.now(timezone.utc)
    date = f"{stamp.day} {stamp:%B %Y}"
    subtitle_html = f'<p class="subtitle">{html.escape(subtitle)}</p>' if subtitle else ""
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{plain_title}</title>
<style>{css}</style></head><body>
<div class="doc">
<section class="cover"><div class="cover-body">
<p class="eyebrow">Report</p>
<h1 class="doc-title">{title_html}</h1>
<div class="rule"></div>
{subtitle_html}
<p class="date">{date}</p>
</div></section>
<main>
{body}
{provenance_html}
</main>
</div>
</body></html>"""
