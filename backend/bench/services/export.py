"""Deliverable export: assemble APPROVED draft_section findings into MD/HTML.

Only approved sections are exportable — the blessing gate extends to the
assembled document. PDF export is a v2 item (weasyprint).
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
        parts.append(f"\n---\n\n## {slug.replace('_', ' ').title()}\n")
        parts.append(f.payload.get("markdown", ""))
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
