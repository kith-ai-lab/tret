from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user, require_admin
from tret.db.engine import get_db
from tret.db.models import Pack, Project, User, Workspace
from tret.packs.loader import PackValidationError, install_pack, validate_pack

router = APIRouter(prefix="/api/packs", tags=["packs"])


def _out(p: Pack) -> dict:
    manifest = dict(p.manifest)
    return {
        "id": str(p.id),
        "slug": p.slug,
        "version": p.version,
        "display_name": manifest.get("display_name", p.slug),
        "description": manifest.get("description", ""),
        "frameworks": manifest.get("frameworks", []),
        "doctrine_sha": p.doctrine_sha,
        # The integrity pin: sha256 over every file in the pack, recorded at
        # install. Surfaced so an operator can compare what is installed against
        # what the pack author published (docs/pack-authoring.md). Null for packs
        # installed before integrity pinning landed.
        "content_hash": p.content_hash,
        "doctrine_files": manifest.get("doctrine", []),
        "task_types": manifest.get("task_types", []),
        "schemas": manifest.get("schemas", {}),
        "installed_at": p.installed_at.isoformat() if p.installed_at else None,
    }


@router.get("")
async def list_packs(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    packs = (await db.execute(select(Pack).order_by(Pack.slug))).scalars().all()
    return [_out(p) for p in packs]


@router.get("/{pack_id}")
async def get_pack(
    pack_id: uuid.UUID, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
):
    p = await db.get(Pack, pack_id)
    if p is None:
        raise HTTPException(404, "Pack not found")
    out = _out(p)
    # Doctrine viewer content.
    docs = {}
    pack_dir = Path(p.source_path)
    for rel in p.manifest.get("doctrine", []):
        try:
            docs[rel] = (pack_dir / rel).read_text()
        except OSError:
            docs[rel] = "(file unavailable)"
    out["doctrine_contents"] = docs
    return out


@router.get("/methods/all")
async def list_methods(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """All deterministic methods across installed packs (for UI + reference)."""
    packs = (await db.execute(select(Pack).order_by(Pack.slug))).scalars().all()
    out = []
    for p in packs:
        for m in p.manifest.get("methods", []):
            out.append({**m, "pack_slug": p.slug, "pack_id": str(p.id)})
    return out


class InstallBody(BaseModel):
    path: str


@router.post("/install")
async def install(
    body: InstallBody, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    pack_dir = Path(body.path)
    if not pack_dir.is_dir():
        raise HTTPException(422, f"'{body.path}' is not a directory")
    workspace = (await db.execute(select(Workspace))).scalars().first()
    project = (await db.execute(select(Project))).scalars().first()
    try:
        pack = await install_pack(db, pack_dir, workspace.id, project.id)
    except PackValidationError as e:
        raise HTTPException(422, {"errors": e.errors})
    return _out(pack)


class ValidateBody(BaseModel):
    path: str


@router.post("/validate")
async def validate(body: ValidateBody, user: User = Depends(current_user)):
    pack_dir = Path(body.path)
    if not pack_dir.is_dir():
        raise HTTPException(422, f"'{body.path}' is not a directory")
    manifest, schemas, errors = validate_pack(pack_dir)
    return {
        "valid": not errors,
        "errors": errors,
        "pack": manifest.pack if manifest else None,
        "task_types": [t.slug for t in manifest.task_types] if manifest else [],
        "schemas": sorted(schemas),
    }
