"""Auto-approval policy: which agent outputs are approved at creation instead
of waiting in the Approvals queue.

Three categories can be switched on, per deployment (`TRET_AUTO_APPROVE_DEFAULT`)
and per workspace (`Workspace.settings["auto_approve"]`, a list that wins over
the default when present, `[]` meaning "off here"). A `package_map` is never
auto-approved: approving one starts packaging, which is always a human call.

An automatic approval is a real `Approval` row (so the banner strip, the
SharePoint upload and the audit trail all happen exactly as for a person) but
with purpose "auto": it is stamped with the user who started the run, and it is
NOT a review, so it never counts as quality evidence about the model.
"""
from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from tret.config import get_settings
from sqlalchemy import select

from tret.db.models import Finding, User, Workspace, WorkspaceMember

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("tret.auto_approve")

AUTO_APPROVE_CATEGORIES = ("draft_section", "connected_write", "findings")
# The key inside `Workspace.settings` holding a per-workspace list.
AUTO_APPROVE_SETTINGS_KEY = "auto_approve"
# `Approval.purpose` of an automatic approval. Never accepted from the public API.
AUTO_PURPOSE = "auto"


def resolve_auto_approve(workspace_settings: dict | None, default: list[str] | None) -> frozenset[str]:
    """The workspace's own list when the key is present (even empty), else the
    deployment default. Unknown members (a stale or hand-edited value) are
    ignored, not an error."""
    settings = workspace_settings or {}
    chosen = settings[AUTO_APPROVE_SETTINGS_KEY] if AUTO_APPROVE_SETTINGS_KEY in settings else default
    if not isinstance(chosen, (list, tuple, set, frozenset)):
        chosen = default or []
    return frozenset(c for c in chosen if c in AUTO_APPROVE_CATEGORIES)


def category_for(finding: Finding) -> str | None:
    """The auto-approve category of a finding, or None when it can never be
    auto-approved (a package_map)."""
    if finding.schema_slug == "package_map":
        return None
    if finding.schema_slug in ("draft_section", "connected_write"):
        return finding.schema_slug
    return "findings"


async def enabled_categories(db: AsyncSession, workspace_id: uuid.UUID | None) -> frozenset[str]:
    """The categories auto-approved in this workspace right now."""
    workspace = await db.get(Workspace, workspace_id) if workspace_id is not None else None
    return resolve_auto_approve(workspace.settings if workspace else None, get_settings().auto_approve_default)


async def maybe_auto_approve(
    db: AsyncSession,
    finding_id: uuid.UUID,
    *,
    workspace_id: uuid.UUID | None,
    actor_user_id: uuid.UUID | None,
) -> bool:
    """Approve the just-created draft `finding_id` when its category is enabled
    for the workspace. The caller must have committed the draft first. Returns
    True when it was approved. Never raises: on any failure the finding stays a
    draft for a human and this returns False."""
    if workspace_id is None or actor_user_id is None:
        return False
    try:
        # The stamped actor must still be an enabled member of this workspace:
        # a run outlives a removal or deactivation, and an automatic approval
        # (and any upload it triggers) is made in that person's name.
        actor = await db.get(User, actor_user_id)
        if actor is None or actor.disabled:
            return False
        member = (
            await db.execute(
                select(WorkspaceMember.user_id).where(
                    WorkspaceMember.user_id == actor_user_id,
                    WorkspaceMember.workspace_id == workspace_id,
                )
            )
        ).first()
        if member is None:
            return False
        finding = await db.get(Finding, finding_id)
        if finding is None or finding.status != "draft":
            return False
        category = category_for(finding)
        if category is None:
            return False
        if category not in await enabled_categories(db, workspace_id):
            return False
        # Lazy: tret.api.findings imports the engine's neighbours, and this runs
        # from inside the engine's tools.
        from tret.api.findings import _decide

        await _decide(
            db, finding_id, "approve", "Approved automatically by workspace policy",
            approver_id=actor_user_id, approver_name="Automatic", workspace_id=workspace_id,
            auto=True,
        )
        return True
    except Exception:  # noqa: BLE001 - a policy hiccup must leave a normal draft
        log.warning("auto-approval of finding %s failed; left as draft", finding_id, exc_info=True)
        try:
            await db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False
