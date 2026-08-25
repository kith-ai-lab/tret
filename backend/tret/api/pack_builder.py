"""The in-app pack builder's draft CRUD and actions (Plan Phase D).

A `DraftPack` is workspace-scoped scratch state — a slug plus a manifest and
a bag of files, all JSONB — that never reaches the engine at run time. Three
actions turn a draft into real files on disk for the span of one request and
nothing longer: `validate` (materialize -> the unchanged `validate_pack` ->
discard), `test-install` (materialize with a `{version}+draft.{n}` version
override -> the unchanged `install_pack` -> swap into the pack's permanent
storage directory, exactly as `install_pack_from_archive` does for an
uploaded archive), and `export` (materialize straight to tar.gz bytes, the
same archive layout an upload accepts). See `tret.packs.draft` for the
shared materialization code all three call into.

**Who can create**: gating is the workspace-admin role (`require_workspace_admin`),
so both workspace kinds author packs — every user is `owner` of their own
personal workspace, and team owners/admins author in team workspaces, where
drafts are workspace-scoped rows every admin of that team can see and edit.
Test-installs land in whichever workspace the draft lives in. Cross-workspace
access to a draft 404s (never 403s) like every other workspace-scoped
resource in this codebase.

**No methods authoring**: `PATCH`'s manifest replacement rejects any
non-empty `manifest_json["methods"]` with 422 — methods packs (executable
Python, only deterred, never sandboxed; see `packs/safety.py`) arrive only
via `POST /api/packs/install/archive`, which keeps the marketplace review
queue's elevated scrutiny over method-bearing packs meaningful. There is no
server-side way around this for a draft; an author who needs methods builds
the archive by hand (or exports a methods-free draft and adds them after).
This check only ever inspects `PatchDraftBody.manifest_json` — it does
nothing to guard `files{}`, so the guarantee also depends on `files{}` never
being able to *supply* a `pack.yaml` of its own: `validate_draft_relpath`
(`tret.packs.draft`) rejects that key by name, and `materialize_draft`/
`build_draft_archive` refuse to let it win even if one somehow reached the
database anyway (`tret.packs.draft.RESERVED_ROOT_NAMES`). Without that,
`files["pack.yaml"]` would let a PATCH replace the *generated* manifest
outright — methods rejection and all — at validate/test-install/export time.

**Submit-to-marketplace** (Phase D's `POST .../submit`) is deliberately not
implemented here: it is cloud-only wiring (calls the in-process marketplace
API directly, capability-gated away on self-host) that lands with the
registry client itself. The seam is `GET /{draft_id}/export`, above — a
self-hosted author's path is export, then upload through the cloud UI.
"""
from __future__ import annotations

import re
import shutil
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_project, require_workspace_admin
from tret.db.engine import get_db
from tret.db.models import DraftPack, User
from tret.packs.draft import (
    DraftFileContentError,
    DraftPathError,
    build_draft_archive,
    file_bytes,
    materialize_draft,
    validate_draft_relpath,
    validate_no_path_shadowing,
)
from tret.packs.loader import (
    PackInstallConflict,
    PackValidationError,
    _swap_into_final_dir,
    install_pack,
    validate_pack,
)
from tret.packs.storage import PackStorage

router = APIRouter(prefix="/api/packs/drafts", tags=["pack-builder"])

# A draft's files are markdown doctrine, JSON schemas, small CSV datasets and
# template text — nothing here should ever need to approach an archive
# upload's own 10MB cap (`packs/archive.py::MAX_COMPRESSED_BYTES`), so the
# same figure is reused as the *uncompressed* total a draft may hold. Per-file
# is half of that: generous for any one file this editor is meant to produce,
# without letting a single upload consume the whole budget.
MAX_DRAFT_TOTAL_BYTES = 10 * 1024 * 1024
MAX_DRAFT_FILE_BYTES = 5 * 1024 * 1024

# `{version}+draft.{n}` — see DraftPack.test_install_seq's own docstring.
DRAFT_VERSION_INFIX = "+draft."

_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")


async def _get_own_draft(db: AsyncSession, draft_id: uuid.UUID, ctx: WorkspaceContext) -> DraftPack:
    """The draft, only if it belongs to the caller's current workspace — a
    draft in another workspace 404s, never confirming it exists there, the
    same posture `api.workspace.project_in_workspace` documents for every
    other workspace-scoped resource."""
    draft = await db.get(DraftPack, draft_id)
    if draft is None or draft.workspace_id != ctx.id:
        raise HTTPException(404, "Draft not found")
    return draft


def _summary(d: DraftPack) -> dict:
    manifest = d.manifest_json or {}
    return {
        "id": str(d.id),
        "slug": d.slug,
        "version": manifest.get("version"),
        "display_name": manifest.get("display_name", d.slug),
        "file_count": len(d.files or {}),
        "created_by": str(d.created_by) if d.created_by else None,
        "created_at": d.created_at.isoformat() if d.created_at else None,
        "updated_at": d.updated_at.isoformat() if d.updated_at else None,
    }


