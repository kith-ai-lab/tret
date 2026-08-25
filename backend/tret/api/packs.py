from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user, require_admin
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, require_workspace_admin
from tret.db.engine import get_db
from tret.db.models import Dataset, Finding, Harness, MethodRun, Pack, Project, Run, User
from tret.packs.archive import MAX_COMPRESSED_BYTES, PackArchiveError
from tret.packs.loader import (
    PackInstallConflict,
    PackValidationError,
    install_pack,
    install_pack_from_archive,
    validate_pack,
)
from tret.packs.storage import PackStorage

router = APIRouter(prefix="/api/packs", tags=["packs"])

# Same posture as api/documents.py's upload cap: refuse a declared
# Content-Length over the cap early, and enforce the cap again mid-stream
# since Content-Length is client-declared and not to be trusted alone. The
# allowance covers the multipart envelope so an archive *at* the cap is not
# refused for its wrapper.
#
# Neither check happens "before any work", despite that having once been this
# comment's claim: FastAPI resolves the `file: UploadFile` parameter below by
# having Starlette parse the whole multipart body — spooling the archive's
# bytes into `file` — before this endpoint's body (and so this cap) ever
# runs. What the Content-Length check actually buys is an early exit *within*
# this handler once that parse has already happened, same honest caveat
# api/documents.py's own upload path documents. A hard body-size limit in
# front of tret (the reverse proxy) is what would refuse the transfer itself
# — see docs/hardening.md.
_ARCHIVE_CHUNK_BYTES = 1024 * 1024
_ARCHIVE_MULTIPART_OVERHEAD_ALLOWANCE = 64 * 1024


def _archive_too_large() -> HTTPException:
    return HTTPException(413, f"Archive too large ({MAX_COMPRESSED_BYTES // (1024 * 1024)}MB max)")


async def _read_capped_upload(request: Request, file: UploadFile) -> bytes:
    """Read `file` fully into memory, refusing anything over
    `MAX_COMPRESSED_BYTES` before or during the read. `extract_pack_archive`
    takes archive bytes directly (not a path), and the cap is small enough
    (10MB) that buffering it is fine — unlike documents.py's 25MB uploads,
    which spool straight to disk instead.

    By the time this runs, Starlette has already spooled the full multipart
    body (including the archive bytes) into `file` as part of resolving this
    endpoint's `UploadFile` parameter — see the module comment above. This
    function's checks are real (they stop an oversized archive from being
    buffered into memory a second time, hashed, or extracted), just not a
    refusal of the network transfer itself.
    """
    declared = request.headers.get("content-length")
    if declared and declared.isdigit():
        if int(declared) > MAX_COMPRESSED_BYTES + _ARCHIVE_MULTIPART_OVERHEAD_ALLOWANCE:
            raise _archive_too_large()

    chunks: list[bytes] = []
    total = 0
    while chunk := await file.read(_ARCHIVE_CHUNK_BYTES):
        total += len(chunk)
        if total > MAX_COMPRESSED_BYTES:
            raise _archive_too_large()
        chunks.append(chunk)
    return b"".join(chunks)


def _relative_pack_errors(errors: list[str], pack_root: Path) -> list[str]:
    """Rewrite `pack_root`'s server-absolute prefix out of `validate_pack`
    error strings before they reach a 4xx body — those strings are written to
    read well for `/install` and `/validate`, where the directory *is* an
    operator's own path worth naming, but an archive upload's `pack_root` is
    a server-side staging directory nobody outside this process should learn
    the location of. The one message this also reword outright is the
    missing-manifest case: `validate_pack` names the (now-relative) manifest
    path, but "the archive must contain pack.yaml at its top level" is what
    actually tells an uploader what to fix.
    """
    root_str = str(pack_root)
    out = []
    for error in errors:
        rewritten = error.replace(root_str + "/", "").replace(root_str, ".")
        if rewritten in ("pack.yaml does not exist", "./pack.yaml does not exist"):
            rewritten = "the archive must contain pack.yaml at its top level"
        out.append(rewritten)
    return out


