"""`GET/PUT/DELETE /api/workspace/settings/emissions` — a workspace's own
override document for the emissions factor ladder (grid intensity, PUE,
embodied hardware, the uncertainty band, the baseline model), plus what those
overrides resolve to right now and what tret ships if nothing is configured.

Read is open to any member; write requires admin or owner and is additionally
subject to a registered workspace gate (`emissions_factors_edit`), the same
fail-open extension seam `api/workspaces.py` gives invite creation. See
`tret/services/emission_settings.py` for the layering itself and
`docs/emissions-methodology.md`'s "Effective factors and the settings API"
subsection for the contract this router implements.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.db.engine import get_db
from tret.db.models import User, Workspace
from tret.engine.extensions import get_extension_registry
from tret.providers.catalog import get_catalog
from tret.services.emission_factors import EmissionsOverrides
from tret.services.emission_settings import (
    EMISSIONS_SETTINGS_KEY,
    MAX_EMISSIONS_BODY_BYTES,
    effective_factors,
    shipped_defaults,
    validation_detail as _validation_detail,
    workspace_emissions_layers,
)
from tret.services.emissions import LOCAL_PROVIDER

log = logging.getLogger("tret.emissions_settings")

router = APIRouter(prefix="/api", tags=["emissions-settings"])

# The workspace-gate action a registered extension may veto a write on — the
# same string on both the PUT and DELETE routes, since clearing the document
# is as much an edit as setting it.
GATE_ACTION = "emissions_factors_edit"


async def _notify_settings_hooks(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    user_id: uuid.UUID | None,
) -> None:
    """Fire `run_workspace_settings_hooks` without letting it turn a
    successful write into an error response.

    `run_workspace_settings_hooks` already catches and logs whatever an
    individual hook raises (see `tret/engine/extensions.py`), but this call
    site guards against a bug in the dispatcher itself (or in
    `get_extension_registry()`) the same way — a broken extension seam must
    never take down a PUT or DELETE that already committed successfully.
    """
    try:
        await get_extension_registry().run_workspace_settings_hooks(
            db, workspace_id, EMISSIONS_SETTINGS_KEY, before, after, user_id
        )
    except Exception:
        log.exception("run_workspace_settings_hooks raised; ignoring")


def _response(
    workspace_doc: dict[str, Any] | None,
    managed_doc: dict[str, Any] | None,
    *,
    error: str | None = None,
) -> dict:
    """The GET/PUT/DELETE response shape.

    `error` is set (and `effective` withheld as `None`) when `workspace_doc`
    no longer validates against `EmissionsOverrides` — a downgrade, a
    hand-edited row, a field a later version removed. `overrides` still
    carries the raw, un-resolved document either way, so the settings panel
    can show what is stored (and a "clear overrides" action) even when
    nothing can be safely resolved to "what applies next". PUT and DELETE
    never reach this with an invalid `workspace_doc` — the document they just
    wrote (or cleared) already validated or does not exist — so `error` is
    GET's own concern.
    """
    response: dict[str, Any] = {
        "overrides": workspace_doc or {},
        "effective": (
            None
            if error is not None
            else effective_factors(workspace_doc=workspace_doc, managed_doc=managed_doc)
        ),
        "shipped_defaults": shipped_defaults(),
    }
    if error is not None:
        response["error"] = error
    return response


@router.get("/workspace/settings/emissions")
async def get_emissions_settings(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Fails open on a stored document that no longer validates: the panel
    still needs `overrides` (to show what is stored and offer "clear
    overrides") even though nothing can be safely resolved to `effective` —
    see `_response`. A run reading the *same* broken document never 500s
    either, for the identical reason (`HarnessEngine._factors_for`); this is
    the read side of that same fail-open contract.
    """
    workspace_doc, managed_doc = await workspace_emissions_layers(db, ctx.id)
    if workspace_doc:
        try:
            EmissionsOverrides(**workspace_doc)
        except ValidationError as exc:
            return _response(workspace_doc, managed_doc, error=_validation_detail(exc))
    return _response(workspace_doc, managed_doc)


def _too_large_body() -> HTTPException:
    return HTTPException(
        413, f"Request body too large ({MAX_EMISSIONS_BODY_BYTES // (1024 * 1024)}MB max)"
    )


@router.put("/workspace/settings/emissions")
async def put_emissions_settings(
    request: Request,
    body: dict = Body(default_factory=dict),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    caller: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    # Refused before the expensive part (`EmissionsOverrides(**body)`, which
    # parses every `grid.tables` CSV entry) ever runs — same posture as
    # `api/packs.py`'s archive-upload cap and `api/documents.py`'s
    # file-upload cap: FastAPI has already parsed this JSON body into memory
    # by the time this handler runs, so this buys refusing the validation
    # work, not the network transfer itself. See `MAX_EMISSIONS_BODY_BYTES`.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_EMISSIONS_BODY_BYTES:
        raise _too_large_body()

    try:
        overrides = EmissionsOverrides(**body)
    except ValidationError as exc:
        raise HTTPException(422, detail=_validation_detail(exc)) from exc

    if overrides.baseline_model:
        model = get_catalog().get(overrides.baseline_model)
        if model is None or model.provider == LOCAL_PROVIDER:
            raise HTTPException(
                422,
                detail=(
                    f"baseline_model must name a non-local model in the catalog, "
                    f"got {overrides.baseline_model!r}"
                ),
            )

    gate = await get_extension_registry().check_workspace_gate(db, ctx.id, GATE_ACTION)
    if not gate.allowed:
        raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})

    doc = overrides.model_dump(exclude_none=True)
    doc["updated_by"] = caller.email or str(caller.id)
    doc["updated_at"] = datetime.now(timezone.utc).isoformat()

    workspace = await db.get(Workspace, ctx.id)
    # Reassigned, not mutated in place: SQLAlchemy only detects a change to a
    # JSONB column on assignment, and an in-place `workspace.settings[...] = `
    # would silently fail to commit.
    settings = dict(workspace.settings or {})
    before = settings.get(EMISSIONS_SETTINGS_KEY)
    settings[EMISSIONS_SETTINGS_KEY] = doc
    workspace.settings = settings
    await db.commit()

    await _notify_settings_hooks(db, ctx.id, before, doc, caller.id)

    workspace_doc, managed_doc = await workspace_emissions_layers(db, ctx.id)
    return _response(workspace_doc, managed_doc)


@router.delete("/workspace/settings/emissions")
async def delete_emissions_settings(
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    caller: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
):
    gate = await get_extension_registry().check_workspace_gate(db, ctx.id, GATE_ACTION)
    if not gate.allowed:
        raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})

    workspace = await db.get(Workspace, ctx.id)
    settings = dict(workspace.settings or {})
    before = settings.get(EMISSIONS_SETTINGS_KEY)
    settings.pop(EMISSIONS_SETTINGS_KEY, None)
    workspace.settings = settings
    await db.commit()

    await _notify_settings_hooks(db, ctx.id, before, None, caller.id)

    workspace_doc, managed_doc = await workspace_emissions_layers(db, ctx.id)
    return _response(workspace_doc, managed_doc)
