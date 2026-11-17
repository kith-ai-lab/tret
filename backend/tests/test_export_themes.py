"""Export themes: `plain` is frozen, `report` is the polished document."""
from __future__ import annotations

import hashlib
import io
import re
import sys
import types

import pytest

from tret.services import export as export_module
from tret.services.export import (
    EXPORT_THEMES,
    render_deliverable_bytes,
    render_html,
    render_pdf,
)
from tret.services.html_sanitize import render_markdown

SECTIONS = [
    {
        "section": "governance",
        "status": "approved",
        "model": "m1",
        "doctrine_sha": "abc",
        "energy_wh": 1.234,
        "co2e_g": 0.5,
    }
]

MARKDOWN = """# Climate Disclosure

_Assembled 2026-11-16 10:00 UTC from 1 approved section._

---

## Governance

Board oversight is **quarterly**.

| Metric | FY24 | FY25 |
|---|---:|---:|
| Scope 1 | 1,204 | 1,090 |

> Key point: emissions fell 9%.

    x = 1

<script>alert(1)</script>
"""


def _body() -> str:
    return render_markdown(MARKDOWN)


def test_themes_tuple():
    assert EXPORT_THEMES == ("plain", "report")


def test_plain_pdf_document_is_byte_identical_to_the_original(monkeypatch):
    """Hash of the document string the pre-theme renderer produced for this input."""
    captured = {}

    class FakeHTML:
        def __init__(self, string=None, url_fetcher=None):
            captured["doc"] = string

        def write_pdf(self):
            return b"%PDF"

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    intro = [{**SECTIONS[0], "section": "intro"}]
    render_pdf("<h1>T</h1><p>body</p>", "t", intro, "abcdef0123456789abcdef")
    digest = hashlib.sha256(captured["doc"].encode()).hexdigest()
    assert digest == "e93017000e6b88f3c622d6fe95cb11a5593e6c8d9c1c142da6b10e67b8b1a00a"
    # And explicit "plain" equals the default.
    first = captured["doc"]
    render_pdf("<h1>T</h1><p>body</p>", "t", intro, "abcdef0123456789abcdef", theme="plain")
    assert captured["doc"] == first


def test_plain_html_unchanged():
    body = _body()
    assert render_html(body, "d", SECTIONS, None) == body
    assert render_html(body, "d", SECTIONS, None, theme=None) == body
    assert render_html(body, "d", SECTIONS, None, theme="plain") == body


def test_report_html_has_cover_title_and_subtitle():
    out = render_html(_body(), "d", SECTIONS, None, theme="report", subtitle="Acme & Sons Ltd")
    assert 'class="cover"' in out
    assert '<h1 class="doc-title">Climate Disclosure</h1>' in out
    assert 'class="subtitle">Acme &amp; Sons Ltd<' in out
    assert "<title>Climate Disclosure</title>" in out
    assert 'class="provenance"' in out
    # The title moved to the cover; it is not repeated in the body.
    assert out.count("Climate Disclosure</h1>") == 1


def test_report_without_subtitle_has_no_subtitle_line():
    out = render_html(_body(), "d", SECTIONS, None, theme="report")
    assert 'class="subtitle"' not in out


def test_report_output_is_self_contained():
    out = render_html(_body(), "d", SECTIONS, None, theme="report")
    assert "<script" not in out.replace("&lt;script&gt;", "")
    assert not re.search(r"(?i)(https?:)?//[a-z0-9]", out)
    assert "@import" not in out
    assert "url(" not in out


def test_accent_applied_and_validated():
    out = render_html(_body(), "d", SECTIONS, None, theme="report", accent="#AA3311")
    assert "--accent: #aa3311;" in out


@pytest.mark.parametrize(
    "bad",
    ["red;} body{display:none", "#fff", "#12345g", "123456", "#123456\n", "#123456;}", ""],
)
def test_bad_accent_rejected(bad):
    with pytest.raises(ValueError):
        render_html(_body(), "d", SECTIONS, None, theme="report", accent=bad)
    with pytest.raises(ValueError):
        render_pdf(_body(), "d", SECTIONS, None, theme="report", accent=bad)


def test_unknown_theme_raises():
    with pytest.raises(ValueError, match="theme"):
        render_html(_body(), "d", SECTIONS, None, theme="neon")
    with pytest.raises(ValueError, match="theme"):
        render_pdf(_body(), "d", SECTIONS, None, theme="neon")


async def test_unknown_theme_and_accent_rejected_before_db_work():
    with pytest.raises(ValueError, match="theme"):
        await render_deliverable_bytes(None, None, "x", "html", theme="neon")
    with pytest.raises(ValueError, match="accent"):
        await render_deliverable_bytes(None, None, "x", "pdf", accent="nope")


def test_report_keeps_markdown_sanitised():
    out = render_html(_body(), "d", SECTIONS, None, theme="report")
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<table>" in out and "<blockquote>" in out and "<pre>" in out


def test_report_pdf_goes_through_the_blocking_fetcher(monkeypatch):
    seen = {}

    class FakeHTML:
        def __init__(self, string=None, url_fetcher=None):
            seen["fetcher"] = url_fetcher

        def write_pdf(self):
            return b"%PDF"

    module = types.ModuleType("weasyprint")
    module.HTML = FakeHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    render_pdf(_body(), "d", SECTIONS, None, theme="report")
    assert seen["fetcher"] is export_module.blocked_url_fetcher


def test_report_pdf_renders_multiple_pages():
    pytest.importorskip("weasyprint")
    try:
        pdf = render_pdf(_body(), "d", SECTIONS, "abcdef0123456789", theme="report", subtitle="Acme")
    except export_module.PdfUnavailable:
        pytest.skip("WeasyPrint native libraries unavailable")
    assert pdf.startswith(b"%PDF")
    pypdf = pytest.importorskip("pypdf")
    reader = pypdf.PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) >= 2  # cover + body (+ provenance)
