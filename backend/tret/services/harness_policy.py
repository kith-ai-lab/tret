"""Harness policy history: who changed a harness's `model_policy` /
`loop_config`, when, and from what, so an admin can see and revert it.

Rows are written by the harness PUT (source "edit"), by experiment promotion
(source "experiment") and by `revert_policy_change` (source "revert"). Nothing
here commits: the caller owns the transaction.
"""
from __future__ import annotations

import copy
import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api._policy import validate_policy
from tret.db.models import Harness, HarnessPolicyHistory, User, utcnow

SOURCES = ("edit", "experiment", "revert")


class PolicyUnchanged(Exception):
    """A revert that would leave the harness exactly as it is."""


def policy_changed(previous_model_policy, previous_loop_config, harness: Harness) -> bool:
    """True when the harness's policy or loop config differs from the previous values."""
    return (
        harness.model_policy != previous_model_policy
        or harness.loop_config != previous_loop_config
    )


async def record_policy_change(
    db: AsyncSession,
    harness: Harness,
    *,
    previous_model_policy: dict,
    previous_loop_config: dict,
    user_id: uuid.UUID | None,
    source: str,
    source_id: uuid.UUID | None = None,
    note: str | None = None,
) -> HarnessPolicyHistory:
    """Record a change already assigned on `harness`; the caller commits."""
    if source not in SOURCES:
        raise ValueError(f"unknown policy-change source '{source}'")
    row = HarnessPolicyHistory(
        workspace_id=harness.workspace_id,
        harness_id=harness.id,
        changed_by=user_id,
        changed_at=utcnow(),
        source=source,
        source_id=source_id,
        previous_model_policy=copy.deepcopy(previous_model_policy),
        previous_loop_config=copy.deepcopy(previous_loop_config),
        new_model_policy=copy.deepcopy(harness.model_policy),
        new_loop_config=copy.deepcopy(harness.loop_config),
        note=note,
    )
    db.add(row)
    await db.flush()
    return row


async def list_policy_history(
    db: AsyncSession, harness: Harness, *, limit: int = 100
) -> list[dict]:
    """Newest first, with the changer's display name."""
    rows = (
        await db.execute(
            select(HarnessPolicyHistory, User.display_name)
            .outerjoin(User, User.id == HarnessPolicyHistory.changed_by)
            .where(HarnessPolicyHistory.harness_id == harness.id)
            .order_by(HarnessPolicyHistory.changed_at.desc(), HarnessPolicyHistory.id)
            .limit(limit)
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "harness_id": str(r.harness_id),
            "changed_at": r.changed_at.isoformat() if r.changed_at else None,
            "changed_by": str(r.changed_by) if r.changed_by else None,
            "changed_by_name": name,
            "source": r.source,
            "source_id": str(r.source_id) if r.source_id else None,
            "note": r.note,
            "previous_model_policy": r.previous_model_policy,
            "previous_loop_config": r.previous_loop_config,
            "new_model_policy": r.new_model_policy,
            "new_loop_config": r.new_loop_config,
        }
        for r, name in rows
    ]


async def revert_policy_change(
    db: AsyncSession, harness: Harness, history_id: uuid.UUID, user: User
) -> HarnessPolicyHistory:
    """Restore a row's previous policy and loop config; records a "revert" row.

    LookupError: the row is not this harness's. PolicyUnchanged: the harness
    already has those values (no row is written). ValueError: the previous policy
    no longer validates (a model that left the catalog, say). Caller commits.
    """
    row = await db.get(HarnessPolicyHistory, history_id)
    if row is None or row.harness_id != harness.id:
        raise LookupError("Policy history entry not found")
    target_policy = copy.deepcopy(row.previous_model_policy)
    target_loop = copy.deepcopy(row.previous_loop_config)
    try:
        validate_policy(target_policy)
    except HTTPException as exc:
        raise ValueError(str(exc.detail)) from exc
    if not policy_changed(target_policy, target_loop, harness):
        raise PolicyUnchanged("The harness already has these settings")
    before_policy = copy.deepcopy(harness.model_policy)
    before_loop = copy.deepcopy(harness.loop_config)
    harness.model_policy = target_policy
    harness.loop_config = target_loop
    return await record_policy_change(
        db,
        harness,
        previous_model_policy=before_policy,
        previous_loop_config=before_loop,
        user_id=user.id,
        source="revert",
        source_id=row.id,
    )
