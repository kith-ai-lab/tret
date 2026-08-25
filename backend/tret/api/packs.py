from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user, require_admin
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.db.engine import get_db
from tret.db.models import Pack, Project, User
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
async def list_packs(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    packs = (
        await db.execute(select(Pack).where(Pack.workspace_id == ctx.id).order_by(Pack.slug))
    ).scalars().all()
    return [_out(p) for p in packs]


@router.get("/{pack_id}")
async def get_pack(
    pack_id: uuid.UUID,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    p = await db.get(Pack, pack_id)
    if p is None or p.workspace_id != ctx.id:
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
async def list_methods(
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """All deterministic methods across this workspace's installed packs."""
    packs = (
        await db.execute(select(Pack).where(Pack.workspace_id == ctx.id).order_by(Pack.slug))
    ).scalars().all()
    out = []
    for p in packs:
        for m in p.manifest.get("methods", []):
            out.append({**m, "pack_slug": p.slug, "pack_id": str(p.id)})
    return out


class InstallBody(BaseModel):
    path: str


@router.post("/install")
async def install(
    body: InstallBody,
    admin: User = Depends(require_admin),
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Instance-admin gated, not workspace-admin: `path` names a directory on
    the *server's* filesystem — any workspace-admin (which any user can
    become simply by creating their own team workspace, see
    services/workspace.py::create_workspace) being able to call this would
    turn it into a directory-existence oracle and a doctrine-file reader over
    the whole host, not just something scoped to that admin's own data. The
    data this endpoint writes (the installed Pack row, `project` lookup) stays
    scoped to the caller's *current* workspace via `ctx` — only who may call
    it at all is instance-wide.
    """
    pack_dir = Path(body.path)
    if not pack_dir.is_dir():
        raise HTTPException(422, f"'{body.path}' is not a directory")
    project = (
        await db.execute(select(Project).where(Project.workspace_id == ctx.id))
    ).scalars().first()
    if project is None:
        raise HTTPException(500, "No project exists in this workspace")
    try:
        pack = await install_pack(db, pack_dir, ctx.id, project.id)
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
