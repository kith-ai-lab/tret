"""Sharing a batch, read-only, with another workspace.

A batch belongs to one workspace and every `/api/batches*` route keeps answering 404 to
any other (`core.get_collection`); a share does not change that. It gives the recipient
workspace its own door instead: `/api/shared-batches*` (`tret/api/batch_shares.py`),
backed by this module, which serves a WHITELIST view of the batch: rows, statuses,
evaluation scores and the files written for each row, and nothing about how the batch
was produced (no transcripts, costs, models, settings, findings, lessons or
experiments). The serializers below build each dict field by field; never grow them by
calling `core.collection_out` / `core.target_out` and deleting keys.

A share is addressed to a person (`recipient_email`, `pending` until they accept it
from a workspace) or to a workspace the sharer already belongs to (`active` at once).
Everything unknown, unshared, revoked, declined, pending or owned by an archived
workspace answers the same 404, so a caller learns nothing about what exists.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only

from tret.batches import core, evaluation, scoring
from tret.config import get_settings
from tret.db.models import Batch, BatchShare, BatchTarget, Document, User, Workspace, WorkspaceMember
from tret.services.identity import account_email_is_verified
from tret.storage import get_store, key_for_document

LIVE_STATUSES = ("pending", "active")
NOT_FOUND = "Batch not found"
MAX_EMAIL_CHARS = 320

# What a recipient may see of a row's reported outcome, per batch profile. An outcome is
# whatever the run's profile stored: a disclosure run's `report_collection_outcome`
# fields, or for a prompt batch the final answer plus the finding the run produced (an
# id, a schema and a payload that belong to the owner's review, so they never cross).
# An unknown profile gets the minimum. Add a key here only when it is plain content
# the owner reported about the row, never a cost, model, run or finding reference.
DISCLOSURE_OUTCOME_KEYS = (
    "status", "summary", "reporting_period_label", "period_start", "period_end",
    "publication_date", "publication_basis", "disclosure_type", "framework", "language",
    "fy2025_gap", "substitution_note", "candidate_urls", "blocked_urls", "reported_at",
)
PROMPT_OUTCOME_KEYS = ("answer", "reported_at")
DEFAULT_OUTCOME_KEYS = ("status", "reported_at")
OUTCOME_KEYS_BY_PROFILE = {"disclosure": DISCLOSURE_OUTCOME_KEYS, "prompt": PROMPT_OUTCOME_KEYS}


# Descriptive provenance copied from the source document's meta when present. Never
# `package_finding_id`, `batch_id` or `source_document_id`: those are ids in the owner's
# workspace.
COPIED_META_KEYS = (
    "title", "url", "role", "stem", "package_role", "source_sha256", "retained_sha256", "text_mode",
)
SHARED_SOURCE_KIND = "shared"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return core._iso(value)


def normalise_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _batch_404() -> HTTPException:
    return HTTPException(404, NOT_FOUND)


async def _live_workspace(db: AsyncSession, workspace_id: uuid.UUID | None) -> Workspace | None:
    if workspace_id is None:
        return None
    ws = await db.get(Workspace, workspace_id)
    return ws if ws is not None and ws.archived_at is None else None


def _person(user: User | None) -> dict | None:
    if user is None:
        return None
    out = {"email": user.email}
    if user.display_name:
        out["name"] = user.display_name
    return out


# ══════════════════════════════════════════════════════════════════════════
# owner side
# ══════════════════════════════════════════════════════════════════════════
async def _live_share_for(
    db: AsyncSession, batch_id: uuid.UUID, *, workspace_id: uuid.UUID | None = None, email: str | None = None
) -> BatchShare | None:
    q = select(BatchShare).where(BatchShare.batch_id == batch_id, BatchShare.status.in_(LIVE_STATUSES))
    if workspace_id is not None:
        q = q.where(BatchShare.recipient_workspace_id == workspace_id)
    if email is not None:
        q = q.where(BatchShare.recipient_email == email)
    return (await db.execute(q.order_by(BatchShare.created_at).limit(1))).scalars().first()


async def create_share(
    db: AsyncSession,
    batch: Batch,
    *,
    user: User,
    email: str | None = None,
    workspace_id: uuid.UUID | None = None,
) -> BatchShare:
    """Share `batch` with a person (by email, pending) or with one of the caller's own
    other workspaces (active at once). A repeat request returns the live share."""
    email = normalise_email(email)
    batch_id = batch.id  # read now: a rollback below expires the instance
    if bool(email) == (workspace_id is not None):
        raise HTTPException(422, "Give exactly one of `email` or `workspace_id`")
    if workspace_id is not None:
        if workspace_id == batch.workspace_id:
            raise HTTPException(422, "A batch cannot be shared with the workspace it belongs to")
        from tret.api.workspace import ROLE_RANK

        member = await db.get(WorkspaceMember, (user.id, workspace_id))
        # Handing a batch to a workspace is the same act as accepting one there, so it
        # needs the same role: approver or above. A lesser role looks like no membership.
        if (
            member is None
            or ROLE_RANK.get(member.role, -1) < ROLE_RANK["approver"]
            or await _live_workspace(db, workspace_id) is None
        ):
            raise HTTPException(404, "Workspace not found")
        existing = await _live_share_for(db, batch.id, workspace_id=workspace_id)
        if existing is not None:
            return existing
        share = BatchShare(
            batch_id=batch.id,
            workspace_id=batch.workspace_id,
            recipient_workspace_id=workspace_id,
            status="active",
            created_by=user.id,
            accepted_by=user.id,
            accepted_at=_now(),
        )
    else:
        if "@" not in email or len(email) > MAX_EMAIL_CHARS or any(c.isspace() for c in email):
            raise HTTPException(422, "Enter a valid email address")
        existing = await _live_share_for(db, batch.id, email=email)
        if existing is not None:
            return existing
        share = BatchShare(
            batch_id=batch.id,
            workspace_id=batch.workspace_id,
            recipient_email=email,
            status="pending",
            created_by=user.id,
        )
    db.add(share)
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent request created the same active share first: return that one.
        await db.rollback()
        existing = await _live_share_for(db, batch_id, workspace_id=workspace_id, email=email or None)
        if existing is None:
            raise
        return existing
    return share


async def list_shares(db: AsyncSession, batch: Batch) -> list[BatchShare]:
    rows = (
        await db.execute(
            select(BatchShare)
            .where(BatchShare.batch_id == batch.id)
            .order_by(BatchShare.created_at.desc())
            .execution_options(populate_existing=True)
        )
    ).scalars()
    return list(rows)


async def revoke_share(db: AsyncSession, batch: Batch, share_id: uuid.UUID) -> BatchShare:
    share = await db.get(BatchShare, share_id)
    if share is None or share.batch_id != batch.id:
        raise HTTPException(404, "Share not found")
    if share.status in LIVE_STATUSES:
        share.status = "revoked"
        share.revoked_at = _now()
        await db.commit()
    return share


async def share_out(db: AsyncSession, share: BatchShare) -> dict:
    """The owner's view of a share (`ShareOut`)."""
    recipient = await db.get(Workspace, share.recipient_workspace_id) if share.recipient_workspace_id else None
    creator = await db.get(User, share.created_by) if share.created_by else None
    return {
        "id": str(share.id),
        "batch_id": str(share.batch_id),
        "status": share.status,
        "recipient_email": share.recipient_email,
        "recipient_workspace": {"id": str(recipient.id), "name": recipient.name} if recipient else None,
        "created_by": (
            {"id": str(creator.id), **_person(creator)} if creator is not None else None  # type: ignore[arg-type]
        ),
        "created_at": _iso(share.created_at),
        "accepted_at": _iso(share.accepted_at),
        "revoked_at": _iso(share.revoked_at),
    }


