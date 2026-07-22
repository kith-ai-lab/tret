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

    return app


app = create_app()