def _out(p: Pack) -> dict:
    manifest = dict(p.manifest)
    return {
        "id": str(p.id),
        "slug": p.slug,
        "version": p.version,
        "display_name": manifest.get("display_name", p.slug),
        "description": manifest.get("description", ""),
        "author": manifest.get("author"),
        "license": manifest.get("license"),
        "homepage": manifest.get("homepage"),
        "tags": manifest.get("tags", []),
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


@router.post("/install/archive")
async def install_archive(
    request: Request,
    file: UploadFile,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Workspace-admin gated — unlike `/install` above, not instance-admin.

    `/install`'s stricter gate exists because `path` names a directory on the
    *server's* filesystem, which turns any caller into a directory-existence
    oracle over the whole host. An archive upload carries no such oracle: the
    only thing a caller controls is bytes that get extracted into a staging
    directory and validated exactly as `/install` validates a path
    (`packs/loader.py::install_pack_from_archive`, `packs/archive.py`'s
    rejection list). A workspace's own admin installing into their own
    workspace is the ordinary case this exists to serve.
    """
    archive_bytes = await _read_capped_upload(request, file)
    if not archive_bytes:
        raise HTTPException(422, "The uploaded archive is empty")

    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "No project exists in this workspace")

    try:
        pack = await install_pack_from_archive(db, ctx.id, project.id, archive_bytes)
    except PackArchiveError as e:
        raise HTTPException(422, str(e))
    except PackValidationError as e:
        errors = _relative_pack_errors(e.errors, e.pack_root) if e.pack_root else e.errors
        raise HTTPException(422, {"errors": errors})
    except PackInstallConflict as e:
        raise HTTPException(409, str(e))
    except IntegrityError:
        # Two concurrent uploads installing the same brand-new (workspace,
        # slug, version) both pass install_pack's own idempotency check
        # (neither sees the other's row yet) and race to insert — the
        # unique constraint on (workspace_id, slug, version) catches
        # whichever commits second. A real conflict, not a server error: the
        # session's failed transaction must be rolled back before it can be
        # used again (Postgres aborts it after a failed statement), and the
        # caller gets a 409 telling them to retry rather than a 500.
        await db.rollback()
        raise HTTPException(409, "A pack with this slug and version is already being installed")
    return _out(pack)


@router.delete("/{pack_id}")
async def delete_pack(
    pack_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Workspace-admin gated. 404s for a pack in another workspace (never
    confirms it exists there), 409s while any Harness, Run, Finding, or
    MethodRun in this workspace still references it (a used pack is not
    deletable — its task types, doctrine and methods are load-bearing for
    whatever used it; uninstall is for a mistaken install, not a pack with a
    history), and otherwise deletes the `Pack` row plus its extracted
    directory.

    Pack-seeded datasets are deliberately left in place (ship minimal
    uninstall; the plan's own resolved judgment call) — deleting the pack
    does not delete data an analyst may already be relying on. `Dataset.pack_id`
    is nullable, so those rows are severed from the pack (set to NULL) rather
    than blocking the delete or being deleted themselves.

    Ordering matters here: the row delete is committed *before* the directory
    is removed, not after. `Pack.id` is a foreign key on Harness/Run/Finding/
    MethodRun (checked above) and on Dataset (severed above), so committing
    first is what makes the DB the source of truth — if the directory removal
    below fails partway, the row is already gone either way, and a leaked
    directory is a cheap, logged loose end rather than a `Pack` row left
    pointing at bytes that no longer exist (the previous ordering's failure
    mode: `db.commit()` could still fail with an FK violation *after* the
    directory was already destroyed).
    """
    pack = await db.get(Pack, pack_id)
    if pack is None or pack.workspace_id != ctx.id:
        raise HTTPException(404, "Pack not found")

    for model, label in (
        (Harness, "harness"),
        (Run, "run"),
        (Finding, "finding"),
        (MethodRun, "method run"),
    ):
        referencing = (
            await db.execute(select(model).where(model.pack_id == pack_id).limit(1))
        ).scalar_one_or_none()
        if referencing is not None:
            name = getattr(referencing, "name", None) or str(referencing.id)
            raise HTTPException(
                409,
                f"Pack '{pack.slug}@{pack.version}' is still referenced by a {label} "
                f"('{name}') — used packs are not deletable; uninstall targets mistaken "
                "installs.",
            )

    await db.execute(update(Dataset).where(Dataset.pack_id == pack_id).values(pack_id=None))

    source_path = pack.source_path
    await db.delete(pack)
    await db.commit()

    storage = PackStorage()
    if storage.owns(source_path):
        # A failed removal here leaks a directory, never an exception: the
        # row above is already committed gone, and `PackStorage.remove`
        # itself logs a removal failure rather than raising one — see its
        # docstring.
        storage.remove(Path(source_path))

    return {"deleted": True}


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
