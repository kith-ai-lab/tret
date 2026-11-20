"""The audit sidecar written next to every file a run saves to OneDrive or
SharePoint (R24): `<filename>.audit.json`, schema `tret.deliverable_audit/v1`.

It answers "where did this file come from?" without opening Tret: who produced
it, which run, which models, which files and datasets it read, which methods and
unvetted calculations it ran (and whether each was vetted), what the grounding
check said, the guardrails in force, and the run tree's tokens, cost, energy and
CO2e. Nothing in it is a secret: ids, hashes, names and figures only, never a
token, a credential, or a prompt/transcript. Figures are estimates read from the
run records as they stood when the file was saved (a run that is still going when
an automatic approval saves its file reports `run_status: "running"`).
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import AnalysisRun, Document, Finding, MethodRun, Run, User

AUDIT_SCHEMA = "tret.deliverable_audit/v1"
AUDIT_SUFFIX = ".audit.json"
# Bound on listed files/datasets so one run that read thousands cannot make the sidecar huge.
_MAX_LISTED = 200


def audit_filename(saved_path: str) -> str:
    """The sidecar's path, beside `saved_path` (a path relative to the tret/ folder)."""
    return f"{saved_path}{AUDIT_SUFFIX}"


def _num(value) -> float | None:
    if value is None:
        return None
    return round(float(value), 6)


async def _run_tree(db: AsyncSession, run: Run) -> list[Run]:
    root_id = run.root_run_id or run.id
    rows = (
        await db.execute(select(Run).where((Run.id == root_id) | (Run.root_run_id == root_id)))
    ).scalars().all()
    if not any(r.id == run.id for r in rows):
        rows = [*rows, run]
    return sorted(rows, key=lambda r: (r.created_at or datetime.min.replace(tzinfo=timezone.utc)))


def _models(tree: list[Run]) -> list[str]:
    seen: list[str] = []
    for r in tree:
        for name in [r.model_used, *[seg.get("model") for seg in (r.model_timeline or []) if isinstance(seg, dict)]]:
            if name and name not in seen:
                seen.append(name)
    return seen


def _co2e_g(tree: list[Run]) -> float | None:
    parts = [
        (r.energy_accounting or {}).get("co2e_g")
        for r in tree
        if isinstance(r.energy_accounting, dict)
    ]
    nums = [float(p) for p in parts if isinstance(p, (int, float))]
    return round(sum(nums), 6) if nums else None