# ══════════════════════════════════════════════════════════════════════════
# recipient side: incoming, accept, decline, remove
# ══════════════════════════════════════════════════════════════════════════
def _email_matches(user: User, share: BatchShare) -> bool:
    """The same rule as `my_pending_invites`: the account's email must count as verified
    and equal the address the share was sent to, ignoring case and spaces."""
    return (
        account_email_is_verified(user)
        and bool(share.recipient_email)
        and normalise_email(user.email) == normalise_email(share.recipient_email)
    )


async def incoming_shares(db: AsyncSession, user: User) -> list[dict]:
    """Pending shares addressed to the caller's own email, as `IncomingShareOut`."""
    if not account_email_is_verified(user):
        return []
    email = normalise_email(user.email)
    rows = (
        await db.execute(
            select(BatchShare)
            .where(BatchShare.status == "pending", BatchShare.recipient_email == email)
            .order_by(BatchShare.created_at.desc())
        )
    ).scalars().all()
    out: list[dict] = []
    for share in rows:
        batch = await db.get(Batch, share.batch_id)
        owner = await _live_workspace(db, share.workspace_id)
        if batch is None or owner is None or batch.workspace_id != share.workspace_id:
            continue
        total = len(await core.load_targets(db, [batch.id]))
        sharer = await db.get(User, share.created_by) if share.created_by else None
        out.append(
            {
                "id": str(share.id),
                "batch": {
                    "id": str(batch.id),
                    "name": batch.name,
                    "profile": batch.profile or "disclosure",
                    "counts": {"total": total},
                },
                "from_workspace": {"name": owner.name},
                "shared_by": _person(sharer),
                "created_at": _iso(share.created_at),
            }
        )
    return out


