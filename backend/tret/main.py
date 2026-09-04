"""tret — an open-source AI harness platform for non-technical knowledge work."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from starlette.datastructures import MutableHeaders

from tret.api import (
    analytics,
    auth,
    chat,
    connections as connections_api,
    docs,
    documents,
    findings,
    harnesses,
    pack_builder,
    packs,
    runs,
    settings as settings_api,
    workspaces as workspaces_api,
)
from tret.config import enforce_production_safety, get_settings
from tret.db.engine import get_engine, get_session_factory
from tret.db.migrate import ensure_schema
from tret.engine.extensions import get_extension_registry, load_extensions
from tret.net import log_egress_at_boot
from tret.providers.catalog import get_catalog

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tret")

# ── security response headers ─────────────────────────────────────────────────
# tret sets no CORS middleware and the session cookie is already `SameSite=lax`
# (tret/api/auth.py), which is the actual CSRF defense — these four headers are
# a different concern: clickjacking (X-Frame-Options), MIME-sniffing
# (X-Content-Type-Options), referrer leakage across origins, and a CSP as a
# backstop against script injection reaching anywhere it could act on the
# session. Mirrored verbatim in frontend/nginx.conf for the docker-compose
# deployment path, which serves the same built SPA without ever going through
# this backend.
#
# script-src carries one hash rather than 'unsafe-inline': frontend/index.html
# has exactly one inline script (the dark-mode pre-paint check, so a stored
# preference never flashes light) and nothing else needs inline script.
# 'unsafe-inline' would have covered it too, but would also have covered any
# script an XSS bug ever manages to inject — the one thing this header exists
# to rule out. If that inline script's contents change, recompute the hash
# (`sha256(content)`, base64) and update it here **and** in nginx.conf, or the
# built SPA's dark-mode preload is silently blocked (degraded, not broken: the
# app still renders, just with an occasional light-mode flash before React's
# own theme state takes over).
#
# style-src keeps 'unsafe-inline': React's `style={{...}}` becomes inline
# `style="..."` attributes, which CSP's style-src (not script-src) governs, and
# there is no equivalent hash-per-element scheme worth the churn for those.
# connect-src 'self' covers /api/runs/{id}/events (same-origin SSE). One
# exception to "nothing in the bundle calls a different origin": the Google
# Picker (frontend/src/components/shared/googleDrivePicker.ts) loads
# `https://apis.google.com/js/api.js` (script-src), renders in a
# `https://docs.google.com` iframe (frame-src) and makes XHRs to
# `https://content.googleapis.com` and `https://docs.google.com` (connect-src)
# — those three origins are the minimal widening needed for it, added to
# script-src/frame-src/connect-src only, nothing else loosened.
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self' 'sha256-2uazwxIKVNaSPni5VjTyuSxTIMSS7feaMo3K0rfV0Hg=' https://apis.google.com; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "font-src 'self' data:; "
    "connect-src 'self' https://content.googleapis.com https://docs.google.com; "
    "frame-src 'self' https://docs.google.com; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

SECURITY_HEADERS: dict[str, str] = {
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


class SecurityHeadersMiddleware:
    """Adds the headers above to every response that doesn't already set them.

    Pure ASGI rather than `BaseHTTPMiddleware`: the latter buffers a response
    to let middleware read/rewrite it, which does not play well with the
    long-lived SSE stream `GET /api/runs/{id}/events` depends on. This instead
    wraps `send` and only ever touches the single `http.response.start`
    message, so a streaming response's later `http.response.body` messages
    pass through untouched.

    "Doesn't already set them" matters for one route today:
    `api/findings.py`'s deliverable HTML export sets its own much stricter,
    sandboxed CSP (and its own X-Content-Type-Options / Referrer-Policy) for a
    document rendered from model-authored markdown — this middleware must
    never widen that back out.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS.items():
                    if name not in headers:
                        headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Enforce the single-instance assumption the run event bus depends on,
    # before anything else touches the database: a deploy handover's brief
    # overlap is retried out (services/instance_lock.py), and only once that
    # is settled does it make sense to ask "what did the prior instance leave
    # behind" below — sweeping orphaned runs while an old machine might still
    # legitimately be finishing them would be exactly backwards.
    from tret.services.instance_lock import acquire_instance_lock

    instance_lock = await acquire_instance_lock()
    # Bring the schema to the Alembic head — including adopting a legacy
    # create_all database created by an older release — then run the idempotent
    # seed. tret/db/migrate.py explains the three states this handles; a
    # database it cannot classify raises SchemaUpgradeError and startup fails
    # here on purpose, rather than crashing later on a missing column.
    await ensure_schema(get_engine())
    from tret.services.bootstrap import bootstrap

    async with get_session_factory()() as db:
        await bootstrap(db)
    # A prior process's runs stuck at queued/running are, on this single-
    # instance deployment, orphaned by definition — nothing else could still
    # be executing them. Swept and their post-run hooks fired here: after
    # extension registration (`load_extensions`, above, in create_app — a
    # hook needs a loaded billing extension to reach), and before the
    # extension startup tasks below, so nothing an extension warms at boot
    # (a credit balance cache, say) is built against runs still wrongly
    # showing as in-flight.
    #
    # Skipped when `instance_lock.state == "lost"`: Postgres,
    # TRET_INSTANCE_LOCK=warn (the default), and another process already held
    # the key when this one's retry budget ran out. `InstanceLock(None, ...)`
    # looks identical for "lost" and "not_applicable" (SQLite, or the check
    # off entirely) if this only inspected `._conn` — that ambiguity is
    # exactly the bug: a machine that lost the race is not "the only instance,
    # nothing else could still be executing these runs", it is a *second*
    # instance next to one that may well still be executing them, and sweeping
    # here would fail every run the live machine has not finished yet and
    # double-meter it when it does.
    from tret.services.reconcile import sweep_orphaned_runs

    if instance_lock.state == "lost":
        log.error(
            "skipping the orphaned-run sweep: this process lost the instance lock "
            "(TRET_INSTANCE_LOCK=warn) — another instance may still be running and "
            "legitimately finishing runs this process would otherwise wrongly fail"
        )
    else:
        async with get_session_factory()() as db:
            await sweep_orphaned_runs(db)
    # Extension seam: awaited, so an extension whose startup work (warming a
    # cache, checking its own schema) must finish before the app is reachable
    # gets to block boot on it. No-op with no extensions loaded.
    await get_extension_registry().run_startup_tasks()
    # Discover local + dynamic models in the background. Deliberately *not*
    # awaited: a boot must never depend on a reachable Ollama or on
    # openrouter.ai, and a slow or hanging model server must not hold the socket
    # closed. Scheduling it here is what makes a headless run on a fresh process
    # able to route to a local or dynamic model without anyone opening the UI
    # first (the router also calls `warm_once()` as a backstop, for the run that
    # arrives before this task finishes).
    warm_task = asyncio.create_task(get_catalog().warm())
    # Before "ready", so anything switched off is visible above the line an
    # operator reads as success. A deployment with egress off looks exactly like
    # a deployment with a bad API key; this is the difference.
    log_egress_at_boot(logger=log)
    log.info("tret is ready")
    try:
        yield
    finally:
        # Shutdown: stop waiting on a network call nobody needs the answer to.
        warm_task.cancel()
        with suppress(asyncio.CancelledError):
            await warm_task
        # Release last: a process that never actually held the lock (SQLite,
        # TRET_INSTANCE_LOCK=off, or a `warn` that lost the race) closes
        # nothing here — see InstanceLock.release.
        await instance_lock.release()


