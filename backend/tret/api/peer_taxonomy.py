"""Peer taxonomy routes (`tret/taxonomy`, `tret/services/peer_taxonomy.py`): the
workspace's company classifications on the NACE Rev. 2 ladder.

* `GET /api/company-classifications`: list, filtered by source, cluster, division, or
  GICS disagreements only; paginated.
* `GET /api/company-classifications/effective?cik=&lei=&ticker=&target=`: one company's
  effective classification (manual > sec_sic > extractor) across all the keys given, with every
  row; the same resolver as `tret disclosure classifications`.
* `PUT /api/company-classifications/manual`: a workspace admin's override.

Every route is a 404 while `TRET_PEER_TAXONOMY` is off, as though the feature did not
exist. Rows are workspace-scoped. The operator CLI (`tret disclosure classify-sec`,
`classifications`) works whether or not the setting is on.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tret import taxonomy
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace, require_workspace_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import CompanyClassification, User
from tret.services import peer_taxonomy as svc

MAX_PAGE = 500


def _require_enabled() -> None:
    if not svc.peer_taxonomy_enabled(get_settings()):
        raise HTTPException(404, "Not found")


# A router-level dependency, so the 404 comes before any auth or role answer.
router = APIRouter(prefix="/api", tags=["peer-taxonomy"], dependencies=[Depends(_require_enabled)])


class ManualClassificationIn(BaseModel):
    company_key_type: str = Field(description="lei | cik | ticker | target")
    company_key: str = Field(min_length=1, max_length=200)
    company_name: str | None = Field(default=None, max_length=300)
    nace_code: str = Field(
        min_length=1, max_length=10,
        description='A NACE Rev. 2 division ("21") or group ("21.2" or "212")',
    )
    note: str | None = Field(default=None, max_length=1000)


@router.get("/company-classifications")
async def list_company_classifications(
    source: str | None = None,
    cluster: str | None = None,
    division: str | None = None,
    disagreements_only: bool = False,
    limit: int = Query(100, ge=1, le=MAX_PAGE),
    offset: int = Query(0, ge=0),
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    cc = CompanyClassification
    where = [cc.workspace_id == ctx.id]
    if source:
        where.append(cc.source == source)
    if cluster:
        where.append(cc.cluster == cluster)
    if division:
        where.append(cc.nace_division == division)
    if disagreements_only:
        where.append(cc.gics_disagrees.is_(True))
    total = (await db.execute(select(func.count()).select_from(cc).where(*where))).scalar_one()
    rows = (
        await db.execute(
            select(cc).where(*where)
            .order_by(cc.company_name, cc.company_key_type, cc.company_key, cc.source)
            .limit(limit).offset(offset)
        )
    ).scalars()
    return {
        "classifications": [svc.row_out(r) for r in rows],
        "total": total, "limit": limit, "offset": offset,
        "taxonomy_version": taxonomy.TAXONOMY_VERSION,
    }


@router.get("/company-classifications/effective")
async def get_effective_classification(
    cik: str | None = None,
    lei: str | None = None,
    ticker: str | None = None,
    target: str | None = None,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """One company's effective classification, resolved exactly as `tret disclosure
    classifications` does: across the rows of every key given (at least one of `cik`, `lei`,
    `ticker`, `target`), manual > sec_sic > extractor. A manual override keyed by ticker
    therefore applies to every target with that ticker in the workspace."""
    keys: list[tuple[str, str]] = []
    for key_type, value in (("cik", cik), ("lei", lei), ("ticker", ticker), ("target", target)):
        if value is None:
            continue
        norm = svc.normalise_key(key_type, value)
        if norm is None:
            raise HTTPException(422, f"not a valid {key_type} key")
        keys.append((key_type, norm))
    if not keys:
        raise HTTPException(422, "give at least one of cik, lei, ticker, target")
    best, ordered = await svc.effective_for_keys(db, ctx.id, keys)
    if best is None:
        raise HTTPException(404, "No classification for this company")
    return {"effective": svc.row_out(best), "sources": [svc.row_out(r) for r in ordered]}


@router.put("/company-classifications/manual")
async def put_manual_classification(
    body: ManualClassificationIn,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    try:
        row = await svc.set_manual(
            db, ctx.id,
            key_type=body.company_key_type, key=body.company_key, company_name=body.company_name,
            nace_code=body.nace_code, note=body.note, reviewed_by=user.email,
        )
    except ValueError as e:
        raise HTTPException(422, str(e)) from None
    await db.commit()
    return svc.row_out(row)
