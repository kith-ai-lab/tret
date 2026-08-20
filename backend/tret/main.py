"""tret — an open-source AI harness platform for non-technical knowledge work."""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI

from tret.api import (
    analytics,
    auth,
    chat,
    docs,
    documents,
    findings,
    harnesses,
    packs,
    runs,
    settings as settings_api,
)
from tret.config import enforce_production_safety
from tret.db.engine import get_engine, get_session_factory
from tret.db.migrate import ensure_schema
from tret.net import log_egress_at_boot
from tret.providers.catalog import get_catalog

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tret")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Bring the schema to the Alembic head — including adopting a legacy
    # create_all database created by an older release — then run the idempotent
    # seed. tret/db/migrate.py explains the three states this handles; a
    # database it cannot classify raises SchemaUpgradeError and startup fails
    # here on purpose, rather than crashing later on a missing column.
    await ensure_schema(get_engine())
    from tret.services.bootstrap import bootstrap

    async with get_session_factory()() as db:
        await bootstrap(db)
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
    app.include_router(auth.router)
    app.include_router(analytics.router)
    app.include_router(chat.router)
    app.include_router(runs.router)
    app.include_router(harnesses.router)
    app.include_router(documents.router)
    # Reference documentation, read straight out of `docs/` so the methodology the
    # product displays is the methodology in the repository (tret/api/docs.py).
    app.include_router(docs.router)
    app.include_router(findings.router)
    app.include_router(packs.router)
    app.include_router(settings_api.router)

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True}

    # Single-app deployments (Fly, etc.): serve the built SPA from the backend.
    from tret.config import get_settings

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