def _safe_static_file(root: Path, request_path: str) -> Path | None:
    """The file `request_path` names inside `root`, or None if it escapes.

    `root` must already be resolved. Returns None for anything that is not a
    regular file contained in `root` — traversal (encoded or not), absolute
    paths, symlinks pointing outside, and NUL bytes all land here.
    """
    if not request_path:
        return None
    try:
        candidate = (root / request_path).resolve()
        if not candidate.is_relative_to(root):
            return None
        if not candidate.is_file():
            return None
    except (OSError, ValueError):
        # ValueError: embedded NUL. OSError: symlink loops, name too long, etc.
        return None
    return candidate


def create_app() -> FastAPI:
    # Fail fast, before the socket is bound: TRET_ENVIRONMENT=production must
    # not run on the shipped development secrets. See docs/hardening.md.
    enforce_production_safety(log=log)

    app = FastAPI(title="tret", version="0.1.0", lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.include_router(auth.router)
    # Teams, members and invites (api/workspaces.py) — always mounted, like
    # api/workspace.py's context resolution: tenancy primitives are core, not
    # gated behind multi_tenant/auth_mode.
    app.include_router(workspaces_api.router)
    app.include_router(workspaces_api.invite_accept_router)
    # Generic OIDC login (tret/api/oidc.py) — mounted only when an issuer is
    # configured, so every self-hosted deployment (the default) never even
    # imports this module. See config.py::oidc_issuer.
    if get_settings().oidc_issuer:
        from tret.api import oidc as oidc_api

        app.include_router(oidc_api.router)
    app.include_router(analytics.router)
    app.include_router(chat.router)
    app.include_router(runs.router)
    app.include_router(harnesses.router)
    app.include_router(documents.router)
    # Reference documentation, read straight out of `docs/` so the methodology the
    # product displays is the methodology in the repository (tret/api/docs.py).
    app.include_router(docs.router)
    app.include_router(findings.router)
    # In-app pack builder (Plan Phase D): draft CRUD + validate/test-install/
    # export. Additive to the path-install and archive-install surfaces below,
    # never a replacement for either. Mounted *before* `packs.router`: FastAPI
    # matches routes in registration order, and packs.router's own
    # `GET|DELETE /api/packs/{pack_id}` would otherwise swallow
    # `GET|POST /api/packs/drafts` first, failing pack_id's UUID conversion
    # with a 422 rather than ever reaching this router's list/create routes.
    app.include_router(pack_builder.router)
    app.include_router(packs.router)
    app.include_router(settings_api.router)
    # Workspace connections (Google Drive / Microsoft 365 OAuth) — always
    # mounted, like workspaces_api above: every provider simply reports
    # `configured: false` (GET /api/connections/providers) until an operator
    # sets its client id/secret or an extension supplies one.
    app.include_router(connections_api.router)
    # After every core router: an extension's own router (if it adds one) is
    # additive to the open-source API surface, never a replacement for it.
    # No-op with TRET_EXTENSIONS unset — load_extensions still runs, and sets
    # the singleton get_extension_registry() returns to a default that allows
    # everything and mounts nothing.
    load_extensions(app, get_settings().extensions)

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True}

    # Single-app deployments (Fly, etc.): serve the built SPA from the backend.
    frontend_dir = get_settings().serve_frontend_dir
    if frontend_dir:
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # Resolved once, at startup: every request is checked for containment
        # against this real path, so no request can escape the served directory.
        dist = Path(frontend_dir).resolve()
        index = dist / "index.html"
        if index.is_file():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

            @app.get("/{full_path:path}", include_in_schema=False)
            async def spa(full_path: str):
                # This route is unauthenticated and `full_path` is fully client
                # controlled. ASGI servers percent-decode the request path but do
                # NOT normalize it, so "%2e%2e%2f" arrives as a real ".." segment
                # and Path.__truediv__ happily absorbs both that and an absolute
                # path. Containment is therefore checked on the *resolved* path
                # (which also collapses symlinks) rather than by string matching.
                served = _safe_static_file(dist, full_path)
                if served is not None:
                    return FileResponse(served)
                # Unknown paths are SPA deep links: hand back the app shell.
                return FileResponse(index)

    return app


app = create_app()
