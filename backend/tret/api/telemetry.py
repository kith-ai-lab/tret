"""Admin API for the opt-in anonymous telemetry reporter
(`tret/services/telemetry.py`) — contract §6.

`GET`/`PUT` mirror each other's shape so a UI can render the same card either
way; `GET /preview` shares `build_payload()`'s exact code path with the
sender, so what an admin sees is byte-for-byte what would be POSTed.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import require_admin
from tret.db.engine import get_db
from tret.db.models import User
from tret.services.telemetry import TelemetryLocked
from tret.services.telemetry import preview as telemetry_preview
from tret.services.telemetry import set_enabled as telemetry_set_enabled
from tret.services.telemetry import status as telemetry_status

router = APIRouter(prefix="/api/admin/telemetry", tags=["telemetry"])


class TelemetryToggle(BaseModel):
    enabled: bool


@router.get("")
async def get_telemetry(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return await telemetry_status(db)


@router.get("/preview")
async def get_telemetry_preview(
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    return await telemetry_preview(db)


@router.put("")
async def put_telemetry(
    body: TelemetryToggle,
    user: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    try:
        await telemetry_set_enabled(db, body.enabled)
    except TelemetryLocked as exc:
        raise HTTPException(
            status_code=409,
            detail=(
                f"telemetry is locked ({exc.reason}) — the effective state cannot be "
                "changed from this toggle."
            ),
        ) from exc
    return await telemetry_status(db)
