"""Pack lessons: list, review (approve/reject), retire.

The URL still names a specific `Pack` row (`/packs/{pack_id}/lessons`) — the
one a human is looking at in the Packs view — but every read and write below
resolves that to `pack.slug` and keys off `PackLesson.pack_slug` from there,
never `PackLesson.pack_id`. A lesson is durable across pack version bumps
(see `db/models.py::PackLesson`), so `/packs/{a_specific_version}/lessons`
shows the same rows regardless of which installed version's id is in the URL
— `_pack_in_workspace` only exists to check that id belongs to this
workspace before trusting its slug.

The reviewer identity is stamped from the session — same rule as
`api/findings.py`'s approvals: the API accepts no reviewer field.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import (
    WorkspaceContext,
    current_workspace,
    require_workspace_admin,
    require_workspace_approver,
)
from tret.db.engine import get_db
from tret.db.models import Pack, PackLesson, User
from tret.services import lessons as lessons_service

router = APIRouter(prefix="/api", tags=["lessons"])


def _lesson_out(lesson: PackLesson, *, in_effect: bool) -> dict:
    return {
        "id": str(lesson.id),
        "pack_id": str(lesson.pack_id) if lesson.pack_id else None,
        "pack_slug": lesson.pack_slug,
        "ordinal": lesson.ordinal,
        "status": lesson.status,
        "text": lesson.text,
        "rationale": lesson.rationale,
        # Whether this lesson is actually within `approved_lessons`'s cap —
        # only ever true for a `status == "approved"` row; see
        # `lessons_service.effective_lesson_ids` for the shared cap logic.
        # An approved-but-over-cap lesson is stored and reviewable like any
        # other but is silently never sent to a run's prompt, which is worth
        # surfacing rather than leaving a reviewer to infer it from the count.
        "in_effect": in_effect,
        "proposed_by_run_id": str(lesson.proposed_by_run_id) if lesson.proposed_by_run_id else None,
        "reviewed_by_user_id": str(lesson.reviewed_by_user_id) if lesson.reviewed_by_user_id else None,
        "created_at": lesson.created_at.isoformat() if lesson.created_at else None,
        "reviewed_at": lesson.reviewed_at.isoformat() if lesson.reviewed_at else None,
    }


async def _pack_in_workspace(db: AsyncSession, pack_id: uuid.UUID, workspace_id: uuid.UUID) -> Pack:
    """The pack, only if it belongs to this workspace — else 404, same
    workspace-isolation rule `api/workspace.py::project_in_workspace` states
    for projects: a member of one workspace must not be able to tell that a
    pack (or a lesson under it) in another workspace exists at all."""
    pack = await db.get(Pack, pack_id)
    if pack is None or pack.workspace_id != workspace_id:
        raise HTTPException(404, "Pack not found")
    return pack


@router.get("/packs/{pack_id}/lessons")
async def list_lessons(
    pack_id: uuid.UUID,
    status: str | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Every lesson for this pack's slug in this workspace, any status,
    across every version of the pack ever installed under that slug —
    members see the full history (proposed/approved/rejected/retired); the
    run-time tool (`list_pack_lessons`) is the one that narrows to
    approved-plus-own-pending.
    """
    pack = await _pack_in_workspace(db, pack_id, ctx.id)
    q = (
        select(PackLesson)
        .where(PackLesson.workspace_id == ctx.id, PackLesson.pack_slug == pack.slug)
        .order_by(PackLesson.ordinal)
    )
    if status:
        q = q.where(PackLesson.status == status)
    rows = (await db.execute(q)).scalars().all()
    approved_in_order = [r for r in rows if r.status == "approved"]
    effective_ids = lessons_service.effective_lesson_ids(approved_in_order)
    return [
        _lesson_out(r, in_effect=r.status == "approved" and r.id in effective_ids) for r in rows
    ]


class LessonReviewBody(BaseModel):
    # No reviewer field, deliberately — see module docstring. Keep this in
    # step with the frontend, which sends only {approve}.
    model_config = ConfigDict(extra="forbid")

    approve: bool


@router.post("/packs/{pack_id}/lessons/{lesson_id}/review")
async def review_lesson(
    pack_id: uuid.UUID,
    lesson_id: uuid.UUID,
    body: LessonReviewBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_approver),
    db: AsyncSession = Depends(get_db),
):
    pack = await _pack_in_workspace(db, pack_id, ctx.id)
    lesson = await db.get(PackLesson, lesson_id)
    if lesson is None or lesson.workspace_id != ctx.id or lesson.pack_slug != pack.slug:
        raise HTTPException(404, "Lesson not found")
    try:
        lesson = await lessons_service.review_lesson(
            db, lesson_id, approve=body.approve, user_id=user.id
        )
    except lessons_service.LessonAlreadyDecided as e:
        raise HTTPException(409, f"Lesson is already '{e.status}'") from e
    in_effect = lesson.status == "approved" and lesson.id in lessons_service.effective_lesson_ids(
        await lessons_service.approved_lesson_rows(db, ctx.id, pack.slug)
    )
    return _lesson_out(lesson, in_effect=in_effect)


@router.post("/packs/{pack_id}/lessons/{lesson_id}/retire")
async def retire_lesson(
    pack_id: uuid.UUID,
    lesson_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    pack = await _pack_in_workspace(db, pack_id, ctx.id)
    lesson = await db.get(PackLesson, lesson_id)
    if lesson is None or lesson.workspace_id != ctx.id or lesson.pack_slug != pack.slug:
        raise HTTPException(404, "Lesson not found")
    try:
        lesson = await lessons_service.retire_lesson(db, lesson_id, user_id=user.id)
    except lessons_service.LessonAlreadyDecided as e:
        raise HTTPException(409, f"Only an 'approved' lesson can be retired (this one is '{e.status}')") from e
    # Just retired: never in effect regardless of cap.
    return _lesson_out(lesson, in_effect=False)