async def _claim_pending(db: AsyncSession, share: BatchShare, **values: Any) -> bool:
    """Move `share` out of `pending` with one conditional UPDATE, so two requests racing
    on the same share cannot both win. False when someone else got there first."""
    res = await db.execute(
        update(BatchShare)
        .where(BatchShare.id == share.id, BatchShare.status == "pending")
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        await db.rollback()
        return False
    return True


async def accept_share(
    db: AsyncSession, share_id: uuid.UUID, *, user: User, workspace_id: uuid.UUID
) -> tuple[BatchShare, Batch]:
    """Bind a pending share addressed to `user` to the workspace they are acting in."""
    share = await db.get(BatchShare, share_id)
    if share is None or share.status != "pending" or not _email_matches(user, share):
        raise HTTPException(404, "Share not found")
    batch = await db.get(Batch, share.batch_id)
    if batch is None or await _live_workspace(db, share.workspace_id) is None:
        raise HTTPException(404, "Share not found")
    if workspace_id == share.workspace_id:
        raise HTTPException(409, "This batch already belongs to this workspace")
    batch_id = batch.id  # read now: a rollback below expires the instance
    existing = await _live_share_for(db, batch_id, workspace_id=workspace_id)
    if existing is not None:
        # This workspace already has the batch: the pending one is spent.
        if not await _claim_pending(db, share, status="declined"):
            raise HTTPException(404, "Share not found")
        await db.commit()
        await db.refresh(share)
        return existing, batch
    try:
        # The partial unique index (one active share per batch and workspace) is checked
        # when the UPDATE runs, so a concurrent accept of another share of this batch
        # into the same workspace fails here, not at commit.
        claimed = await _claim_pending(
            db, share, recipient_workspace_id=workspace_id, status="active", accepted_by=user.id, accepted_at=_now()
        )
        if claimed:
            await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _live_share_for(db, batch_id, workspace_id=workspace_id)
        if existing is None:
            raise
        await db.execute(
            update(BatchShare)
            .where(BatchShare.id == share_id, BatchShare.status == "pending")
            .values(status="declined")
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return existing, await db.get(Batch, batch_id)  # type: ignore[return-value]
    if not claimed:
        raise HTTPException(404, "Share not found")
    await db.refresh(share)
    return share, batch


async def decline_share(db: AsyncSession, share_id: uuid.UUID, *, user: User) -> None:
    share = await db.get(BatchShare, share_id)
    if share is None or share.status != "pending" or not _email_matches(user, share):
        raise HTTPException(404, "Share not found")
    share.status = "declined"
    await db.commit()


async def remove_share(db: AsyncSession, batch_id: uuid.UUID, *, workspace_id: uuid.UUID) -> None:
    """The recipient workspace drops a shared batch from its list."""
    await get_shared_batch(db, batch_id, workspace_id)
    # Every active row for this (batch, workspace), not just the one that resolved.
    await db.execute(
        update(BatchShare)
        .where(
            BatchShare.batch_id == batch_id,
            BatchShare.recipient_workspace_id == workspace_id,
            BatchShare.status == "active",
        )
        .values(status="revoked", revoked_at=_now())
        .execution_options(synchronize_session=False)
    )
    await db.commit()


async def shared_batches(db: AsyncSession, workspace_id: uuid.UUID) -> list[tuple[BatchShare, Batch]]:
    """The batches shared with `workspace_id` (active shares of live owner workspaces)."""
    if await _live_workspace(db, workspace_id) is None:
        return []
    shares = (
        await db.execute(
            select(BatchShare)
            .where(BatchShare.recipient_workspace_id == workspace_id, BatchShare.status == "active")
            .order_by(BatchShare.accepted_at.desc(), BatchShare.created_at.desc())
        )
    ).scalars().all()
    out: list[tuple[BatchShare, Batch]] = []
    seen: set[uuid.UUID] = set()
    for share in shares:
        batch = await db.get(Batch, share.batch_id)
        if batch is None or batch.id in seen or batch.workspace_id != share.workspace_id:
            continue
        if await _live_workspace(db, share.workspace_id) is None:
            continue
        seen.add(batch.id)
        out.append((share, batch))
    return out


async def get_shared_batch(
    db: AsyncSession, batch_id: uuid.UUID, workspace_id: uuid.UUID
) -> tuple[BatchShare, Batch]:
    """The active share of `batch_id` bound to `workspace_id` and the batch, or the one
    404 every miss gets."""
    share = (
        await db.execute(
            select(BatchShare)
            .where(
                BatchShare.batch_id == batch_id,
                BatchShare.recipient_workspace_id == workspace_id,
                BatchShare.status == "active",
            )
            .order_by(BatchShare.created_at)
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if share is None:
        raise _batch_404()
    batch = await db.get(Batch, batch_id)
    if (
        batch is None
        or batch.workspace_id != share.workspace_id
        or await _live_workspace(db, share.workspace_id) is None
        or await _live_workspace(db, workspace_id) is None
    ):
        raise _batch_404()
    return share, batch


# ══════════════════════════════════════════════════════════════════════════
# the files of a shared batch
# ══════════════════════════════════════════════════════════════════════════
FileEntry = tuple[dict, Document]


# Only the columns a file listing, a download, a zip or a copy needs. Never
# `extracted_text` (up to ~2 MB a document): `copy_files` loads it for the one document
# it is copying.
_FILE_COLUMNS = (
    Document.id, Document.project_id, Document.meta, Document.filename, Document.content_type,
    Document.storage_path, Document.storage_key, Document.sha256, Document.byte_size,
    Document.created_at,
)


async def batch_files(
    db: AsyncSession, batch: Batch, targets: list[BatchTarget]
) -> dict[uuid.UUID, list[FileEntry]]:
    """The live files of each row: `written_files_latest` entries whose document is in
    the batch's own project and has stored bytes. A row with no written files (a batch
    whose files were packaged but never uploaded to a write target) falls back to its
    package documents: the approved packages of its findings. Two queries at most, both
    limited to the columns in `_FILE_COLUMNS`, the second to the rows' own findings."""
    out: dict[uuid.UUID, list[FileEntry]] = {}
    wanted: list[tuple[BatchTarget, dict]] = []
    needs_packages: list[BatchTarget] = []
    for t in targets:
        entries = core.written_files_latest(t)
        for entry in entries:
            wanted.append((t, entry))
        out[t.id] = []
        if not entries and t.finding_ids:
            needs_packages.append(t)
    ids: set[uuid.UUID] = set()
    for _, entry in wanted:
        try:
            ids.add(uuid.UUID(str(entry.get("document_id"))))
        except ValueError:
            continue
    docs: dict[uuid.UUID, Document] = {}
    if ids:
        for d in (
            await db.execute(
                select(Document).options(load_only(*_FILE_COLUMNS)).where(Document.id.in_(list(ids)))
            )
        ).scalars():
            docs[d.id] = d
    for t, entry in wanted:
        try:
            doc = docs.get(uuid.UUID(str(entry.get("document_id"))))
        except ValueError:
            continue
        if doc is None or doc.project_id != batch.project_id or not doc.storage_path:
            continue
        out[t.id].append((entry, doc))
    if needs_packages:
        finding_ids = sorted({str(fid) for t in needs_packages for fid in t.finding_ids or []})
        by_finding: dict[str, list[Document]] = {}
        rows = (
            await db.execute(
                select(Document)
                .options(load_only(*_FILE_COLUMNS))
                .where(
                    Document.project_id == batch.project_id,
                    Document.source_kind == "package",
                    Document.meta["package_finding_id"].as_string().in_(finding_ids),
                )
                .order_by(Document.created_at)
            )
        ).scalars()
        for d in rows:
            fid = (d.meta or {}).get("package_finding_id")
            if fid and d.storage_path:
                by_finding.setdefault(str(fid), []).append(d)
        for t in needs_packages:
            latest: dict[str, FileEntry] = {}
            for fid in t.finding_ids or []:
                for d in by_finding.get(str(fid), []):
                    latest.pop(d.filename, None)
                    latest[d.filename] = (
                        {
                            "filename": d.filename,
                            "sha256": d.sha256,
                            "byte_size": d.byte_size,
                            "uploaded_at": _iso(d.created_at),
                            "document_id": str(d.id),
                        },
                        d,
                    )
            out[t.id] = list(latest.values())
    return out


async def target_files(db: AsyncSession, batch: Batch, target: BatchTarget) -> list[FileEntry]:
    return (await batch_files(db, batch, [target])).get(target.id, [])


def _file_out(entry: dict, doc: Document) -> dict:
    return {
        "filename": entry.get("filename"),
        "sha256": entry.get("sha256") or doc.sha256,
        "byte_size": entry.get("byte_size") if entry.get("byte_size") is not None else doc.byte_size,
        "uploaded_at": entry.get("uploaded_at"),
    }


async def record_documents(db: AsyncSession, batch: Batch) -> list[tuple[str, Document]]:
    """The finalize record files that are stored Documents of this batch. OWNER side
    only (the owner's own archive): the records name costs, models and runs, so nothing
    on the recipient surface uses this."""
    wanted: dict[uuid.UUID, str] = {}
    for f in (batch.finalize or {}).get("files") or []:
        name, doc_id = f.get("name"), f.get("document_id")
        if not name or not doc_id:
            continue
        try:
            wanted[uuid.UUID(str(doc_id))] = str(name)
        except ValueError:
            continue
    if not wanted:
        return []
    docs = {
        d.id: d
        for d in (
            await db.execute(
                select(Document).options(load_only(*_FILE_COLUMNS)).where(Document.id.in_(list(wanted)))
            )
        ).scalars()
    }
    out: list[tuple[str, Document]] = []
    for doc_id, name in wanted.items():
        doc = docs.get(doc_id)
        if doc is None or (doc.meta or {}).get("batch_id") != str(batch.id) or not doc.storage_path:
            continue
        out.append((name, doc))
    return out


# ══════════════════════════════════════════════════════════════════════════
# whitelist serializers
# ══════════════════════════════════════════════════════════════════════════
def _counts(targets: list[BatchTarget]) -> dict:
    by_status = {s: 0 for s in core.TARGET_STATUSES}
    for t in targets:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    return {"total": len(targets), "by_status": by_status}


def _outcome_out(outcome: Any, profile: str | None) -> dict | None:
    """The row's reported outcome, cut down to the profile's key whitelist."""
    if not isinstance(outcome, dict):
        return None
    keys = OUTCOME_KEYS_BY_PROFILE.get(profile or "disclosure", DEFAULT_OUTCOME_KEYS)
    return {k: outcome[k] for k in keys if k in outcome}


def shared_target_out(
    t: BatchTarget, files: list[FileEntry], brief: dict | None, profile: str | None = None
) -> dict:
    """One row as the recipient sees it. Every key is named here; nothing else leaves.
    `profile` is the batch's (None counts as disclosure, like `batch.profile or ...`
    elsewhere); it picks the outcome whitelist."""
    return {
        "id": str(t.id),
        "position": t.position,
        "slug": t.slug,
        "label": t.label,
        "status": t.status,
        "company": t.company,
        "market": t.market,
        "domicile_country": t.domicile_country,
        "sector": t.sector,
        "category": t.category,
        "expected_form": t.expected_form,
        "fiscal_year": t.fiscal_year,
        "task_input": {k: v for k, v in dict(t.task_input or {}).items() if not str(k).startswith("_")},
        "outcome": _outcome_out(t.outcome, profile),
        "evaluation": (
            {"score": brief.get("score"), "status": brief.get("status")} if brief is not None else None
        ),
        "files": [_file_out(entry, doc) for entry, doc in files],
        "started_at": _iso(t.started_at),
        "finished_at": _iso(t.finished_at),
    }


def shared_batch_out(
    share: BatchShare,
    batch: Batch,
    targets: list[BatchTarget],
    files: dict[uuid.UUID, list[FileEntry]],
    overview: dict | None,
    *,
    from_workspace: str,
    shared_by: User | None,
) -> dict:
    """The batch as the recipient sees it (`SharedBatchOut`)."""
    overview = overview or {}
    live = [f for t in targets for f in files.get(t.id, [])]
    return {
        "id": str(batch.id),
        "name": batch.name,
        "run_slug": batch.run_slug,
        "profile": batch.profile or "disclosure",
        "status": batch.status,
        "created_at": _iso(batch.created_at),
        "started_at": _iso(batch.started_at),
        "finalized_at": _iso(batch.finalized_at),
        "counts": _counts(targets),
        "quality_mean": overview.get("quality_mean"),
        "evaluation_status": overview.get("status"),
        "file_count": len(live),
        "byte_size": sum(int(_file_out(e, d)["byte_size"] or 0) for e, d in live),
        "share": {
            "id": str(share.id),
            "from_workspace": {"name": from_workspace},
            "shared_by": _person(shared_by),
            "shared_at": _iso(share.created_at),
            "accepted_at": _iso(share.accepted_at),
        },
    }


async def target_scores(db: AsyncSession, batch: Batch, targets: list[BatchTarget]) -> dict[uuid.UUID, dict]:
    """Per row `{score, status}` from the batch's current evaluation pass: the quality
    score and the row's evaluation status, nothing else (no cost, rationale or run)."""
    current = await scoring.current_pass(db, batch.id)
    if current is None:
        return {}
    by_id = {t.id: t for t in targets}
    out: dict[uuid.UUID, dict] = {}
    for row in await scoring.pass_rows(db, current.id):
        t = by_id.get(row.target_id)
        if t is None:
            continue
        stale = scoring.is_stale(row, t)
        out[t.id] = {
            "score": None if stale or row.quality is None else float(row.quality),
            "status": row.status,
        }
    return out


async def build_shared_batch(
    db: AsyncSession, share: BatchShare, batch: Batch, *, detail: bool
) -> dict:
    targets = await core.load_targets(db, [batch.id])
    files = await batch_files(db, batch, targets)
    overview = (await evaluation.evaluation_overview(db, [batch.id])).get(batch.id)
    owner = await db.get(Workspace, share.workspace_id)
    sharer = await db.get(User, share.created_by) if share.created_by else None
    out = shared_batch_out(
        share, batch, targets, files, overview,
        from_workspace=owner.name if owner is not None else "", shared_by=sharer,
    )
    if detail:
        scores = await target_scores(db, batch, targets)
        out["targets"] = [shared_target_out(t, files.get(t.id, []), scores.get(t.id), batch.profile or "disclosure") for t in targets]
    return out


# ══════════════════════════════════════════════════════════════════════════
# the archive
# ══════════════════════════════════════════════════════════════════════════
async def archive_files(db: AsyncSession, batch: Batch, *, include_records: bool = False) -> list:
    """The zip entries of `batch`: `<target slug>/<file name>` for each live row file and,
    only for the owner's own archive (`include_records`), `records/<name>` for each stored
    record file. The record files carry cost, model and run detail, so the archive a
    recipient gets never has them. Plain data, safe to use after the request's session
    is closed."""
    from tret.batches.files import ArchiveFile, unique_zip_names

    targets = await core.load_targets(db, [batch.id])
    files = await batch_files(db, batch, targets)
    rows: list[tuple[str, str, Any, str | None]] = []
    for t in targets:
        for entry, doc in files.get(t.id, []):
            filename = str(entry.get("filename") or doc.filename).rsplit("/", 1)[-1]
            rows.append((t.slug, filename, doc, t.slug))
    if include_records:
        for name, doc in await record_documents(db, batch):
            rows.append(("records", name, doc, None))
    names = unique_zip_names([(folder, filename) for folder, filename, _, _ in rows])
    return [
        ArchiveFile(path=path, storage_path=str(doc.storage_path), storage_key=doc.storage_key, target_slug=slug)
        for path, (_, _, doc, slug) in zip(names, rows, strict=True)
    ]


# ══════════════════════════════════════════════════════════════════════════
# copying files into the recipient's library
# ══════════════════════════════════════════════════════════════════════════
async def _blob_present(doc: Document) -> bool:
    store = get_store(get_settings())
    try:
        if store.backend == "local":
            return Path(doc.storage_path).is_file()
        if await store.exists(key_for_document(doc)):
            return True
        return Path(doc.storage_path).is_file()
    except (OSError, ValueError):
        return False


async def copy_files(
    db: AsyncSession,
    batch: Batch,
    share: BatchShare,
    *,
    dest_workspace_id: uuid.UUID,
    target_ids: list[uuid.UUID] | None,
    user: User,
) -> dict:
    """Add the shared batch's live row files to the recipient workspace's document
    library. No bytes move: each new `Document` row points at the same
    content-addressed blob (cleanup of a registered blob goes through
    `tret.services.blobs.delete_blob_unless_referenced`, which keeps it while any row
    refers to it; the only direct deletions are of objects a failed request had just
    created, which no copy can point at).

    A copy is `source_kind="shared"`, not `upload`: the text was collected by an agent
    and reviewed in ANOTHER workspace, so the recipient's agents get a banner saying so
    (`engine/tools.py::_document_banner`) rather than an upload's silence. `uploaded_by`
    is still the person who copied it. The owner's `package` / `batch_record` kinds name
    an approval or a run in the OWNER's workspace and `package_finding_id` / `batch_id`
    would make owner-scoped lookups match, so the copy carries only `meta.shared_from`
    and the descriptive keys in `COPIED_META_KEYS`.
    Chunks are not copied: `services/retrieval.py::ensure_chunks` builds them lazily from
    `extracted_text` the first time a document is searched, which is also how every
    other document starts out. Idempotent per source document."""
    from tret.api.workspace import current_project

    project = await current_project(db, dest_workspace_id)
    if project is None:
        raise HTTPException(409, "This workspace has no project to copy files into")
    targets = await core.load_targets(db, [batch.id])
    if target_ids is not None:
        known = {t.id for t in targets}
        if any(tid not in known for tid in target_ids):
            raise HTTPException(404, "Target not found")
        chosen = set(target_ids)
        targets = [t for t in targets if t.id in chosen]
    files = await batch_files(db, batch, targets)
    owner = await db.get(Workspace, share.workspace_id)
    sources = [(t, entry, doc) for t in targets for entry, doc in files.get(t.id, [])]
    already: set[str] = set()
    if sources:
        existing = (
            await db.execute(
                select(Document)
                .options(load_only(Document.id, Document.meta))
                .where(
                    Document.project_id == project.id,
                    Document.sha256.in_(list({d.sha256 for _, _, d in sources})),
                )
            )
        ).scalars()
        for d in existing:
            shared_from = (d.meta or {}).get("shared_from")
            if isinstance(shared_from, dict) and shared_from.get("document_id"):
                already.add(str(shared_from["document_id"]))
    copied: list[dict] = []
    skipped = missing = 0
    now = _now().isoformat()
    for t, entry, doc in sources:
        if str(doc.id) in already:
            skipped += 1
            continue
        if not await _blob_present(doc):
            missing += 1
            continue
        src_meta = doc.meta or {}
        meta: dict[str, Any] = {
            "provenance": "shared_batch",
            "shared_from": {
                "share_id": str(share.id),
                "batch_id": str(batch.id),
                "batch_name": batch.name,
                "workspace_name": owner.name if owner is not None else None,
                "document_id": str(doc.id),
                "target_slug": t.slug,
                "copied_at": now,
            },
        }
        for key in COPIED_META_KEYS:
            if src_meta.get(key) is not None:
                meta[key] = src_meta[key]
        text_row = (
            await db.execute(
                select(Document.extracted_text, Document.extraction_status).where(Document.id == doc.id)
            )
        ).first()
        copy = Document(
            project_id=project.id,
            filename=doc.filename,
            content_type=doc.content_type,
            byte_size=doc.byte_size,
            storage_path=doc.storage_path,
            storage_key=key_for_document(doc),
            extracted_text=text_row[0] if text_row else None,
            extraction_status=text_row[1] if text_row else "pending",
            meta=meta,
            sha256=doc.sha256,
            source_kind=SHARED_SOURCE_KIND,
            uploaded_by=user.id,
        )
        db.add(copy)
        await db.flush()
        already.add(str(doc.id))
        copied.append({"id": str(copy.id), "filename": copy.filename, "target_slug": t.slug})
    await db.commit()
    return {"copied": len(copied), "skipped": skipped, "missing": missing, "documents": copied}

