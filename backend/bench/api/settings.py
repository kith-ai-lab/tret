"""Settings + catalog endpoints: provider key status (write-only keys), the
model catalog for pickers, and the available tool list.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user, require_admin
from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import ProviderCredential, User, Workspace
from bench.engine.tools import get_builtin_tools
from bench.providers.catalog import ProviderRegistry, get_catalog
from bench.services.credentials import get_fernet, load_db_keys

router = APIRouter(prefix="/api", tags=["settings"])

PROVIDERS = ("anthropic", "kimi", "openrouter")


@router.get("/settings/providers")
async def provider_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    db_keys = await load_db_keys(db)
    env_keys = {
        "anthropic": settings.anthropic_api_key,
        "kimi": settings.moonshot_api_key,
        "openrouter": settings.openrouter_api_key,
    }
    out = []
    for p in PROVIDERS:
        env, stored = env_keys.get(p, ""), db_keys.get(p, "")
        effective = env or stored
        out.append(
            {
                "provider": p,
                "configured": bool(effective),
                "source": "env" if env else ("db" if stored else None),
                "last4": effective[-4:] if effective else None,
            }
        )
    return out


class SetKeyBody(BaseModel):
    provider: str
    api_key: str


@router.post("/settings/providers")
async def set_provider_key(
    body: SetKeyBody, user: User = Depends(require_admin), db: AsyncSession = Depends(get_db)
):
    if body.provider not in PROVIDERS:
        raise HTTPException(422, f"provider must be one of {PROVIDERS}")
    workspace = (await db.execute(select(Workspace))).scalars().first()
    encrypted = get_fernet().encrypt(body.api_key.encode())
    existing = await db.get(ProviderCredential, (workspace.id, body.provider))
    if existing:
        existing.encrypted_key = encrypted
    else:
        db.add(
            ProviderCredential(
                workspace_id=workspace.id, provider=body.provider, encrypted_key=encrypted
            )
        )
    await db.commit()
    return {"ok": True}


@router.get("/models")
async def list_models(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    catalog = get_catalog()
    await catalog.refresh_dynamic()
    registry = ProviderRegistry(await load_db_keys(db))
    return [
        {**m.to_json(), "available": registry.has_key(m.provider)}
        for m in catalog.all()
    ]


@router.get("/tools")
async def list_tools(user: User = Depends(current_user)):
    return [
        {"name": t.name, "description": t.description, "parameters": t.parameters}
        for t in get_builtin_tools().values()
    ]


@router.get("/settings/router")
async def router_settings(user: User = Depends(current_user)):
    settings = get_settings()
    from bench.router_llm.prompts import ROUTING_PROMPT_VERSION

    return {
        "router_model": settings.router_model,
        "routing_prompt_version": ROUTING_PROMPT_VERSION,
        "timeout_seconds": settings.router_timeout_seconds,
    }
