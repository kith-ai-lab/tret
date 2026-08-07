"""Settings + catalog endpoints: provider key status (write-only keys), the
model catalog for pickers, the available tool list, and the local-model
connection diagnostic.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bench.api.auth import current_user, require_admin
from bench.config import get_settings
from bench.db.engine import get_db
from bench.db.models import ProviderCredential, User, Workspace
from bench.engine.tools import get_builtin_tools
from bench.providers.catalog import (
    KEY_PROVIDERS,
    PROVIDER_SPECS,
    ProviderRegistry,
    get_catalog,
)
from bench.services.credentials import get_fernet, load_db_keys

log = logging.getLogger("bench.settings")

router = APIRouter(prefix="/api", tags=["settings"])

# The providers a workspace can store an API key for, straight from the catalog's
# ProviderSpec table — not a second hand-maintained list. Adding a provider is
# meant to be a one-place change (an advertised extension path, README/
# docs/architecture.md), and this endpoint used to be one of the places that
# quietly had to be edited too: a provider missing from here was unreachable from
# the settings UI even though the registry could build it.
PROVIDERS = KEY_PROVIDERS

# Cap for the catalog refresh behind `GET /api/models`. Discovery reaches out to
# OpenRouter and to the configured local server, and local discovery probes each
# model it finds for tool support — so without a cap one hung local inference
# server holds the model picker (and everything that waits for it) for as long as
# it likes. Past this the honest answer is the catalog as it stands: the picker
# renders the cloud models, and the next call re-tries discovery.
MODELS_DISCOVERY_TIMEOUT_SECONDS = 12.0

# Whole-request cap for the local connection test: one /models GET (10s) plus a
# forced tool-call probe per discovered model (8s each) can otherwise add up on
# a server with a dozen models pulled. Past this the answer is "too slow to be
# usable", which is itself the useful diagnostic.
LOCAL_TEST_TIMEOUT_SECONDS = 45.0


@router.get("/settings/providers")
async def provider_status(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    settings = get_settings()
    db_keys = await load_db_keys(db)
    # Which Settings attribute holds each provider's env key: read off the same
    # ProviderSpec rows the registry constructs providers from, so the two can no
    # longer disagree about where a key lives.
    env_keys = {
        spec.name: getattr(settings, spec.env_key_attr)
        for spec in PROVIDER_SPECS
        if spec.env_key_attr
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
    # Key-optional providers ("local" today) have no API key concept — a
    # configured base URL is the credential, so there is nothing to keep
    # write-only or DB-store the way cloud keys are. Same response shape, driven
    # by the spec's `enabled_attr` rather than a hardcoded name.
    for spec in PROVIDER_SPECS:
        if not spec.key_optional:
            continue
        enabled = bool(getattr(settings, spec.enabled_attr))
        out.append(
            {
                "provider": spec.name,
                "configured": enabled,
                "source": "env" if enabled else None,
                "last4": None,
            }
        )
    return out


def _local_test_payload(
    *,
    configured: bool,
    base_url: str | None,
    reachable: bool,
    error: str | None = None,
    models: list[dict] | None = None,
) -> dict:
    models = models or []
    tool_capable = sum(1 for m in models if m["supports_tools"])
    return {
        "configured": configured,
        "base_url": base_url,
        "reachable": reachable,
        "error": error,
        "models": models,
        "counts": {
            "models": len(models),
            "tool_capable": tool_capable,
            "no_tools": len(models) - tool_capable,
        },
    }


@router.post("/settings/providers/local/test")
async def test_local_provider(user: User = Depends(require_admin)):
    """Diagnose the configured local model server. Reads only; persists nothing.

    Deliberately takes **no body and no parameters**. The only URL this endpoint
    will ever fetch is the server's own `BENCH_LOCAL_BASE_URL`: accepting a URL
    from the client would turn a logged-in browser into a request forger against
    anything the backend can reach (cloud metadata endpoints, internal
    services). Echoing the configured base URL back is fine — it is operator
    config, not a secret, and it is already visible to whoever set it.

    A fresh pass, not the cached one: it bypasses the 5-minute discovery TTL and
    re-probes tool support, because the whole point of pressing the button is to
    see the server as it is right now (`ollama pull` a minute ago included).
    """
    settings = get_settings()
    if not settings.local_base_url:
        return _local_test_payload(configured=False, base_url=None, reachable=False)

    catalog = get_catalog()
    try:
        result = await asyncio.wait_for(
            catalog.refresh_local(force=True), timeout=LOCAL_TEST_TIMEOUT_SECONDS
        )
    except (asyncio.TimeoutError, TimeoutError):
        return _local_test_payload(
            configured=True,
            base_url=settings.local_base_url,
            reachable=False,
            error=(
                f"Timeout: the server did not finish discovery and tool probing within "
                f"{LOCAL_TEST_TIMEOUT_SECONDS:.0f}s"
            ),
        )

    return _local_test_payload(
        configured=True,
        base_url=result.base_url,
        reachable=result.reachable,
        error=result.error,
        models=[
            {
                "id": m.id,
                "display_name": m.display_name,
                "supports_tools": m.supports_tools,
                "context_window": m.context_window,
            }
            for m in sorted(result.models, key=lambda m: m.id)
        ],
    )


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
    try:
        # Concurrent and capped: a refresh that does not finish in time is
        # abandoned, and the catalog is served as it stands rather than the
        # request hanging. Both refreshes are best-effort by design — each
        # publishes its result only on success — so an abandoned one leaves no
        # half-updated catalog behind.
        await asyncio.wait_for(
            asyncio.gather(catalog.refresh_dynamic(), catalog.refresh_local()),
            timeout=MODELS_DISCOVERY_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        log.warning(
            "model discovery did not finish within %.0fs; serving the catalog as it stands. "
            "A slow or hung local inference server (BENCH_LOCAL_BASE_URL) is the usual cause — "
            "use POST /api/settings/providers/local/test to diagnose it.",
            MODELS_DISCOVERY_TIMEOUT_SECONDS,
        )
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
