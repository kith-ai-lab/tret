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

from tret.db.models import Finding, Run
from tret.services.emissions import energy_wh_field, grid_comparison_signature
from tret.services.emissions_validation import number
from tret.services.html_sanitize import render_markdown


def strip_draft_status_banner(markdown: str | None) -> str | None:
    """Drop every "Draft status" paragraph — bold (`**Draft status:** ...`)
    or plain — wherever it sits in the section: leading, or under the
    section's own heading, which is where models most often put it.

    Models drafting a `draft_section` finding sometimes open with exactly this
    boilerplate ("This section is a draft awaiting review by a named human
    reviewer..."). It is true while the finding is a draft and stale the
    moment it is approved, so it must never ship in an export or an
    approved-only render. Called both at approval time (`api/findings.py`)
    and again here at assembly, defensively, for any section approved before
    that stripping existed.

    A banner glued to body text by a single newline loses only its first
    line. The function never returns an empty body: if stripping would leave
    nothing, the markdown comes back unchanged.
    """
    if not markdown:
        return markdown
    paragraphs = markdown.split("\n\n")
    kept: list[str] = []
    changed = False
    for para in paragraphs:
        probe = para.strip().lstrip("*").strip().lower()
        if not probe.startswith("draft status"):
            kept.append(para)
            continue
        changed = True
        first_line, nl, tail = para.strip().partition("\n")
        if nl and tail.strip():
            kept.append(tail)
    if not changed:
        return markdown
    result = "\n\n".join(kept).strip("\n")
    return result if result.strip() else markdown


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
        body = strip_draft_status_banner(f.payload.get("markdown", "") or "").lstrip()
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
    if footprint["runs"]:
        # Reported in the deliverable itself, not just the audit view: a climate
        # document that hides its own compute footprint is making an argument it
        # would not accept from anyone else. Estimated, and labelled as such.
        #
        # `energy_wh` is None only when the runs behind it genuinely disagree on
        # energy boundary (F5) — never just because a boundary is missing. A
        # missing boundary means the run predates boundary labelling, which is
        # itself a known, nameable state (`energy_boundary_legacy_qualifier`),
        # not an unknown one; the paragraph must never be dropped for it.
        carbon = (f", ≈{footprint['co2e_g']:.3g} gCO2e" if footprint["co2e_g"] is not None
                  else "; combined carbon withheld because coverage or factor bases differ")
        description = ("Observed energy with estimated carbon factors." if footprint["energy_source"] == "measured"
                       else "Heuristic estimate or mixed measurement coverage; not a measurement of the whole task.")
        if footprint["energy_wh"] is not None:
            energy_clause = f"{footprint['energy_wh']:.3g} Wh{carbon}"
        else:
            subtotals = "; ".join(
                f"{s['boundary']}: {s['energy_wh']:.3g} Wh across {s['runs']} run(s)"
                for s in footprint["energy_boundary_subtotals"]
            )
            energy_clause = (
                "combined energy figure withheld because these drafting runs record "
                f"different energy boundaries ({subtotals})"
            )
        boundary_note = (
            " Energy boundary: legacy, unresolved (runs recorded before boundary "
            "labelling); figures summed as recorded."
            if footprint.get("energy_boundary_legacy_qualifier") else ""
        )
        parts.append(
            f"\n---\n\n_Estimated compute footprint of the {footprint['runs']} drafting run(s), as recorded: "
            f"{energy_clause}. {description} "
            "Covers selected drafting runs; other attempts, tools and supporting activity may be excluded."
            f"{boundary_note}_"
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

    Energy is withheld (`energy_wh: None`) only when two or more runs genuinely
    disagree on energy boundary — never merely because a boundary predates
    labelling (F5). A legacy-only window still sums, with
    `energy_boundary_legacy_qualifier: True` flagging the caveat the caller
    must print; a real mix withholds the sum and reports
    `energy_boundary_subtotals` instead.
    """
    runs = list({str(r.id): r for r in runs}.values())
    energy = [float(number(r.energy_wh, "energy_wh")) for r in runs if r.energy_wh is not None]
    accounts = [r.energy_accounting or {} for r in runs if r.energy_wh is not None]
    carbon = [float(number(a["co2e_g"], "co2e_g")) for a in accounts
              if a.get("co2e_g") is not None]

    # Carbon is summable when every accounted run priced electricity the same
    # way: one GHG basis, and — via `grid_comparison_signature` (the same
    # comparison `combine_accountings` and the analytics rollup use) — one
    # grid-factor method or identical exact factor. That signature already
    # treats missing provenance (source/label/layer) as *not* part of
    # identity and collapses every "we don't know" spelling in the metadata
    # to one sentinel, so legacy rows priced at the identical factor compare
    # equal even though they predate boundary/dataset-version labelling.
    # `grid_co2e_source`/`label`/`layer` are provenance, not identity, so a
    # basis check plus the signature is sufficient here too — no separate
    # "all metadata known" gate.
    single_run = len(energy) == 1
    bases = {a.get("grid_co2e_basis") for a in accounts}
    signatures = {grid_comparison_signature(a) for a in accounts}
    known_compatible = len(bases) == 1 and len(signatures) == 1
    grids = {
        float(number(a["grid_co2e_g_per_kwh"], "grid_co2e_g_per_kwh"))
        if a.get("grid_co2e_g_per_kwh") is not None else None
        for a in accounts
    }
    sources = {a.get("energy_source", "unknown") for a in accounts}
    boundaries = {a.get("energy_boundary", "unknown") for a in accounts}

    # ── F5: a missing `energy_boundary` is a *legacy* row, not an unknown one ──
    # A pre-batch run has no `energy_boundary` key at all (it predates boundary
    # labelling entirely); a v2 run that genuinely couldn't resolve one records
    # `"unknown"` alongside a `method_id`. Only the former group's energy may be
    # summed with a caveat rather than withheld outright — the whole point of
    # this classification is that "legacy" and "genuinely unresolved" mean
    # different things and must not be folded into the same withholding rule
    # that a real cross-boundary mix triggers below.
    def boundary_class(a: dict) -> str:
        if "energy_boundary" not in a:
            return "legacy_unresolved"
        boundary = a.get("energy_boundary")
        if boundary in (None, "unknown") and not a.get("method_id"):
            return "legacy_unresolved"
        return boundary or "unknown"

    boundary_classes = [boundary_class(a) for a in accounts]
    per_boundary_energy: dict[str, float] = {}
    per_boundary_runs: dict[str, int] = {}
    for wh, cls in zip(energy, boundary_classes):
        per_boundary_energy[cls] = per_boundary_energy.get(cls, 0.0) + wh
        per_boundary_runs[cls] = per_boundary_runs.get(cls, 0) + 1
    boundary_subtotals = [
        {"boundary": cls, "energy_wh": round(total, 6), "runs": per_boundary_runs[cls]}
        for cls, total in sorted(per_boundary_energy.items())
    ]
    distinct_classes = set(boundary_classes)
    # A single run is summed regardless of its own boundary (nothing to mix
    # with); two-or-more legacy rows sum with the qualifier sentence; two or
    # more rows spanning distinct boundary classes withhold the combined
    # figure and publish per-boundary subtotals instead.
    energy_summable = bool(energy) and (single_run or len(distinct_classes) == 1)
    legacy_qualifier = (
        bool(energy) and not single_run and distinct_classes == {"legacy_unresolved"}
    )

    return {
        "estimated": True,
        "runs": len(energy),
        "runs_without_estimate": sum(1 for r in runs if r.energy_wh is None),
        "runs_without_carbon": len(runs) - len(carbon),
        "energy_wh": round(sum(energy), 6) if energy_summable else None,
        "co2e_g": round(sum(carbon), 6) if carbon and len(carbon) == len(runs) and known_compatible else None,
        "grid_co2e_g_per_kwh": next(iter(grids)) if len(grids) == 1 else None,
        "energy_source": next(iter(sources)) if len(sources) == 1 else "mixed",
        "energy_boundary": next(iter(boundaries)) if len(boundaries) == 1 else "mixed",
        "coverage": "selected_drafting_runs_only",
        "carbon_compatible": known_compatible,
        # F5 additions — additive only, the shape above is unchanged.
        "energy_boundary_legacy_qualifier": legacy_qualifier,
        "energy_boundary_subtotals": boundary_subtotals if (energy and not single_run) else [],
    }


class DeliverableEmpty(Exception):
    """No (approved, or approved+draft) sections exist for this deliverable."""


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


async def render_deliverable_bytes(
    db: AsyncSession,
    project_id: uuid.UUID,
    deliverable_slug: str,
    format: str,
    include_draft: bool = False,
) -> tuple[bytes, str]:
    """Assemble a deliverable and render it to `(bytes, content_type)` in
    `format` ("markdown"|"html"|"pdf") — the one rendering path shared by the
    deliverable export endpoint and anything that uploads a deliverable
    elsewhere (`api/findings.py`'s approved-`connected_write` upload and its
    `POST /api/deliverables/{slug}/publish` route), so the PDF
    doctrine-hash-dedup logic and the "nothing to render" check live in
    exactly one place.

    Raises `DeliverableEmpty` when the deliverable has no (approved, or
    approved+draft) sections, and `PdfUnavailable` (unchanged) when
    `format="pdf"` and WeasyPrint's native libraries are missing.
    """
    if format not in ("markdown", "html", "pdf"):
        raise ValueError(f"format must be markdown|html|pdf, got {format!r}")
    result = await assemble_deliverable(db, project_id, deliverable_slug, include_draft)
    if not result["sections"]:
        raise DeliverableEmpty(deliverable_slug)
    if format == "html":
        return result["html"].encode("utf-8"), "text/html"
    if format == "pdf":
        shas = {s.get("doctrine_sha") for s in result["sections"] if s.get("doctrine_sha")}
        pdf = render_pdf(
            result["html"],
            deliverable_slug,
            result["sections"],
            shas.pop() if len(shas) == 1 else None,
        )
        return pdf, "application/pdf"
    return result["markdown"].encode("utf-8"), "text/markdown"
