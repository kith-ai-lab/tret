"""Jury selection and the per-pass freeze (disclosure evaluation engine, PR 5).

Engine 1 judges pairs of report modules with a jury: one juror per match, rotated across model
families. Engine 2 makes one sealed call whose model is picked once per pass. Both need the same
thing: a model selection made by the ROUTER (never by name), shown and explained BEFORE any spend,
then held fixed for the whole measurement pass so the instrument does not change under the
measurement. The next pass picks again, freely.

* `preview_jury`: ask the router (`ModelRouter.route_jury`) who would sit, and why. No database
  write, no model call, no router LLM call.
* `freeze_jury`: the same pick, stored in `routing_freezes` under a `pass_ref`. Idempotent: a
  second call with the same pass reference returns the stored selection and never re-picks.
  `k=1` is Engine 2's one model per pass.
* `release_freeze`: the pass is over; the selection stays on record, it is just no longer active.

`routed_json_call(..., freeze=selection, juror_index=i)` then calls the frozen model without a new
routing decision. Runs made that way carry the `JUROR_KEY` / `SEALED_CALL_KEY` task-input marker
(`tret/batches/rubrics.py`) and never become router performance samples.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.models import RoutingFreeze
from tret.engine.routed_call import CallServices, check_policy, get_call_services
from tret.router_llm.jury_select import JurySelection

PURPOSES = ("eval_jury", "sealed_call")
MAX_K = 5


class JuryError(Exception):
    """A jury request that cannot be served; `status` is the HTTP status the API answers with."""

    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


def _check(purpose: str, k: int) -> None:
    if purpose not in PURPOSES:
        raise JuryError(f"purpose must be one of {', '.join(PURPOSES)}")
    if not 1 <= int(k) <= MAX_K:
        raise JuryError(f"k must be between 1 and {MAX_K}")


async def preview_jury(
    db: AsyncSession | None,
    *,
    workspace_id: uuid.UUID | None,
    model_policy: dict | None = None,
    k: int = 3,
    distinct_authors: bool = True,
    min_context_window: int = 0,
    purpose: str = "eval_jury",
    task_family: str | None = None,
    services: CallServices | None = None,
) -> JurySelection:
    """Who would sit on the jury, and why. Writes nothing and calls no model."""
    from tret.engine.routed_call import RoutedCallError
    from tret.router_llm.router import RoutingUnavailable

    _check(purpose, k)
    family = None
    if task_family:
        # A family key only; the workspace is appended here, never taken from the caller, so one
        # workspace cannot rank by another's family priors (as `services.outcomes` derives it).
        if "@" in task_family or workspace_id is None:
            raise JuryError("task_family is a family key without a workspace; it needs a workspace")
        family = f"{task_family}@{workspace_id}"
    svc = services or await get_call_services(db, workspace_id)
    try:
        return await svc.router.route_jury(
            check_policy(model_policy), int(k), distinct_authors=distinct_authors,
            min_context_window=int(min_context_window or 0), purpose=purpose, task_family=family,
        )
    except RoutedCallError as e:
        raise JuryError(str(e)) from e
    except RoutingUnavailable as e:
        raise JuryError(str(e)) from e


async def get_freeze(db: AsyncSession, *, workspace_id: uuid.UUID, freeze_id: uuid.UUID) -> RoutingFreeze:
    row = await db.get(RoutingFreeze, freeze_id)
    if row is None or row.workspace_id != workspace_id:
        raise JuryError("Freeze not found", 404)
    return row


async def find_freeze(
    db: AsyncSession, *, workspace_id: uuid.UUID, purpose: str, pass_ref: str
) -> RoutingFreeze | None:
    return (
        await db.execute(
            select(RoutingFreeze).where(
                RoutingFreeze.workspace_id == workspace_id,
                RoutingFreeze.purpose == purpose,
                RoutingFreeze.pass_ref == pass_ref,
            )
        )
    ).scalar_one_or_none()


async def freeze_jury(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    pass_ref: str,
    purpose: str = "eval_jury",
    model_policy: dict | None = None,
    k: int = 3,
    distinct_authors: bool = True,
    min_context_window: int = 0,
    task_family: str | None = None,
    created_by: str | None = None,
    services: CallServices | None = None,
) -> tuple[RoutingFreeze, bool]:
    """Pick once and store. Returns `(row, created)`. An active freeze for the same
    (workspace, purpose, pass_ref) is returned unchanged (`created` False) and nothing is re-picked,
    whatever policy the repeat call carries. A released one is a conflict: use a new pass reference."""
    _check(purpose, k)
    if not pass_ref or not pass_ref.strip():
        raise JuryError("pass_ref is required")
    existing = await find_freeze(db, workspace_id=workspace_id, purpose=purpose, pass_ref=pass_ref)
    if existing is not None:
        if existing.status != "active":
            raise JuryError(f"pass_ref {pass_ref!r} was already used and released; use a new pass reference", 409)
        return existing, False
    selection = await preview_jury(
        db, workspace_id=workspace_id, model_policy=model_policy, k=k, distinct_authors=distinct_authors,
        min_context_window=min_context_window, purpose=purpose, task_family=task_family, services=services,
    )
    row = RoutingFreeze(
        workspace_id=workspace_id, purpose=purpose, pass_ref=pass_ref,
        model_policy=selection.policy, k=int(k), selection=selection.to_json(), status="active",
        created_by=created_by,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        # A concurrent freeze of the same pass won: return its selection, never a second pick.
        existing = await find_freeze(db, workspace_id=workspace_id, purpose=purpose, pass_ref=pass_ref)
        if existing is None:
            raise
        if existing.status != "active":
            raise JuryError(f"pass_ref {pass_ref!r} was already used and released; use a new pass reference", 409) from None
        return existing, False
    return row, True


async def release_freeze(db: AsyncSession, *, workspace_id: uuid.UUID, freeze_id: uuid.UUID) -> RoutingFreeze:
    row = await get_freeze(db, workspace_id=workspace_id, freeze_id=freeze_id)
    if row.status != "released":
        row.status = "released"
        row.released_at = datetime.now(timezone.utc)
        await db.flush()
    return row


def selection_of(row: RoutingFreeze) -> JurySelection:
    """The frozen selection, to pass as `routed_json_call(freeze=...)`."""
    return JurySelection.from_json(row.selection)


def freeze_out(row: RoutingFreeze) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "purpose": row.purpose,
        "pass_ref": row.pass_ref,
        "k": row.k,
        "status": row.status,
        "model_policy": row.model_policy,
        "selection": row.selection,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "released_at": row.released_at.isoformat() if row.released_at else None,
    }
