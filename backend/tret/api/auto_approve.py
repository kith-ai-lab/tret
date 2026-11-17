"""`GET`/`PUT /api/workspace/settings/auto-approve` — which agent outputs this
workspace approves automatically (see `tret/services/auto_approve.py`).

Any member can read the effective policy (the chat view uses it); only a
workspace admin or owner can change it, because it decides what reaches
SharePoint and the approved deliverables without a person looking first.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Workspace
from tret.services.auto_approve import (
    AUTO_APPROVE_CATEGORIES,
    AUTO_APPROVE_SETTINGS_KEY,
    resolve_auto_approve,
)

router = APIRouter(prefix="/api", tags=["auto-approve"])

URL = "/workspace/settings/auto-approve"


class AutoApproveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Required, but null is a value: it clears the workspace override.
    categories: list[Literal["draft_section", "connected_write", "findings"]] | None


def _ordered(categories) -> list[str]:
    return [c for c in AUTO_APPROVE_CATEGORIES if c in categories]


def _view(settings: dict | None) -> dict:
    default = get_settings().auto_approve_default or []
    raw = (settings or {}).get(AUTO_APPROVE_SETTINGS_KEY)
    override = _ordered(raw) if isinstance(raw, list) else None
    return {
        "override": override,
        "effective": _ordered(resolve_auto_approve(settings, default)),
        "default": _ordered(default),
    }


@router.get(URL)
async def get_auto_approve(ctx: WorkspaceContext = Depends(current_workspace)):
    return _view(ctx.workspace.settings)


@router.put(URL)
async def put_auto_approve(
    body: AutoApproveBody,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, ctx.id)
    # Reassigned, not mutated in place: SQLAlchemy only detects a change to a
    # JSONB column on assignment.
    settings = dict(workspace.settings or {})
    if body.categories is None:
        settings.pop(AUTO_APPROVE_SETTINGS_KEY, None)
    else:
        settings[AUTO_APPROVE_SETTINGS_KEY] = _ordered(body.categories)
    workspace.settings = settings
    await db.commit()
    return _view(settings)