async def build_audit(
    db: AsyncSession,
    *,
    finding: Finding,
    saved_path: str,
    filename: str,
    sha256: str,
    byte_size: int,
    actor_user_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None = None,
) -> dict:
    """The audit document for the file a `connected_write` finding just saved."""
    run = await db.get(Run, finding.run_id)
    tree = await _run_tree(db, run) if run is not None else []
    tree_ids = [r.id for r in tree]
    root = next((r for r in tree if r.id == (run.root_run_id or run.id)), run) if run is not None else None

    actor = await db.get(User, actor_user_id) if actor_user_id else None
    creator = await db.get(User, run.created_by) if run is not None and run.created_by else actor
    who = creator or actor

    # Files read: documents attached to / materialized by any run in the tree, the
    # finding's own snapshot, and the saved file's source document.
    doc_ids: list[uuid.UUID] = []

    def _add(value) -> None:
        try:
            d = value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
        except (ValueError, TypeError):
            return
        if d not in doc_ids:
            doc_ids.append(d)

    for r in tree:
        for d in r.document_ids or []:
            _add(d)
    for d in (finding.provenance or {}).get("document_ids") or []:
        _add(d)
    source = (finding.payload or {}).get("source") or {}
    source_doc_id = source.get("document_id") if source.get("kind") == "document" else None
    files_read = []
    if doc_ids:
        docs = (await db.execute(select(Document).where(Document.id.in_(doc_ids)))).scalars().all()
        for d in docs[:_MAX_LISTED]:
            if source_doc_id and str(d.id) == str(source_doc_id):
                continue  # the file being saved, not a file it read
            meta = d.meta or {}
            files_read.append(
                {
                    "document_id": str(d.id),
                    "name": d.filename,
                    "source_kind": d.source_kind,
                    "path": meta.get("path"),
                    "web_url": meta.get("web_url"),
                    "sha256": d.sha256,
                }
            )

    datasets: dict[str, int] = {}
    for item in (finding.provenance or {}).get("retrieved_values") or []:
        name = item.get("dataset") if isinstance(item, dict) else None
        if name and not str(name).startswith("method/"):
            datasets[name] = datasets.get(name, 0) + 1

    method_runs: list[dict] = []
    analysis_runs: list[dict] = []
    if tree_ids:
        for m in (await db.execute(select(MethodRun).where(MethodRun.run_id.in_(tree_ids)))).scalars().all():
            method_runs.append(
                {
                    "id": str(m.id),
                    "method": m.method_slug,
                    "code_sha": m.code_sha,
                    "output_hash": m.output_hash,
                    "rows": m.row_count,
                    "vetted": True,
                }
            )
        for a in (await db.execute(select(AnalysisRun).where(AnalysisRun.run_id.in_(tree_ids)))).scalars().all():
            analysis_runs.append(
                {
                    "id": str(a.id),
                    "purpose": a.purpose,
                    "code_sha": a.code_sha,
                    "output_hash": a.output_hash,
                    "rows": a.row_count,
                    "status": a.status,
                    "vetted": False,
                }
            )

    grounding = None
    if root is not None and isinstance(root.grounding, dict):
        g = root.grounding
        grounding = {k: g.get(k) for k in ("status", "checked", "attempts", "unsupported", "unvetted") if k in g}
    guardrails = None
    if root is not None and isinstance(root.guardrails, dict):
        guardrails = root.guardrails.get("settings")

    cost = sum((Decimal(r.cost_usd or 0) for r in tree), Decimal(0))
    reported = [Decimal(r.reported_cost_usd) for r in tree if r.reported_cost_usd is not None]
    energy = [Decimal(r.energy_wh) for r in tree if r.energy_wh is not None]
    return {
        "schema": AUDIT_SCHEMA,
        "file": {"name": filename, "path": saved_path, "sha256": sha256, "bytes": byte_size},
        "produced_by": {
            "user_id": str(who.id) if who is not None else None,
            "display_name": who.display_name if who is not None else None,
        },
        "saved_by": {
            "user_id": str(actor.id) if actor is not None else None,
            "display_name": actor.display_name if actor is not None else None,
        },
        "produced_at": datetime.now(timezone.utc).isoformat(),
        "run_id": str(finding.run_id),
        "root_run_id": str(run.root_run_id or run.id) if run is not None else None,
        "conversation_id": str(conversation_id or (run.conversation_id if run is not None else None) or "") or None,
        "run_status": root.status if root is not None else None,
        "models": _models(tree),
        "files_read": files_read,
        "datasets_looked_up": [{"dataset": k, "values_retrieved": v} for k, v in list(datasets.items())[:_MAX_LISTED]],
        "method_runs": method_runs,
        "analysis_runs": analysis_runs,
        "grounding": grounding,
        "guardrails": guardrails,
        "run_tree": {
            "runs": len(tree),
            "input_tokens": sum(int(r.input_tokens or 0) for r in tree),
            "output_tokens": sum(int(r.output_tokens or 0) for r in tree),
            "cost_usd": _num(cost),
            "reported_cost_usd": _num(sum(reported, Decimal(0))) if reported else None,
            "energy_wh": _num(sum(energy, Decimal(0))) if energy else None,
            "co2e_g": _co2e_g(tree),
            "note": "Estimates from the run records at the moment the file was saved; see docs/eco-accounting.md.",
        },
    }


def audit_bytes(audit: dict) -> bytes:
    return (json.dumps(audit, indent=2, sort_keys=False, default=str) + "\n").encode("utf-8")
