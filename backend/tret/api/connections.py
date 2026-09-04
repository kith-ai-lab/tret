"""Workspace connections: connect a workspace to Google Drive or Microsoft
365, list/inspect the connections it has, disconnect one, and mint a
short-lived access token for the Google Picker. Phase 0 only — the
server-side Graph browse endpoint and the document-import endpoint (Phase 1)
land in a later change.

**No server session for the OAuth dance**, same reasoning as `api/oidc.py`'s
own module docstring: `POST /{provider}/authorize` runs inside an
authenticated session and can stamp identity straight into a signed `state`
(itsdangerous, its own salt, its own short `max_age`) — workspace_id, user_id,
provider, a nonce — so `GET /callback`, which is unauthenticated (there is no
session left by the time the provider redirects back), can recover exactly
who this connection is for without one.

**`state`'s signature alone does not bind it to a browser** — same gap
`api/oidc.py`'s own docstring walks through, and the same fix. A signed
`state` proves this router minted it at some point, not that it was minted
*for the browser presenting it*. Without more, an admin of workspace W1 could
start their own `/authorize` (getting back a validly-signed `state` naming
their own `(W1, self)` plus a `code` for a provider account they control),
then lure an admin of some other workspace W2 into finishing it — e.g. as a
link in an email ("approve our new integration"). W2's admin's browser
carries no cookie the callback checks, so nothing distinguishes "the browser
that started this authorize call" from "any browser that received the URL":
the callback happily upserts the connection into **W1**, exactly as the
`state` says, binding the attacker's provider account to a workspace the
victim never intended to grant it to (the connected account is then live for
every member of W1 to import from, per `docs/connections.md`'s "whole-
workspace access" note). The victim clicking the link is not consent to
anything — they never saw a `POST /authorize`, an admin-role check, or a
provider consent screen naming *their* workspace.

The fix is the same random per-attempt `sid`, minted in `authorize`, carried
in two independent channels that must agree: inside the signed `state` (so it
can't be forged) and in a short-lived, `httpOnly` cookie scoped to this
router's callback path (so only the browser `authorize` actually set it in
can present it — an attacker relaying a URL cannot also plant their target's
cookie jar). `callback` requires the two to match before doing anything else
that costs a round trip, and clears the cookie on every exit path so a
captured, already-used callback URL cannot be replayed a second time even by
the browser that legitimately started it.

Redirect URI registered with each provider: `{TRET_APP_URL}/api/connections/
callback` — built from `TRET_APP_URL`, this deployment's own explicit public
base URL (config.py), the same setting `api/workspaces.py` uses for a link
handed to an external party (an invite email). Never inferred from the
incoming request: behind a TLS-terminating proxy `request.url` cannot be
trusted any more than `api/oidc.py`'s own redirect_uri derivation trusts
`X-Forwarded-Proto` (see that module's config.py setting).
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.api.auth import current_user
from tret.api.workspace import ROLE_RANK, WorkspaceContext, current_workspace, require_workspace_admin
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import User, WorkspaceConnection, WorkspaceMember
from tret.engine.extensions import get_extension_registry
from tret.services.connections import (
    GDRIVE,
    M365,
    PROVIDER_SPECS,
    ConnectionAuthError,
    authorize_url as build_authorize_url,
    browse_m365,
    exchange_code,
    get_access_token_with_expiry,
    get_connection,
    get_oauth_client,
    revoke_token,
)
from tret.services.credentials import get_fernet

log = logging.getLogger("tret.connections")

router = APIRouter(prefix="/api/connections", tags=["connections"])

STATE_SALT = "tret-connections-state"
# 10 minutes: long enough for a consent screen, short enough that a captured
# callback URL is stale fast — the same window api/oidc.py uses.
STATE_MAX_AGE = 600
# Binds `state` to the browser that started this authorize call — see module
# docstring. Scoped to the callback path alone (below), same as api/oidc.py
# scopes its own sid cookie to its own router path.
SID_COOKIE = "tret_conn_sid"
_CALLBACK_PATH = "/api/connections/callback"

# Where the frontend's connections settings page lives — every redirect back
# to the browser after the callback (success or failure) lands here with a
# query param it reads (see docstring / GET /callback below).
_CONNECTIONS_PAGE = "/settings/connections"


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().secret_key, salt=STATE_SALT)


def _redirect_uri() -> str:
    return f"{get_settings().app_url.rstrip('/')}/api/connections/callback"


def _provider_or_404(provider: str) -> None:
    if provider not in PROVIDER_SPECS:
        raise HTTPException(404, f"unknown provider {provider!r}; must be one of {list(PROVIDER_SPECS)}")


def _connection_out(conn: WorkspaceConnection) -> dict:
    return {
        "provider": conn.provider,
        "status": conn.status,
        "account_label": conn.account_label,
        "granted_scopes": conn.granted_scopes,
        "connected_at": conn.created_at,
        "refreshed_at": conn.refreshed_at,
        "error_detail": conn.error_detail,
    }


def _error_redirect(code: str) -> RedirectResponse:
    """Every callback failure exits through here — cookie cleared on every
    one of them (not just the success path) so a stale sid never lingers in
    the browser past the one authorize attempt it was minted for."""
    resp = RedirectResponse(f"{_CONNECTIONS_PAGE}?error={code}", status_code=302)
    resp.delete_cookie(SID_COOKIE, path=_CALLBACK_PATH)
    return resp


@router.get("")
async def list_connections(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(WorkspaceConnection).where(WorkspaceConnection.workspace_id == ctx.id)
        )
    ).scalars().all()
    return {"connections": [_connection_out(c) for c in rows]}


@router.get("/providers")
async def list_providers(
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    connected = set(
        (
            await db.execute(
                select(WorkspaceConnection.provider).where(
                    WorkspaceConnection.workspace_id == ctx.id
                )
            )
        ).scalars().all()
    )
    settings = get_settings()
    out = []
    for provider in (GDRIVE, M365):
        picker = None
        if provider == GDRIVE and settings.gdrive_picker_api_key and settings.gdrive_app_id:
            picker = {"api_key": settings.gdrive_picker_api_key, "app_id": settings.gdrive_app_id}
        out.append(
            {
                "provider": provider,
                "configured": get_oauth_client(provider) is not None,
                "connected": provider in connected,
                "picker": picker,
            }
        )
    return {"providers": out}


@router.post("/{provider}/authorize")
async def authorize(
    provider: str,
    response: Response,
    user: User = Depends(current_user),
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    _provider_or_404(provider)
    client = get_oauth_client(provider)
    if client is None:
        raise HTTPException(
            503,
            f"{provider} is not configured — set TRET_{provider.upper()}_CLIENT_ID/"
            f"TRET_{provider.upper()}_CLIENT_SECRET",
        )

    gate = await get_extension_registry().check_workspace_gate(db, ctx.id, "connections.connect")
    if not gate.allowed:
        raise HTTPException(403, detail={"reason": gate.reason, "detail": gate.detail})

    sid = secrets.token_urlsafe(24)  # binds `state` to this browser — see module docstring
    state = _state_serializer().dumps(
        {
            "workspace_id": str(ctx.id),
            "user_id": str(user.id),
            "provider": provider,
            "nonce": secrets.token_urlsafe(16),
            "sid": sid,
        }
    )
    url = build_authorize_url(provider, client=client, redirect_uri=_redirect_uri(), state=state)
    response.set_cookie(
        SID_COOKIE,
        sid,
        max_age=STATE_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=get_settings().cookie_secure,
        path=_CALLBACK_PATH,
    )
    return {"authorize_url": url}


@router.get("/callback")
async def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(get_db),
):
    if error:
        log.info("connections callback: provider returned an error: %s", error)
        return _error_redirect("provider_denied")
    if not code or not state:
        return _error_redirect("missing_code_or_state")

    try:
        unpacked = _state_serializer().loads(state, max_age=STATE_MAX_AGE)
    except BadSignature:
        return _error_redirect("bad_state")
    if not isinstance(unpacked, dict):
        return _error_redirect("bad_state")

    provider = unpacked.get("provider")
    try:
        workspace_id = uuid.UUID(str(unpacked.get("workspace_id")))
        user_id = uuid.UUID(str(unpacked.get("user_id")))
    except (TypeError, ValueError):
        return _error_redirect("bad_state")
    if provider not in PROVIDER_SPECS:
        return _error_redirect("bad_state")

    # `state`'s signature proves this router minted it; it does not prove
    # this browser is the one `authorize` minted it for. The `sid` cookie
    # `authorize` set (httpOnly, scoped to this path) is the second,
    # independent channel that does — see the module docstring. Checked
    # before any external call so a lured browser presenting someone else's
    # (code, state) fails immediately rather than after a wasted token
    # exchange.
    sid = unpacked.get("sid")
    sid_cookie = request.cookies.get(SID_COOKIE)
    if not sid or not sid_cookie or not secrets.compare_digest(sid_cookie, sid):
        return _error_redirect("bad_state")

    # Re-check, right before doing anything with the provider, that
    # `state`'s user_id is *still* an admin/owner of `state`'s workspace_id —
    # the same role predicate `require_workspace_admin` enforces at
    # `/authorize` time. A `state` signed minutes ago for an admin who has
    # since been demoted (or removed) must not still be able to attach a
    # connection to that workspace.
    member = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.user_id == user_id,
                WorkspaceMember.workspace_id == workspace_id,
            )
        )
    ).scalars().first()
    if member is None or ROLE_RANK.get(member.role, -1) < ROLE_RANK["admin"]:
        return _error_redirect("bad_state")

    client = get_oauth_client(provider)
    if client is None:
        return _error_redirect("not_configured")

    try:
        exchanged = await exchange_code(
            provider, client=client, code=code, redirect_uri=_redirect_uri()
        )
    except ConnectionAuthError as exc:
        log.warning("connections callback: %s token exchange failed: %s", provider, exc)
        return _error_redirect("exchange_failed")

    conn = await get_connection(db, workspace_id, provider)
    if conn is None:
        conn = WorkspaceConnection(workspace_id=workspace_id, provider=provider)
        db.add(conn)
    conn.account_label = exchanged.account_label
    conn.encrypted_refresh_token = get_fernet().encrypt(exchanged.refresh_token.encode())
    conn.granted_scopes = exchanged.granted_scopes
    conn.status = "active"
    conn.error_detail = None
    conn.connected_by = user_id
    conn.refreshed_at = datetime.now(timezone.utc)
    await db.commit()

    resp = RedirectResponse(f"{_CONNECTIONS_PAGE}?connected={provider}", status_code=302)
    resp.delete_cookie(SID_COOKIE, path=_CALLBACK_PATH)
    return resp


@router.delete("/{provider}")
async def disconnect(
    provider: str,
    ctx: WorkspaceContext = Depends(require_workspace_admin),
    db: AsyncSession = Depends(get_db),
):
    _provider_or_404(provider)
    conn = await get_connection(db, ctx.id, provider)
    if conn is None:
        return {"ok": True}
    if provider == GDRIVE:
        try:
            refresh_token = get_fernet().decrypt(conn.encrypted_refresh_token).decode()
            await revoke_token(provider, refresh_token)
        except Exception:
            # Best-effort: a broken decrypt or a revoke the provider refuses
            # must never block deleting the row, which is what actually
            # disconnects the workspace.
            log.warning("gdrive token revoke failed during disconnect", exc_info=True)
    await db.delete(conn)
    await db.commit()
    return {"ok": True}


@router.get("/{provider}/token")
async def connection_token(
    provider: str,
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """A short-lived Google access token for the client-side Picker.

    The frontend loads `https://apis.google.com/js/api.js` on demand and
    calls Google's Picker API directly with this token (plus the developer
    key / app id from `GET /api/connections/providers`'s `picker` field) —
    the token never round-trips through this backend again after this call.
    gdrive only: m365's browsing goes through the server-side `/api/
    connections/m365/browse` endpoint (Phase 1) instead of a client-side SDK,
    so there is nothing for a client to do with an m365 access token.
    """
    if provider != GDRIVE:
        raise HTTPException(404, f"no client-side token for provider {provider!r}")
    conn = await get_connection(db, ctx.id, provider)
    if conn is None:
        raise HTTPException(404, f"workspace has no {provider} connection")
    try:
        access_token, expires_in = await get_access_token_with_expiry(db, ctx.id, provider)
    except ConnectionAuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"access_token": access_token, "expires_in": expires_in}


@router.get("/m365/browse")
async def m365_browse(
    scope: str = "sites",
    site_id: str | None = None,
    drive_id: str | None = None,
    item_id: str | None = None,
    ctx: WorkspaceContext = Depends(current_workspace),
    db: AsyncSession = Depends(get_db),
):
    """Server-side Graph browsing for the m365 import picker (Phase 1) — see
    `services/connections.py::browse_m365` for the scope/params contract.
    Any workspace member: browsing does not touch the stored token or the
    connection row, only reads through it, so this needs no admin gate.

    A `ConnectionAuthError` here means the same thing it means at `GET
    /{provider}/token` above: the m365 connection needs to be reconnected —
    409, same as that route, for the same "actionable conflict" reason.
    """
    try:
        items = await browse_m365(
            db, ctx.id, scope=scope, site_id=site_id, drive_id=drive_id, item_id=item_id
        )
    except ConnectionAuthError as exc:
        raise HTTPException(409, f"the m365 connection needs to be reconnected: {exc}") from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return {"items": items}
