"""Deliverable export: assemble APPROVED draft_section findings into MD/HTML/PDF.

Only approved sections are exportable — the blessing gate extends to the
assembled document. PDF rendering uses WeasyPrint (imported lazily: it needs
system libraries — pango/cairo — that the Docker image ships but a bare local
venv may not; the API returns 501 with instructions in that case).
"""
from __future__ import annotations

import html
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings

from tret.db.models import Finding, Run
from tret.services.emissions import energy_wh_field
from tret.services.html_sanitize import render_markdown


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

    runs = await _runs_behind(db, sections.values())
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
        run = runs.get(f.run_id)
        meta.append(
            {
                "section": slug,
                "finding_id": str(f.id),
                "status": f.status,
                "model": f.provenance.get("model"),
                "doctrine_sha": (f.provenance.get("doctrine_sha") or "")[:12],
                "run_id": str(f.run_id) if f.run_id else None,
                # Estimated compute footprint of the run that drafted this
                # section. None where the run predates eco accounting.
                "energy_wh": energy_wh_field(run.energy_wh) if run is not None else None,
                "co2e_g": (run.energy_accounting or {}).get("co2e_g") if run is not None else None,
            }
        )
    footprint = _deliverable_footprint(runs.values())
    if footprint["energy_wh"] is not None:
        # Reported in the deliverable itself, not just the audit view: a climate
        # document that hides its own compute footprint is making an argument it
        # would not accept from anyone else. Estimated, and labelled as such.
        parts.append(
            f"\n---\n\n_Estimated compute footprint of the {footprint['runs']} run(s) behind "
            f"this document: {footprint['energy_wh']:.3g} Wh, ≈{footprint['co2e_g']:.3g} gCO2e "
            f"at {footprint['grid_co2e_g_per_kwh']:.0f} gCO2e/kWh. Heuristic estimate from token "
            "counts and model energy class — not a measurement._"
        )
    markdown_text = "\n".join(parts)
    # Section bodies are model-authored and derived from third-party uploads, so
    # rendering goes through the sanitizing renderer: raw HTML is escaped to
    # visible text and unsafe URL schemes are dropped. See services/html_sanitize.
    html_out = render_markdown(markdown_text)
    return {"markdown": markdown_text, "html": html_out, "sections": meta, "energy": footprint}


async def _runs_behind(db: AsyncSession, findings) -> dict:
    """The runs that produced these findings, keyed by run id."""
    run_ids = {f.run_id for f in findings if f.run_id}
    if not run_ids:
        return {}
    rows = (await db.execute(select(Run).where(Run.id.in_(run_ids)))).scalars().all()
    return {r.id: r for r in rows}


def _deliverable_footprint(runs) -> dict:
    """Estimated energy/carbon for a whole deliverable.

    Summed over *distinct runs*, so two sections drafted by one run are not
    counted twice. Runs with no estimate contribute nothing and are counted as
    missing rather than as zero.
    """
    total_wh = 0.0
    total_co2e = 0.0
    grid = float(get_settings().grid_co2e_g_per_kwh)
    counted = 0
    for run in runs:
        if run.energy_wh is None:
            continue
        counted += 1
        total_wh += float(run.energy_wh)
        accounting = run.energy_accounting or {}
        total_co2e += float(accounting.get("co2e_g") or 0.0)
        if accounting.get("grid_co2e_g_per_kwh"):
            # The intensity actually used at run time wins over today's setting.
            grid = float(accounting["grid_co2e_g_per_kwh"])
    return {
        "estimated": True,
        "runs": counted,
        "runs_without_estimate": sum(1 for r in runs if r.energy_wh is None),
        "energy_wh": round(total_wh, 6) if counted else None,
        "co2e_g": round(total_co2e, 6) if counted else None,
        "grid_co2e_g_per_kwh": grid,
    }


class PdfUnavailable(Exception):
    """WeasyPrint's native libraries are missing in this environment."""


class ExternalResourceBlocked(Exception):
    """The PDF renderer tried to fetch a resource tret did not supply."""


def blocked_url_fetcher(url: str, timeout: int = 10, ssl_context=None):
    """WeasyPrint's resource loader, wired shut.

    WeasyPrint's default fetcher resolves absolute `http(s)://` and `file://`
    URLs for `<img>`, `<link>` and CSS `@import`. The document being rendered is
    model-authored, so that default is an SSRF and local-file-read primitive
    reachable by anything that can get text into a drafted section — including a
    prompt injection carried in an uploaded document.

    Nothing tret renders needs a fetch: the only stylesheet is inlined below.
    `data:` URIs are resolved locally by WeasyPrint (no network, no filesystem),
    so they remain available for anything tret embeds itself; every other
    scheme raises.
    """
    if url.startswith("data:"):
        from weasyprint.urls import default_url_fetcher

        return default_url_fetcher(url, timeout=timeout, ssl_context=ssl_context)
    raise ExternalResourceBlocked(url)


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


def _energy_cell(section: dict) -> str:
    """One provenance row's estimated footprint, or an em dash if unknown."""
    wh = section.get("energy_wh")
    if wh is None:
        return "—"
    co2e = section.get("co2e_g")
    carbon = f" · {co2e:.3g} gCO2e" if co2e is not None else ""
    return f"~{wh:.3g} Wh{carbon}"


def render_pdf(html_body: str, title: str, sections: list[dict], doctrine_sha: str | None) -> bytes:
    """Render the assembled deliverable HTML to a styled PDF with a
    provenance appendix. Raises PdfUnavailable if WeasyPrint can't load."""
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as e:
        raise PdfUnavailable(str(e)) from e

    # Section slugs, statuses and model ids are interpolated into HTML, and the
    # slug is model-chosen — escape every cell rather than trusting the source.
    def esc(value) -> str:
        return html.escape(str(value if value not in (None, "") else "—"))

    provenance_rows = "".join(
        f"<tr><td>{esc(s['section'])}</td><td>{esc(s['status'])}</td>"
        f"<td>{esc(s.get('model'))}</td><td>{esc(s.get('doctrine_sha'))}</td>"
        f"<td>{esc(_energy_cell(s))}</td></tr>"
        for s in sections
    )
    document = f"""<html><head><meta charset="utf-8">
<style>{_PDF_CSS}</style></head><body>
{html_body}
<div class="provenance">
<h2>Provenance</h2>
<p class="meta">Each section below was drafted under the doctrine version shown,
validated, and approved by a named reviewer before inclusion in this document.
{f"Pack doctrine hash: <code>{html.escape(doctrine_sha[:16])}</code>." if doctrine_sha else ""}
The energy column is an <em>estimate</em> of the compute drawn by the run that
drafted each section — derived from token counts and the model's energy class,
never measured, and shared by sections drafted in the same run.</p>
<table>
<tr><th>Section</th><th>Status</th><th>Model</th><th>Doctrine</th>
<th>Energy (est.)</th></tr>
{provenance_rows}
</table>
</div>
</body></html>"""
    # url_fetcher is the SSRF/local-file gate: see blocked_url_fetcher.
    return HTML(string=document, url_fetcher=blocked_url_fetcher).write_pdf()
