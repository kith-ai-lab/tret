"""bench — an open-source AI harness platform for non-technical knowledge work."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from bench.api import auth, chat, documents, findings, harnesses, packs, runs, settings as settings_api
from bench.db.engine import get_engine, get_session_factory
from bench.db.models import Base

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bench")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create tables if missing (Alembic owns real migrations; this keeps the
    # docker-compose demo one-command) and run the idempotent seed.
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    from bench.services.bootstrap import bootstrap

    async with get_session_factory()() as db:
        await bootstrap(db)
    log.info("bench is ready")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="bench", version="0.1.0", lifespan=lifespan)
    app.include_router(auth.router)
    app.include_router(chat.router)
    app.include_router(runs.router)
    app.include_router(harnesses.router)
    app.include_router(documents.router)
    app.include_router(findings.router)
    app.include_router(packs.router)
    app.include_router(settings_api.router)

    @app.get("/api/healthz")
    async def healthz():
        return {"ok": True}

    # Single-app deployments (Fly, etc.): serve the built SPA from the backend.
    from bench.config import get_settings

    frontend_dir = get_settings().serve_frontend_dir
    if frontend_dir:
        from pathlib import Path

        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        dist = Path(frontend_dir)
        if (dist / "index.html").is_file():
            app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

            @app.get("/{full_path:path}", include_in_schema=False)
            async def spa(full_path: str):
                candidate = dist / full_path
                if full_path and candidate.is_file():
                    return FileResponse(candidate)
                return FileResponse(dist / "index.html")

    return app


app = create_app()
