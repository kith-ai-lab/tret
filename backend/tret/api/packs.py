from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user, require_admin
from tret.api.workspace import WorkspaceContext, current_project, current_workspace, require_workspace_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Dataset, Finding, Harness, HarnessPack, MethodRun, Pack, Project, Run, User
from tret.net import EgressDenied, build_client
from tret.net.policy import ClassPolicy, MODE_OFF, MODE_ON, VERIFY_NONE, master_mode
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
        "harnesses": manifest.get("harnesses", []),
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
    confirms it exists there), 409s while any *active* Harness, or any Run,
    Finding, or MethodRun in this workspace still references it (a used pack
    is not deletable — its task types, doctrine and methods are load-bearing
    for whatever used it; uninstall is for a mistaken install, not a pack with
    a history), and otherwise deletes the `Pack` row plus its extracted
    directory.

    An *archived* Harness does not block the delete: an operator who installs
    a preset pack by mistake archives the auto-created harness
    (`PUT /api/harnesses/{id}` with `is_archived: true`) and then deletes the
    pack — the harness's own doctrine/task-type dependency on the pack is
    gone the moment it is archived (it can never run again), so there is
    nothing left for the delete to protect. Nor does the chat front door
    (`Harness.task_profile == "chat"`): it links every installed pack by
    default (`services.workspace._seed_chat_harness`), and that default link
    is advisory, not load-bearing — the chat harness never fails to run for
    lacking a pack (`api/chat.py::send_message` falls back to `pack_id=None`),
    so treating it as a blocker would make a pack it happened to auto-link
    permanently undeletable. A harness's packs are `harness_packs` link rows,
    not a column on `Harness` (one harness may link several packs), so
    archived harnesses (and the chat harness) still linked to this pack have
    those link rows deleted below rather than left dangling on a `pack_id`
    that is about to not exist — left as a real FK, deleting the pack out
    from under one would be an integrity violation, not merely a semantic
    concern.

    Pack-seeded datasets are deliberately left in place (ship minimal
    uninstall; the plan's own resolved judgment call) — deleting the pack
    does not delete data an analyst may already be relying on. `Dataset.pack_id`
    is nullable, so those rows are severed from the pack (set to NULL) rather
    than blocking the delete or being deleted themselves.

    Ordering matters here: the row delete is committed *before* the directory
    is removed, not after. `Pack.id` is a foreign key on `harness_packs`/Run/
    Finding/MethodRun (checked above) and on Dataset (severed above), so
    committing first is what makes the DB the source of truth — if the
    directory removal below fails partway, the row is already gone either
    way, and a leaked directory is a cheap, logged loose end rather than a
    `Pack` row left pointing at bytes that no longer exist (the previous
    ordering's failure mode: `db.commit()` could still fail with an FK
    violation *after* the directory was already destroyed).
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
        if model is Harness:
            # A harness references a pack via `harness_packs`, not a column
            # on Harness itself. Archived harnesses don't count, and neither
            # does the chat front door's own default link — see this
            # endpoint's docstring.
            query = (
                select(Harness)
                .join(HarnessPack, HarnessPack.harness_id == Harness.id)
                .where(
                    HarnessPack.pack_id == pack_id,
                    Harness.is_archived.is_(False),
                    Harness.task_profile != "chat",
                )
            )
        else:
            query = select(model).where(model.pack_id == pack_id)
        referencing = (await db.execute(query.limit(1))).scalar_one_or_none()
        if referencing is not None:
            name = getattr(referencing, "name", None) or str(referencing.id)
            remedy = (
                "archive the harness first, then delete the pack"
                if model is Harness
                else "uninstall targets mistaken installs"
            )
            raise HTTPException(
                409,
                f"Pack '{pack.slug}@{pack.version}' is still referenced by a {label} "
                f"('{name}') — used packs are not deletable; {remedy}.",
            )

    await db.execute(update(Dataset).where(Dataset.pack_id == pack_id).values(pack_id=None))
    # Only archived harnesses and/or the chat harness can still be linked to
    # this pack at this point (any other active harness would have 409'd
    # above) — delete their `harness_packs` rows explicitly, for the same FK
    # reason Dataset is severed just above.
    # Explicit delete rather than an ON DELETE CASCADE on
    # `harness_packs.pack_id` (see `HarnessPack`'s docstring), so this is the
    # one place orphaned links are prevented rather than relying on the DB to
    # clean them up when the Pack row disappears below.
    await db.execute(delete(HarnessPack).where(HarnessPack.pack_id == pack_id))

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


# ── Marketplace registry client (Find / Install) ─────────────────────────────
#
# Everything below is a backend *proxy*: the browser never talks to the
# registry (a hosted deployment's `/api/marketplace` API) directly — every call goes
# through this router, through `tret.net.build_client`, exactly the pattern
# `api/oidc.py` uses for its own outbound calls. See that module's docstring
# for the fuller argument; the short version repeated here because it is easy
# to get backwards: `_marketplace_policy` below is deliberately NOT a sixth
# `TRET_EGRESS_*`-switchable class. Those five classes exist because their
# destinations are chosen by something other than the operator (a model, an
# uploaded document, a configured-but-swappable provider). The marketplace
# registry is a single destination the *operator* names once
# (`TRET_PACK_REGISTRY_URL`, defaulted to Kith's own) and every request this
# router makes to it is one this backend constructs itself from a slug and a
# version — never a URL read out of user input. That is exactly the same
# shape as the OIDC issuer, so it rides the master switch (`TRET_EGRESS`)
# alone, the same way OIDC's egress does.
#
# INVARIANT, worth repeating because it is the whole point of keeping this a
# proxy: the RUN path — executing a task with an already-installed pack —
# never touches the registry. A pack, once installed, is a `Pack` row plus a
# directory on disk (`packs/loader.py`, `packs/storage.py`); nothing at
# runtime reads `pack_registry_url` at all. Registry downtime only affects
# Find, Install and Submit, never a run already using an installed pack.
_MARKETPLACE_EGRESS_CLASS = "marketplace"  # audit-log label only — see note above
_MARKETPLACE_MAX_BYTES = 15 * 1024 * 1024  # generous headroom over a manifest/doctrine JSON body or a compressed archive (archive.py's own 10MB cap applies again once install_pack_from_archive extracts one)
_MARKETPLACE_TIMEOUT_SECONDS = 30.0
_MARKETPLACE_DOWNLOAD_CHUNK_BYTES = 256 * 1024

# `slug`/`version` reach the URLs this router constructs (`{base}/packs/{slug}/{version}`)
# from two places FastAPI does not fully sanitize for this purpose: a path
# parameter (`registry_pack_version`, `registry_pack_summary` — Starlette's
# string convertor excludes a literal "/", but a caller can still hand it
# something like ".." or a percent-encoded segment) and a JSON request body
# (`RegistryInstallBody` — entirely unconstrained). Either one lands straight
# in an f-string with no further encoding, so an unvalidated slug/version
# could walk the URL path on the *configured* registry host to an endpoint
# this router never intended to call (the host allowlist in
# `_marketplace_policy` pins the destination's host, not the path). A pack
# slug/version is operator- or author-chosen at publish time and always
# looks like this in practice; `+` is required (not just `.`/`-`/`_`) for a
# test-install's own `{version}+draft.{n}` suffix (`api/pack_builder.py`) to
# round-trip through Submit once that lands.
_REGISTRY_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def _validate_registry_identifier(value: str, *, what: str) -> str:
    if not _REGISTRY_IDENTIFIER_RE.fullmatch(value):
        raise HTTPException(422, f"{what} {value!r} is not a valid pack slug/version")
    return value

# 60s in-process TTL cache, keyed by the full upstream URL (query string
# included, so `?q=risk` and `?q=climate` are different entries) — the same
# per-process, restart-clears shape as oidc.py's discovery/JWKS caches and
# every other in-memory cache this codebase ships (docs/hardening.md's
# single-worker deployment note). Search and detail are read far more often
# than the registry's own catalog changes, and every call here is still
# audited through the egress chokepoint regardless of whether it hits the
# cache or the network.
_MARKETPLACE_CACHE_TTL_SECONDS = 60.0
# Hard cap on distinct cached URLs — a bound most legitimate use never
# approaches (there are not 256 different searches/pack pages inside one 60s
# TTL window in practice), but without one an attacker who can make this
# backend issue registry GETs with attacker-chosen query strings (registry_search's
# `q`/`tags`/`framework`, each a distinct cache key) could grow this dict
# without bound — it lives for the process's lifetime otherwise, unlike an
# LRU-backed cache with real eviction. Insertion order is eviction order
# (oldest first, a plain dict already preserves that), which is "good enough"
# for a cache whose whole point is a 60s TTL, not real LRU recency.
_MARKETPLACE_CACHE_MAX_ENTRIES = 256
_registry_cache: dict[str, tuple[float, dict]] = {}


def _registry_cache_set(url: str, now: float, data: dict) -> None:
    """Record `data` for `url`, purging every expired entry first (so a burst
    of distinct queries doesn't accumulate the dead weight of ones that have
    already aged out) and then evicting the oldest entries if still over
    `_MARKETPLACE_CACHE_MAX_ENTRIES` — belt and braces: TTL purge handles the
    ordinary case, the hard cap is what keeps a determined attacker (or just
    a very chatty legitimate client) from growing this dict past a bound
    regardless of TTL.
    """
    expired = [key for key, (ts, _) in _registry_cache.items() if now - ts >= _MARKETPLACE_CACHE_TTL_SECONDS]
    for key in expired:
        del _registry_cache[key]
    _registry_cache[url] = (now, data)
    while len(_registry_cache) > _MARKETPLACE_CACHE_MAX_ENTRIES:
        oldest_key = next(iter(_registry_cache))
        del _registry_cache[oldest_key]


def _marketplace_policy(url: str) -> ClassPolicy:
    """A one-host allowlist scoped to the configured registry — see the
    section docstring above for why this is not a `TRET_EGRESS_*`-switchable
    class. `VERIFY_NONE`: the destination is operator config
    (`TRET_PACK_REGISTRY_URL`), never a document a user uploaded or a model
    chose, so the SSRF address-resolution check `research` needs does not
    apply here either — the same reasoning `_oidc_policy` gives for its own
    equally operator-configured destination.

    `mode` is derived through `master_mode` — the same normalization the
    rest of the egress lattice applies to `TRET_EGRESS` (tret/net/policy.py)
    — rather than a local `.strip().lower() == "off"` check, so a spelling
    the lattice folds to `off` for every other class (an alias, garbage, or
    `replay`, which has no meaning outside `research`) folds to off here
    too, instead of leaking through as "not literally off" -> on. The host
    allowlist above is what pins the destination to the configured registry;
    this is only ever the on/off decision.
    """
    host = (urlsplit(url).hostname or "").lower()
    return ClassPolicy(
        name=_MARKETPLACE_EGRESS_CLASS,
        mode=MODE_ON if master_mode() == MODE_ON else MODE_OFF,
        allow_hosts=frozenset({host}) if host else frozenset(),
        allow_http=False,
        standard_ports_only=True,
        verify_addresses=VERIFY_NONE,
        max_bytes=_MARKETPLACE_MAX_BYTES,
        timeout_seconds=_MARKETPLACE_TIMEOUT_SECONDS,
    )


def _registry_base() -> str:
    return get_settings().pack_registry_url.strip().rstrip("/")


def _require_registry_enabled() -> str:
    base = _registry_base()
    if not base:
        raise HTTPException(
            503,
            "The pack marketplace is not configured (TRET_PACK_REGISTRY_URL is empty) — "
            "Find and Install are unavailable; installed packs are unaffected.",
        )
    return base


def _registry_proxy_error(exc: Exception, *, what: str) -> HTTPException:
    """Map a failure reaching the registry to the 4xx/5xx this router's own
    callers see. Mirrors `oidc.py::_get_json`'s error phrasing (a 503 naming
    what is unavailable, a 502 naming what could not be reached), extended
    with a clean 404 for "the registry itself says this doesn't exist" rather
    than folding that into the same 502 a network failure gets.

    `ValueError` (`response.json()`'s own failure mode — `json.JSONDecodeError`
    is a `ValueError` subclass) is the registry answering 200 with a body
    that isn't JSON at all: not this caller's fault and not a network
    failure either, so it gets the same 502 a network failure gets rather
    than bubbling up as an unhandled 500.
    """
    if isinstance(exc, EgressDenied):
        return HTTPException(503, f"The pack marketplace is unavailable: {exc}")
    if isinstance(exc, httpx.HTTPStatusError):
        if exc.response.status_code == 404:
            return HTTPException(404, f"{what} not found in the marketplace registry")
        return HTTPException(
            502, f"The marketplace registry returned HTTP {exc.response.status_code} for {what}"
        )
    if isinstance(exc, httpx.HTTPError):
        return HTTPException(502, f"Could not reach the marketplace registry for {what}: {exc}")
    if isinstance(exc, ValueError):
        return HTTPException(502, f"The marketplace registry returned a malformed response for {what}")
    raise exc  # pragma: no cover — not one of the errors this helper maps


async def _registry_get(url: str, *, what: str) -> dict:
    """GET `url` (already the full, backend-constructed upstream URL) through
    the egress chokepoint, serving a cached body when one is fresh enough.
    Raises the mapped `HTTPException` on any failure — see
    `_registry_proxy_error`."""
    now = time.monotonic()
    cached = _registry_cache.get(url)
    if cached is not None and now - cached[0] < _MARKETPLACE_CACHE_TTL_SECONDS:
        return cached[1]
    try:
        async with build_client(
            _MARKETPLACE_EGRESS_CLASS, policy=_marketplace_policy(url), timeout=_MARKETPLACE_TIMEOUT_SECONDS
        ) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
    except (EgressDenied, httpx.HTTPError, ValueError) as exc:
        raise _registry_proxy_error(exc, what=what) from exc
    _registry_cache_set(url, now, data)
    return data


async def _download_pack_archive(url: str, *, what: str) -> bytes:
    """Stream `url`'s body, refusing anything over `_MARKETPLACE_MAX_BYTES` —
    cut off mid-stream rather than buffered in full first, on the same
    reasoning `tret.net.fetch.fetch_page` gives for a page a model chose to
    fetch. The destination here is operator-configured rather than
    model-chosen, but a misbehaving or compromised registry serving an
    unbounded response is still worth capping rather than trusting.
    """
    try:
        async with build_client(
            _MARKETPLACE_EGRESS_CLASS,
            policy=_marketplace_policy(url),
            timeout=_MARKETPLACE_TIMEOUT_SECONDS,
        ) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                body = bytearray()
                async for chunk in response.aiter_bytes(_MARKETPLACE_DOWNLOAD_CHUNK_BYTES):
                    body.extend(chunk)
                    if len(body) > _MARKETPLACE_MAX_BYTES:
                        raise HTTPException(
                            502,
                            f"The marketplace registry's download for {what} exceeded "
                            f"{_MARKETPLACE_MAX_BYTES // (1024 * 1024)}MB — refusing to install",
                        )
                return bytes(body)
    except (EgressDenied, httpx.HTTPError) as exc:
        raise _registry_proxy_error(exc, what=what) from exc


@router.get("/registry/search")
async def registry_search(
    q: str = "",
    tags: str = "",
    framework: str = "",
    user: User = Depends(current_user),
):
    """Proxy to the registry's `GET /packs` — listed packs only, enforced
    registry-side. Registered before `/registry/{slug}` below so the literal
    path `/registry/search` matches here rather than being swallowed as
    `slug="search"` — FastAPI/Starlette match routes in registration order.
    """
    base = _require_registry_enabled()
    params = {k: v for k, v in {"q": q, "tags": tags, "framework": framework}.items() if v}
    url = f"{base}/packs" + (f"?{urlencode(params)}" if params else "")
    return await _registry_get(url, what="the pack search")


@router.get("/registry/{slug}/{version}")
async def registry_pack_version(slug: str, version: str, user: User = Depends(current_user)):
    """Proxy to the registry's `GET /packs/{slug}/{version}` — manifest,
    integrity hashes and the inlined doctrine preview for one version."""
    _validate_registry_identifier(slug, what="slug")
    _validate_registry_identifier(version, what="version")
    base = _require_registry_enabled()
    return await _registry_get(f"{base}/packs/{slug}/{version}", what=f"pack '{slug}@{version}'")


@router.get("/registry/{slug}")
async def registry_pack_summary(slug: str, user: User = Depends(current_user)):
    """Proxy to the registry's `GET /packs/{slug}` — summary plus its version
    list (client-side "update available" badging reads `latest_listed_version`
    off of this)."""
    _validate_registry_identifier(slug, what="slug")
    base = _require_registry_enabled()
    return await _registry_get(f"{base}/packs/{slug}", what=f"pack '{slug}'")


class RegistryInstallBody(BaseModel):
    slug: str
    version: str


@router.post("/registry/install")
async def registry_install(
    body: RegistryInstallBody,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    """Workspace-admin gated, same posture as `/install/archive` above: the
    only thing a caller controls is which listed (slug, version) to install,
    not a filesystem path.

    Sequence: fetch the version's detail off the registry for its declared
    `content_hash` and size -> stream the archive download, capped mid-stream
    -> `install_pack_from_archive(..., expected_content_hash=<the registry's
    declared hash>)`, which re-verifies that hash against the archive's
    *actual* content before anything is installed (defense in depth — this
    endpoint trusts the registry's own manifest for nothing more than which
    bytes to expect). A mismatch there means the registry served bytes that
    do not match what it itself catalogued, mapped to 502 rather than 422:
    that is not a malformed upload from this caller, it is the registry lying
    about — or corrupting — its own listing.
    """
    _validate_registry_identifier(body.slug, what="slug")
    _validate_registry_identifier(body.version, what="version")
    base = _require_registry_enabled()
    what = f"pack '{body.slug}@{body.version}'"
    detail = await _registry_get(f"{base}/packs/{body.slug}/{body.version}", what=what)
    content_hash = detail.get("content_hash")
    if not content_hash:
        raise HTTPException(
            502, f"The marketplace registry's listing for {what} has no content_hash — refusing to install"
        )

    project = await current_project(db, ctx.id)
    if project is None:
        raise HTTPException(500, "No project exists in this workspace")

    archive_bytes = await _download_pack_archive(
        f"{base}/packs/{body.slug}/{body.version}/download", what=what
    )

    try:
        pack = await install_pack_from_archive(
            db, ctx.id, project.id, archive_bytes, expected_content_hash=content_hash
        )
    except PackArchiveError as e:
        message = str(e)
        # install_pack_from_archive's own hash-mismatch message (loader.py) —
        # the one PackArchiveError case that is the registry's fault, not a
        # malformed upload from this caller. See this function's docstring.
        if "content hash" in message and "does not match the expected" in message:
            raise HTTPException(
                502, f"The marketplace registry served the wrong bytes for {what}: {message}"
            ) from e
        raise HTTPException(422, message)
    except PackValidationError as e:
        errors = _relative_pack_errors(e.errors, e.pack_root) if e.pack_root else e.errors
        raise HTTPException(422, {"errors": errors})
    except PackInstallConflict as e:
        raise HTTPException(409, str(e))
    except IntegrityError:
        # Same race as /install/archive's own IntegrityError handling above:
        # two concurrent installs of the same brand-new (workspace, slug,
        # version) both pass install_pack's own idempotency check before
        # either commits, and the unique constraint catches whichever loses.
        await db.rollback()
        raise HTTPException(409, "A pack with this slug and version is already being installed")
    return _out(pack)
