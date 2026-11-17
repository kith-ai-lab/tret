"""`PUT /api/workspace/settings/edition` — set or clear a workspace's UI
edition override (see `tret/services/edition.py`).

Instance admins only (`require_admin`, the global role), since an edition is
a deployment-shaping choice rather than something a workspace admin picks for
themselves. The edition only shapes which screens the web UI shows.
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import require_admin
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import User, Workspace
from tret.services.edition import EDITION_SETTINGS_KEY, resolve_edition

router = APIRouter(prefix="/api", tags=["edition"])


class EditionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Required, but null is a value: it clears the workspace override.
    edition: Literal["standard", "cohort"] | None


@router.put("/workspace/settings/edition")
async def put_edition(
    body: EditionBody,
    _admin: User = Depends(require_admin),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    workspace = await db.get(Workspace, ctx.id)
    # Reassigned, not mutated in place: SQLAlchemy only detects a change to a
    # JSONB column on assignment.
    settings = dict(workspace.settings or {})
    if body.edition is None:
        settings.pop(EDITION_SETTINGS_KEY, None)
    else:
        settings[EDITION_SETTINGS_KEY] = body.edition
    workspace.settings = settings
    await db.commit()
    return {
        "override": body.edition,
        "edition": resolve_edition(settings, get_settings().edition),
    }