def _out(d: DraftPack) -> dict:
    return {
        **_summary(d),
        "manifest_json": d.manifest_json,
        "files": d.files or {},
        "test_install_seq": d.test_install_seq,
    }


def _draft_size(files: dict) -> int:
    return sum(len(file_bytes(content)) for content in files.values())


def _reject_methods(manifest_json: dict) -> None:
    methods = manifest_json.get("methods") if isinstance(manifest_json, dict) else None
    if methods:
        raise HTTPException(
            422,
            "Drafts may not declare methods: methods packs (executable Python, only "
            "deterred by a static scan, never sandboxed) arrive only via "
            "POST /api/packs/install/archive — the builder has no methods editor, "
            "and this is the server-side half of that, so the marketplace review "
            "queue's elevated scrutiny over method-bearing packs stays meaningful.",
        )


class CreateDraftBody(BaseModel):
    slug: str


@router.post("")
async def create_draft(
    body: CreateDraftBody,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    slug = body.slug.strip()
    if not slug:
        raise HTTPException(422, "slug may not be empty")
    manifest = {
        "pack": slug,
        "version": "0.1.0",
        "display_name": slug,
        "description": "",
        "author": None,
        "license": None,
        "homepage": None,
        "tags": [],
        "frameworks": [],
        "doctrine": [],
        "task_types": [],
        "datasets": [],
        "methods": [],
        "harnesses": [],
    }
    draft = DraftPack(
        workspace_id=ctx.id,
        slug=slug,
        manifest_json=manifest,
        files={},
        created_by=user.id,
    )
    db.add(draft)
    await db.commit()
    await db.refresh(draft)
    return _out(draft)


@router.get("")
async def list_drafts(
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    drafts = (
        await db.execute(
            select(DraftPack).where(DraftPack.workspace_id == ctx.id).order_by(DraftPack.created_at)
        )
    ).scalars().all()
    return [_summary(d) for d in drafts]


@router.get("/{draft_id}")
async def get_draft(
    draft_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    return _out(await _get_own_draft(db, draft_id, ctx))


class PatchDraftBody(BaseModel):
    manifest_json: dict | None = None
    # A file's value is `str` (text), `{"b64": "..."}` (binary), or `None` to
    # delete that path — see the DraftPack model docstring for the stored shape.
    files: dict[str, Any] | None = None


@router.patch("/{draft_id}")
async def patch_draft(
    draft_id: uuid.UUID,
    body: PatchDraftBody,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Partial update: `manifest_json`, when given, *replaces* the draft's
    stored manifest wholesale (the builder's editors always hold the whole
    manifest client-side, so there is no per-field merge to do); `files`,
    when given, is merged per key — a key mapped to `null` deletes that file,
    any other key is added or overwritten, and every key not mentioned is
    left alone.

    Optimistic concurrency (an `If-Unmodified-Since`-style `updated_at`
    match) is deliberately not implemented: a draft is edited by the small
    set of admins in one workspace through one builder UI, not the
    many-writer, external-API surface `Pack`'s own install idempotency
    guards against, so a last-write-wins PATCH is an acceptable simplicity
    trade for Phase D. Revisit if the builder grows real-time multi-editor
    collaboration.
    """
    draft = await _get_own_draft(db, draft_id, ctx)

    if body.manifest_json is not None:
        _reject_methods(body.manifest_json)
        draft.manifest_json = body.manifest_json

    if body.files is not None:
        merged = dict(draft.files or {})
        for relpath, content in body.files.items():
            if content is None:
                merged.pop(relpath, None)
                continue
            try:
                validate_draft_relpath(relpath)
            except DraftPathError as e:
                raise HTTPException(422, str(e)) from e
            try:
                size = len(file_bytes(content))
            except DraftFileContentError as e:
                raise HTTPException(422, f"file '{relpath}': {e}") from e
            if size > MAX_DRAFT_FILE_BYTES:
                raise HTTPException(
                    422,
                    f"file '{relpath}' is {size} bytes, over the "
                    f"{MAX_DRAFT_FILE_BYTES}-byte per-file cap",
                )
            merged[relpath] = content
        try:
            validate_no_path_shadowing(merged.keys())
        except DraftPathError as e:
            raise HTTPException(422, str(e)) from e
        try:
            total = _draft_size(merged)
        except DraftFileContentError as e:
            raise HTTPException(422, str(e)) from e
        if total > MAX_DRAFT_TOTAL_BYTES:
            raise HTTPException(
                422,
                f"draft would total {total} bytes across files, over the "
                f"{MAX_DRAFT_TOTAL_BYTES}-byte cap",
            )
        draft.files = merged

    await db.commit()
    await db.refresh(draft)
    return _out(draft)


@router.delete("/{draft_id}")
async def delete_draft(
    draft_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    draft = await _get_own_draft(db, draft_id, ctx)
    await db.delete(draft)
    await db.commit()
    return {"deleted": True}


@router.post("/{draft_id}/validate")
async def validate_draft(
    draft_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Materialize the draft to a throwaway directory and run the real,
    unchanged `validate_pack` against it — the same error strings a
    filesystem pack with the same flaw would produce, since a draft's
    `doctrine`/`task_types` selectors are already relative paths and
    `materialize_draft` always writes `pack.yaml`, so none of `validate_pack`'s
    error messages ever need a server-path prefix stripped (contrast
    `api.packs._relative_pack_errors`, needed there only because an archive
    upload's staging directory might not contain a `pack.yaml` at all)."""
    draft = await _get_own_draft(db, draft_id, ctx)
    pack_dir = materialize_draft(draft)
    try:
        manifest, schemas, errors = validate_pack(pack_dir)
    finally:
        shutil.rmtree(pack_dir, ignore_errors=True)

    return {
        "valid": not errors,
        "errors": errors,
        "summary": {
            "pack": manifest.pack if manifest else None,
            "version": manifest.version if manifest else None,
            "task_types": [t.slug for t in manifest.task_types] if manifest else [],
            "schemas": sorted(schemas),
            "doctrine_files": list(manifest.doctrine) if manifest else [],
        },
    }


@router.post("/{draft_id}/test-install")
async def test_install_draft(
    draft_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Install the draft into the caller's own current workspace/project
    under version `{version}+draft.{n}` — `n` is `DraftPack.test_install_seq`,
    incremented and committed *before* materialization runs, so a failed
    attempt still consumes its number rather than risking two attempts
    resolving to the same version string.

    Sequence mirrors `install_pack_from_archive` exactly, minus the archive
    extraction step it doesn't need here: materialize straight into
    `PackStorage.staging_dir()` (same filesystem as the pack's eventual
    permanent home, so the swap below is a same-device rename, never
    `OSError(EXDEV)`) -> the unchanged `install_pack` -> `_swap_into_final_dir`
    moves the materialized directory into `PackStorage.path_for(pack.id)` and
    `Pack.source_path` is updated to point there -> commit. The materialized
    directory is removed in `finally` unconditionally: on success it has
    already been renamed away by the swap, so there is nothing left to
    remove; on any failure it is the real cleanup.
    """
    draft = await _get_own_draft(db, draft_id, ctx)
    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "No project exists in this workspace")

    draft.test_install_seq += 1
    n = draft.test_install_seq
    await db.commit()
    await db.refresh(draft)

    base_version = str((draft.manifest_json or {}).get("version") or "0.1.0")
    version = f"{base_version}{DRAFT_VERSION_INFIX}{n}"

    storage = PackStorage()
    pack_root = materialize_draft(draft, version_override=version, root=storage.staging_dir())
    try:
        try:
            pack = await install_pack(db, pack_root, ctx.id, project.id)
        except PackValidationError as e:
            # Already relative — see validate_draft's own docstring for why.
            raise HTTPException(422, {"errors": e.errors}) from e
        except IntegrityError:
            # Same race `api/packs.py::install_archive` guards against: two
            # concurrent test-installs computing the same brand-new
            # `{version}+draft.{n}` (a repeat test-install racing itself
            # across two requests, or two admins test-installing at once)
            # both pass `install_pack`'s own idempotency check before either
            # commits, and the unique constraint on (workspace_id, slug,
            # version) catches whichever commits second. The session's
            # failed transaction must be rolled back before it can be used
            # again (Postgres aborts it after a failed statement) — a 409
            # telling the caller to retry, not a 500.
            await db.rollback()
            raise HTTPException(
                409, "A pack with this slug and version is already being installed"
            )

        final_dir = storage.path_for(pack.id)
        try:
            _swap_into_final_dir(pack_root, final_dir, content_hash=pack.content_hash)
        except PackInstallConflict as e:
            raise HTTPException(409, str(e)) from e
        pack.source_path = str(final_dir)
        await db.commit()
    finally:
        shutil.rmtree(pack_root, ignore_errors=True)

    return {"id": str(pack.id), "slug": pack.slug, "version": pack.version}


def _export_filename(d: DraftPack) -> str:
    slug = _SAFE_FILENAME_CHARS.sub("_", d.slug) or "pack"
    version = _SAFE_FILENAME_CHARS.sub("_", str((d.manifest_json or {}).get("version") or "0.1.0"))
    return f"{slug}-{version}.tar.gz"


@router.get("/{draft_id}/export")
async def export_draft(
    draft_id: uuid.UUID,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """tar.gz download — the self-hosted author's path: export, then upload
    through the cloud UI (or `POST /api/packs/install/archive` directly)."""
    draft = await _get_own_draft(db, draft_id, ctx)
    archive_bytes = build_draft_archive(draft)
    return StreamingResponse(
        iter([archive_bytes]),
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{_export_filename(draft)}"'},
    )


# `POST /{draft_id}/submit` (cloud-only: hands the draft to the in-process
# marketplace API, capability-gated away entirely on self-host) belongs here
# once the registry client (Plan Phase C) and the marketplace submission API
# (Plan Phase B) both exist to call into. Not implemented in Phase D.
