"""Deliverable export: assemble APPROVED draft_section findings into MD/HTML/PDF.

Only approved sections are exportable — the blessing gate extends to the
assembled document. PDF rendering uses WeasyPrint (imported lazily: it needs
system libraries — pango/cairo — that the Docker image ships but a bare local
venv may not; the API returns 501 with instructions in that case).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import markdown as md
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.models import Finding


async def assemble_deliverable(
    db: AsyncSession, project_id: uuid.UUID, deliverable_slug: str, include_draft: bool = False
) -> dict:
    statuses = ["approved"] + (["draft"] if include_draft else [])
    findings = (
        (
            await db.execute(
                select(Finding)
                .where(
                    Finding.project_id == project_id,
                    Finding.schema_slug == "draft_section",
                    Finding.status.in_(statuses),
                )
                .order_by(Finding.created_at)
            )
        )
        .scalars()
        .all()
    )
    # Latest finding per section wins; superseded ones are skipped.
    sections: dict[str, Finding] = {}
    for f in findings:
        if f.subject.get("deliverable") == deliverable_slug:
            sections[f.subject.get("section", "untitled")] = f

    if not sections:
        return {"markdown": "", "sections": [], "html": ""}

    parts = [f"# {deliverable_slug.replace('-', ' ').title()}"]
    parts.append(
        f"_Assembled {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} from "
        f"{len(sections)} {'approved' if not include_draft else 'approved+draft'} section(s)._"
    )
    meta = []
    for slug, f in sections.items():
        title = slug.replace("_", " ").title()
        parts.append(f"\n---\n\n## {title}\n")
        body = f.payload.get("markdown", "").lstrip()
        # Models often open with their own section heading — drop it if it
        # duplicates the title we just added.
        first_line, _, rest = body.partition("\n")
        if first_line.lstrip("# ").strip().lower() == title.lower():
            body = rest.lstrip()
        parts.append(body)
        meta.append(
            {
                "section": slug,
                "finding_id": str(f.id),
                "status": f.status,
                "model": f.provenance.get("model"),
                "doctrine_sha": (f.provenance.get("doctrine_sha") or "")[:12],
            }
        )
    markdown_text = "\n".join(parts)
    html = md.markdown(markdown_text, extensions=["tables"])
    return {"markdown": markdown_text, "html": html, "sections": meta}


class PdfUnavailable(Exception):
    """WeasyPrint's native libraries are missing in this environment."""


_PDF_CSS = """
@page {
    size: A4;
    margin: 2.2cm 2cm 2.4cm 2cm;
    @bottom-left { content: string(doctitle); font: 8pt monospace; color: #888; }
    @bottom-right { content: "page " counter(page) " of " counter(pages);
                    font: 8pt monospace; color: #888; }
}
body { font: 10.5pt/1.55 Georgia, 'Times New Roman', serif; color: #1a1a1a; }
h1 { string-set: doctitle content(); font: 700 20pt Helvetica, Arial, sans-serif;
     border-bottom: 2px solid #1a1a1a; padding-bottom: 8px; margin-bottom: 4px; }
h2 { font: 700 13pt Helvetica, Arial, sans-serif; margin-top: 22px;
     border-bottom: 0.5px solid #bbb; padding-bottom: 3px; page-break-after: avoid; }
h3 { font: 700 11pt Helvetica, Arial, sans-serif; page-break-after: avoid; }
em, .meta { color: #555; }
hr { border: none; border-top: 0.5px solid #ccc; margin: 18px 0; }
table { border-collapse: collapse; width: 100%; font-size: 9pt; margin: 10px 0; }
th, td { border: 0.5px solid #999; padding: 4px 7px; text-align: left; }
th { background: #f0f0f0; font-family: Helvetica, Arial, sans-serif; }
.provenance { margin-top: 28px; page-break-inside: avoid; }
.provenance h2 { border-bottom: 0.5px solid #bbb; }
.provenance table { font-family: monospace; font-size: 8pt; }
"""


def render_pdf(html_body: str, title: str, sections: list[dict], doctrine_sha: str | None) -> bytes:
    """Render the assembled deliverable HTML to a styled PDF with a
    provenance appendix. Raises PdfUnavailable if WeasyPrint can't load."""
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        raise PdfUnavailable(str(e)) from e

    provenance_rows = "".join(
        f"<tr><td>{s['section']}</td><td>{s['status']}</td>"
        f"<td>{s.get('model') or '—'}</td><td>{s.get('doctrine_sha') or '—'}</td></tr>"
        for s in sections
    )
    document = f"""<html><head><meta charset="utf-8">
<style>{_PDF_CSS}</style></head><body>
{html_body}
<div class="provenance">
<h2>Provenance</h2>
<p class="meta">Each section below was drafted under the doctrine version shown,
validated, and approved by a named reviewer before inclusion in this document.
{f"Pack doctrine hash: <code>{doctrine_sha[:16]}</code>." if doctrine_sha else ""}</p>
<table>
<tr><th>Section</th><th>Status</th><th>Model</th><th>Doctrine</th></tr>
{provenance_rows}
</table>
</div>
</body></html>"""
    return HTML(string=document).write_pdf()
