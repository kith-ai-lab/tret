"""Workspace connections: OAuth links from a workspace to an external file
provider (Google Drive, Microsoft 365) that let a connected account's files be
imported into tret. This is the core service layer behind `api/connections.py`
— provider metadata, the OAuth client/credential seam, and the refresh-token
lifecycle. The router owns HTTP concerns (the signed `state`, the routes
themselves); this module owns everything that talks to a provider or the
`WorkspaceConnection` row.

**OAuth client credentials.** `get_oauth_client` resolves a provider's client
id/secret env-first (`TRET_GDRIVE_CLIENT_ID`/`SECRET`, `TRET_M365_CLIENT_ID`/
`SECRET` — config.py), then asks the extension registry
(`ext.add_oauth_client_provider`, engine/extensions.py) — the seam tret_cloud
uses to supply its own hosted OAuth app without this package ever importing
anything proprietary. A provider is "configured" (`GET
/api/connections/providers`) iff this returns non-None.

**Encryption.** `WorkspaceConnection.encrypted_refresh_token` is Fernet-
sealed with `services/credentials.py::get_fernet` — the exact same envelope
`ProviderCredential` uses, not a second key derivation.

**Egress.** Every outbound call here goes through `tret.net.build_client`,
never bare `httpx` (see `tret/net/client.py`'s module docstring — an `import
httpx` anywhere else under `tret/` fails `tests/test_egress_chokepoint.py`).
Modeled on `api/oidc.py`'s `_oidc_policy`/`services/mailer.py`'s
`_resend_policy`: a one-host allowlist scoped to whichever fixed provider
endpoint the call is reaching, gated only by the master `TRET_EGRESS` switch
— not one of the five operator-facing classes `GET /api/settings/egress`
reports, because every *first-hop* host below is a literal from
`PROVIDER_SPECS`, never a model- or document-chosen one, so `VERIFY_NONE`
applies for the same reason it does in those two modules. One exception:
`download_m365_file`'s redirect hops land on a host Microsoft's own response
named, not `PROVIDER_SPECS` — an allowlist pinned to that same host is
tautological, not a check, so those hops are additionally verified as public
addresses (`VERIFY_PUBLIC`, the SSRF check `tret/net/fetch/fetch.py` uses for
exactly the same "destination named by something other than fixed operator
config" reason) — see `_policy`'s own docstring.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from tret.config import get_settings
from tret.db.models import WorkspaceConnection
from tret.engine.extensions import get_extension_registry
from tret.net import EgressDenied, build_client
from tret.net.policy import VERIFY_NONE, VERIFY_PUBLIC, ClassPolicy, master_mode
from tret.services.credentials import get_fernet
from tret.services.documents import MAX_DOCUMENT_BYTES

log = logging.getLogger("tret.connections")

_EGRESS_CLASS = "connections"  # audit-log label only — see module docstring

GDRIVE = "gdrive"
M365 = "m365"
PROVIDERS = (GDRIVE, M365)


@dataclass(frozen=True)
class ProviderSpec:
    authorize_url: str
    token_url: str
    revoke_url: str | None  # None: no revoke endpoint — delete the row instead (m365)
    scopes: tuple[str, ...]
    authorize_extra: dict[str, str]


PROVIDER_SPECS: dict[str, ProviderSpec] = {
    GDRIVE: ProviderSpec(
        authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
        token_url="https://oauth2.googleapis.com/token",
        revoke_url="https://oauth2.googleapis.com/revoke",
        scopes=("openid", "email", "https://www.googleapis.com/auth/drive.file"),
        authorize_extra={"access_type": "offline", "prompt": "consent"},
    ),
    M365: ProviderSpec(
        authorize_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        token_url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
        revoke_url=None,
        scopes=("offline_access", "User.Read", "Files.Read.All", "Sites.Read.All"),
        authorize_extra={},
    ),
}

GRAPH_API_BASE = "https://graph.microsoft.com/v1.0"
GDRIVE_API_BASE = "https://www.googleapis.com/drive/v3"
_GRAPH_ME_URL = f"{GRAPH_API_BASE}/me"

# provider -> the Settings attrs holding its env-configured client id/secret.
_ENV_CLIENT_ATTRS: dict[str, tuple[str, str]] = {
    GDRIVE: ("gdrive_client_id", "gdrive_client_secret"),
    M365: ("m365_client_id", "m365_client_secret"),
}


@dataclass
class OAuthClientConfig:
    client_id: str
    client_secret: str


class ConnectionAuthError(Exception):
    """A connection's refresh token could not be exchanged for an access
    token, or an authorization code could not be exchanged for one.

    `error_code` carries the provider's own `error` field when the failure
    came back as a structured OAuth error response (`"invalid_grant"` is the
    one `get_access_token` acts on); `None` for a transport-level failure
    (egress denied, network error, malformed response) that never got that far.
    """

    def __init__(self, message: str, *, error_code: str | None = None):
        super().__init__(message)
        self.error_code = error_code


# ── OAuth client credentials ─────────────────────────────────────────────────
def get_oauth_client(provider: str) -> OAuthClientConfig | None:
    """This provider's OAuth app credentials, or None if nothing configures
    one. Env vars win when set; otherwise the extension registry is asked
    (see module docstring) — the same env-then-extension precedence
    `api/settings.py`'s provider keys use for LLM providers."""
    attrs = _ENV_CLIENT_ATTRS.get(provider)
    if attrs is not None:
        settings = get_settings()
        client_id = getattr(settings, attrs[0]).strip()
        client_secret = getattr(settings, attrs[1]).strip()
        if client_id and client_secret:
            return OAuthClientConfig(client_id=client_id, client_secret=client_secret)
    return get_extension_registry().get_oauth_client_config(provider)


# ── egress ────────────────────────────────────────────────────────────────────
def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _policy(url: str, *, verify_addresses: str = VERIFY_NONE) -> ClassPolicy:
    """A one-host allowlist scoped to `url`'s own host — see module
    docstring. `verify_addresses` defaults to `VERIFY_NONE`: for a fixed
    provider endpoint (Graph, Google) the allowlist above already pins the
    destination, so resolving would add a lookup without ruling out
    anything new. The one caller that passes `VERIFY_PUBLIC` explicitly is
    `download_m365_file`'s redirect hops — see its own comment for why an
    allowlist pinned to a redirect *target* is not a check at all, and the
    resolved-address check is what actually is one.

    `mode` reads the master switch (`TRET_EGRESS`) alone via `master_mode()`,
    not one of the five operator-facing egress classes (`GET /api/settings/
    egress`) — the same reasoning `api/oidc.py::_oidc_policy` gives for its
    own policy: every host reachable from here is a literal from
    `PROVIDER_SPECS` or a redirect off one, never a model- or document-chosen
    one, so there is nothing per-class to switch independently of "is this
    deployment on the internet at all". See `master_mode`'s own docstring for
    why it (not `master_is_off`) is the right thing to compare against for a
    destination outside `EGRESS_CLASSES`.
    """
    return ClassPolicy(
        name=_EGRESS_CLASS,
        mode=master_mode(),
        allow_hosts=frozenset({_host(url)}),
        allow_http=False,
        standard_ports_only=True,
        verify_addresses=verify_addresses,
        # Not read by build_client/open_client (the manual streaming reads
        # below cap by hand — see IMPORT_MAX_BYTES/_read_capped) — 0 here,
        # same as every other ClassPolicy in this codebase that doesn't feed
        # a fetcher which honours it, rather than a number that looks like a
        # real cap and isn't.
        max_bytes=0,
        timeout_seconds=15.0,
    )


async def _token_request(token_url: str, data: dict) -> dict:
    """POST `data` to a provider's token endpoint; the parsed JSON body on
    success. Raises `ConnectionAuthError` (with `error_code` set from the
    body's `error` field, when there is one) on anything else — egress
    denial, a network error, or a non-2xx response."""
    try:
        async with build_client(_EGRESS_CLASS, policy=_policy(token_url), timeout=15.0) as client:
            response = await client.post(
                token_url, data=data, headers={"Accept": "application/json"}
            )
    except EgressDenied as exc:
        raise ConnectionAuthError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ConnectionAuthError(f"could not reach the provider's token endpoint: {exc}") from exc

    try:
        body = response.json()
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    if response.status_code >= 400:
        error_code = body.get("error")
        detail = body.get("error_description")
        raise ConnectionAuthError(
            f"the provider's token endpoint returned {response.status_code}"
            f"{f' ({error_code})' if error_code else ''}: {detail or body}",
            error_code=error_code,
        )
    return body


# ── authorize / code exchange ────────────────────────────────────────────────
def authorize_url(provider: str, *, client: OAuthClientConfig, redirect_uri: str, state: str) -> str:
    spec = PROVIDER_SPECS[provider]
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(spec.scopes),
        "state": state,
        **spec.authorize_extra,
    }
    return f"{spec.authorize_url}?{urlencode(params)}"


def _decode_gdrive_email(id_token: str | None) -> str | None:
    """The `email` claim off an id_token's payload segment — decode only, no
    signature check: the token just came back from Google's own token
    endpoint over TLS (see module docstring), so there is no third party to
    defend against the way `api/oidc.py`'s callback must for an id_token that
    arrived via a browser redirect."""
    if not id_token or id_token.count(".") != 2:
        return None
    try:
        payload_b64 = id_token.split(".")[1]
        padded = payload_b64 + "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded))
    except Exception:
        log.warning("could not decode gdrive id_token payload for an account label", exc_info=True)
        return None
    email = payload.get("email") if isinstance(payload, dict) else None
    return str(email) if email else None


async def _fetch_m365_upn(access_token: str) -> str | None:
    """`userPrincipalName` from Graph `/me`, best-effort: a failure here must
    not fail the connect flow — the connection is still usable with
    `account_label=None`, just less legible in the connections list."""
    try:
        async with build_client(
            _EGRESS_CLASS, policy=_policy(_GRAPH_ME_URL), timeout=15.0
        ) as client:
            response = await client.get(
                _GRAPH_ME_URL, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            body = response.json()
    except (EgressDenied, httpx.HTTPError, ValueError) as exc:
        log.warning("could not fetch the m365 account label: %s", exc)
        return None
    upn = body.get("userPrincipalName") if isinstance(body, dict) else None
    return str(upn) if upn else None


@dataclass
class ExchangedToken:
    refresh_token: str
    granted_scopes: list[str]
    account_label: str | None


async def exchange_code(
    provider: str, *, client: OAuthClientConfig, code: str, redirect_uri: str
) -> ExchangedToken:
    """Trade an authorization `code` for a refresh token, the granted scopes,
    and (best-effort) the connected account's label. Raises
    `ConnectionAuthError` if the exchange itself fails or the provider did
    not return a refresh_token (it always should for a first-time consent
    with `access_type=offline`/`offline_access` in scope, as both providers'
    specs above request)."""
    spec = PROVIDER_SPECS[provider]
    body = await _token_request(
        spec.token_url,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client.client_id,
            "client_secret": client.client_secret,
        },
    )
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not access_token or not refresh_token:
        raise ConnectionAuthError(
            f"{provider} token exchange did not return both an access_token and a refresh_token"
        )

    scope_str = body.get("scope")
    granted_scopes = scope_str.split() if isinstance(scope_str, str) and scope_str else list(spec.scopes)

    if provider == GDRIVE:
        account_label = _decode_gdrive_email(body.get("id_token"))
    else:
        account_label = await _fetch_m365_upn(access_token)

    return ExchangedToken(
        refresh_token=refresh_token, granted_scopes=granted_scopes, account_label=account_label
    )


async def revoke_token(provider: str, refresh_token: str) -> None:
    """Best-effort revoke against the provider — gdrive only; m365's v2.0
    endpoint has no revoke endpoint, so `api/connections.py` deletes the row
    without calling this for that provider. Never raises: a revoke failing
    (already revoked, a network hiccup) must not block deleting the
    connection, which is the part of "disconnect" that actually matters."""
    spec = PROVIDER_SPECS.get(provider)
    if spec is None or spec.revoke_url is None:
        return
    try:
        async with build_client(
            _EGRESS_CLASS, policy=_policy(spec.revoke_url), timeout=15.0
        ) as client:
            response = await client.post(spec.revoke_url, data={"token": refresh_token})
            response.raise_for_status()
    except (EgressDenied, httpx.HTTPError) as exc:
        log.warning("best-effort %s token revoke failed: %s", provider, exc)


# ── the stored connection ────────────────────────────────────────────────────
async def get_connection(
    db: AsyncSession, workspace_id: uuid.UUID, provider: str
) -> WorkspaceConnection | None:
    return (
        await db.execute(
            select(WorkspaceConnection).where(
                WorkspaceConnection.workspace_id == workspace_id,
                WorkspaceConnection.provider == provider,
            )
        )
    ).scalars().first()


async def _commit_connection(db: AsyncSession, workspace_id: uuid.UUID, provider: str) -> None:
    """Commit a mutation `_refresh` just made to the `WorkspaceConnection`
    row, tolerating the row having been deleted out from under it.

    `_refresh` holds `conn` across an `await _token_request(...)` call that
    can take up to ~15s. `DELETE /api/connections/{provider}` (disconnect)
    takes no lock on the row, so it can delete it while that request is
    still in flight; the flush this commit triggers then emits an UPDATE
    matching 0 rows, and SQLAlchemy raises `StaleDataError` — deletion wins
    the race. That is exactly the state the `conn is None` branch at the top
    of `_refresh` already expresses as "workspace has no {provider}
    connection", so it is translated into the same `ConnectionAuthError`
    here rather than escaping as an unmapped 500 for every caller
    (`browse_m365`, `import_documents`, the picker token endpoint). The
    session is rolled back first so it stays usable by the caller afterward
    — `import_documents` keeps using the same `db` for later items after one
    item's refresh fails this way — and the cache entry is dropped
    defensively, though with the row gone there is nothing left to serve it
    for.
    """
    try:
        await db.commit()
    except StaleDataError as exc:
        await db.rollback()
        invalidate_access_token(workspace_id, provider)
        raise ConnectionAuthError(f"workspace has no {provider} connection") from exc


async def _refresh(db: AsyncSession, workspace_id: uuid.UUID, provider: str) -> dict:
    """Shared core of `_get_access_token`: load the connection, refresh it
    against the provider, persist a rotated refresh token when one comes
    back, and return the raw token response.

    If `disconnect` deletes the row while this function is holding it across
    the provider round trip, every commit below goes through
    `_commit_connection`, which turns the resulting `StaleDataError` into the
    same "no connection" `ConnectionAuthError` a fresh `get_connection` miss
    would raise, instead of letting it escape as an unmapped 500.

    A connection already in `status='error'` is refused immediately, before
    any provider call: once a refresh has failed with `invalid_grant` (or the
    stored token failed to decrypt), retrying against the provider on every
    subsequent browse/import can't succeed either — the row stays broken
    until an admin reconnects it via `POST /api/connections/{provider}/
    authorize` (which, like every other path that can change what `error`
    means for this connection, invalidates the access-token cache — see
    `invalidate_access_token`). This check is *not* in `get_connection`
    itself: callers like the connections list and `disconnect` need to keep
    working on an errored row.

    On `invalid_grant` (the connected account revoked access, or the refresh
    token itself expired) the row is flipped to `status='error'` with
    `error_detail` set and committed before `ConnectionAuthError` is raised.
    The same happens if the stored refresh token fails to decrypt at all
    (`InvalidToken` — most likely a `TRET_SECRET_KEY` rotation since the
    token was written): there is no refresh token to retry with, so this is
    exactly as unrecoverable without a reconnect as `invalid_grant` is. Both
    branches invalidate any cached access token for this (workspace,
    provider) before returning — see `_get_access_token`'s docstring for why
    that ordering is what keeps an errored connection from ever being served
    out of the cache.
    """
    conn = await get_connection(db, workspace_id, provider)
    if conn is None:
        raise ConnectionAuthError(f"workspace has no {provider} connection")
    if conn.status == "error":
        raise ConnectionAuthError(
            conn.error_detail
            or f"the {provider} connection is in an error state and must be reconnected"
        )
    spec = PROVIDER_SPECS.get(provider)
    if spec is None:
        raise ConnectionAuthError(f"unknown provider {provider!r}")
    client = get_oauth_client(provider)
    if client is None:
        raise ConnectionAuthError(f"{provider} OAuth client is not configured")

    try:
        refresh_token = get_fernet().decrypt(conn.encrypted_refresh_token).decode()
    except InvalidToken as exc:
        conn.status = "error"
        conn.error_detail = (
            f"the stored {provider} refresh token could not be decrypted, most "
            "likely because TRET_SECRET_KEY was rotated since it was saved; an "
            "admin must reconnect this connection"
        )
        try:
            await _commit_connection(db, workspace_id, provider)
        except ConnectionAuthError as stale:
            # Disconnect won the race — see _commit_connection's docstring.
            # Chained from the decrypt failure so the traceback still shows
            # why this connection was being flipped to error at all.
            raise stale from exc
        invalidate_access_token(workspace_id, provider)
        raise ConnectionAuthError(conn.error_detail) from exc
    try:
        body = await _token_request(
            spec.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client.client_id,
                "client_secret": client.client_secret,
            },
        )
    except ConnectionAuthError as exc:
        if exc.error_code == "invalid_grant":
            conn.status = "error"
            conn.error_detail = str(exc)
            try:
                await _commit_connection(db, workspace_id, provider)
            except ConnectionAuthError as stale:
                # Disconnect won the race — see _commit_connection's
                # docstring. Chained from the invalid_grant failure so the
                # traceback still shows why this connection was being
                # flipped to error at all.
                raise stale from exc
            invalidate_access_token(workspace_id, provider)
        raise

    if not body.get("access_token"):
        raise ConnectionAuthError(f"{provider} token endpoint did not return an access_token")

    # Microsoft always rotates the refresh token on every exchange; Google
    # occasionally does too. Persisted whenever the response carries one, so
    # a caller of either public function above never has to think about
    # rotation itself.
    new_refresh_token = body.get("refresh_token")
    if new_refresh_token:
        conn.encrypted_refresh_token = get_fernet().encrypt(new_refresh_token.encode())
    conn.refreshed_at = datetime.now(timezone.utc)
    await _commit_connection(db, workspace_id, provider)
    return body


@dataclass
class _CachedAccessToken:
    access_token: str
    expires_at: float  # time.monotonic() deadline


# Safety margin subtracted from a provider's `expires_in` before deciding
# whether (and for how long) to cache — a token cached right up to the edge
# of its real lifetime could be handed to a caller that then loses a race
# with the provider's own clock. An entry is only ever written when
# `expires_in - margin` is positive; anything shorter-lived than the margin
# itself is refreshed fresh on every call, same as before this cache existed.
_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS = 60

# Per-process cache of live access tokens, keyed by (workspace_id, provider).
# tret runs as a single instance (the strict instance lock — docs/
# hardening.md, fly.toml), so a process-local dict is the whole story: there
# is no second process for an entry to leak into or go stale across.
#
# Invariant this cache depends on: a cached entry is never served for a
# connection in `status='error'`. Every place that *sets* `status='error'`
# (both branches in `_refresh` above) also calls `invalidate_access_token`
# for the same key before returning, and nothing else writes a cache entry
# except a successful `_refresh` call — which cannot itself observe
# `status='error'` on the row it just successfully refreshed. So a hit here
# implies the connection was healthy as of the moment it was cached.
#
# A cache-pop alone cannot close the race with an *in-flight* refresh: caller
# A misses the cache, takes the lock, and blocks inside `await _refresh(...)`
# while a reconnect (OAuth callback) or `disconnect` commits a change and
# calls `invalidate_access_token` against what is, at that moment, an empty
# entry — a no-op. A's refresh then returns and writes its (now-stale)
# result into the cache anyway, and that stale entry would otherwise be
# served for up to `cache_for` seconds. `_access_token_epochs` closes this:
# every invalidation bumps the key's epoch, and `_get_access_token` only
# writes the cache if the epoch is unchanged from the one it captured right
# before starting the refresh — so a write that raced an invalidation is
# simply dropped, while the caller still gets its freshly-minted token back.
_access_token_cache: dict[tuple[uuid.UUID, str], _CachedAccessToken] = {}

# Bumped by every `invalidate_access_token` call. See the comment above
# `_access_token_cache` for why a plain pop is not enough to close the
# reconnect/disconnect-lands-during-an-in-flight-refresh window.
_access_token_epochs: dict[tuple[uuid.UUID, str], int] = {}

# One `asyncio.Lock` per (workspace_id, provider), created on demand, so
# concurrent callers asking for the same connection's token serialize on a
# single refresh rather than each starting their own — the concurrent-
# refresh race this cache exists to close (see module-level notes on why a
# lost refresh-token write matters for Microsoft's rotating tokens).
_access_token_locks: dict[tuple[uuid.UUID, str], asyncio.Lock] = {}


def _access_token_lock(key: tuple[uuid.UUID, str]) -> asyncio.Lock:
    lock = _access_token_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _access_token_locks[key] = lock
    return lock


def invalidate_access_token(workspace_id: uuid.UUID, provider: str) -> None:
    """Drop this (workspace, provider)'s cached access token, if any, and
    bump its invalidation epoch.

    Called wherever a token this process might be holding could stop being
    good for the connection it was cached against: `_refresh` flipping the
    row to `status='error'`, `DELETE /api/connections/{provider}`
    (disconnect — nothing should be served for a connection that no longer
    exists), and the OAuth callback that (re)creates or reactivates a
    connection (a reconnect may attach an entirely different provider
    account, so a token minted under the old grant must never be handed out
    as if it were still valid for the new one).

    The epoch bump is what makes this more than a pop: a plain pop only
    protects a cache entry that already exists. If this call lands while
    another caller is blocked *inside* a refresh for the same key (the cache
    was already empty, so the pop above is a no-op), that caller's refresh
    still returns a token it is about to cache — one minted against
    whatever the connection was before this invalidation. Bumping the epoch
    here means `_get_access_token` (which captured the epoch before it
    started refreshing) will see a mismatch and skip writing that stale
    result to the cache.
    """
    _access_token_cache.pop((workspace_id, provider), None)
    key = (workspace_id, provider)
    _access_token_epochs[key] = _access_token_epochs.get(key, 0) + 1


def clear_access_token_cache() -> None:
    """Drop every cached access token, epoch, and per-key lock. Test
    isolation; also safe to call at process startup, though an empty
    process-local dict already starts empty."""
    _access_token_cache.clear()
    _access_token_epochs.clear()
    _access_token_locks.clear()


async def _get_access_token(
    db: AsyncSession, workspace_id: uuid.UUID, provider: str
) -> tuple[str, int]:
    """Shared core of `get_access_token`/`get_access_token_with_expiry`: a
    cached, still-valid access token when there is one, otherwise a real
    refresh through `_refresh` — cached afterward for its own remaining
    lifetime less `_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS`, so the next call in
    that window (a folder click during `browse_m365`, most commonly) never
    starts a second refresh-token exchange for a token that is already good.

    Returns `(access_token, expires_in)`: on a cache hit, the seconds
    remaining on the cached entry — the token's own lifetime less
    `_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS`, not the seconds left on the
    provider's original grant (see the docstring on the caller-facing
    functions); on a miss, the provider's own `expires_in` verbatim,
    unchanged from before this cache existed.

    The lock is acquired *after* the first (lock-free) cache check — a
    cheap, uncontended read for the overwhelmingly common case — and the
    cache is checked again immediately after acquiring it: a caller that
    waited on the lock may find another caller already refreshed and cached
    a token while it waited, and should use that rather than refreshing a
    second time. See the module-level comment above `_access_token_cache`
    for the invariant that makes a cache hit here safe to serve even without
    re-reading the connection row's `status`.

    The epoch captured just before `_refresh` guards against a narrower race
    than the lock does: an invalidation (reconnect, disconnect) landing
    *while* this call is blocked inside `_refresh` itself. Such an
    invalidation cannot pop an entry that does not exist yet, so it bumps
    the epoch instead — and the write below is skipped when the epoch has
    moved, so a token minted against a connection that was reconnected or
    disconnected mid-refresh is still returned to this caller (whoever asked
    gets an answer) but is never handed to anyone else out of the cache.
    """
    key = (workspace_id, provider)
    now = time.monotonic()
    cached = _access_token_cache.get(key)
    if cached is not None and cached.expires_at > now:
        return cached.access_token, max(1, int(cached.expires_at - now))

    async with _access_token_lock(key):
        now = time.monotonic()
        cached = _access_token_cache.get(key)
        if cached is not None and cached.expires_at > now:
            return cached.access_token, max(1, int(cached.expires_at - now))

        epoch = _access_token_epochs.get(key, 0)
        body = await _refresh(db, workspace_id, provider)
        access_token = body["access_token"]
        try:
            expires_in = int(body.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600

        cache_for = expires_in - _ACCESS_TOKEN_SAFETY_MARGIN_SECONDS
        if cache_for > 0 and _access_token_epochs.get(key, 0) == epoch:
            _access_token_cache[key] = _CachedAccessToken(
                access_token=access_token, expires_at=time.monotonic() + cache_for
            )
        return access_token, expires_in


async def get_access_token(db: AsyncSession, workspace_id: uuid.UUID, provider: str) -> str:
    """The workspace's current `provider` access token — a cached one if a
    still-valid access token was minted recently enough (see
    `_get_access_token`), otherwise a fresh refresh of the stored refresh
    token. The cache is per-process and bounded to the access token's own
    lifetime (see `_access_token_cache`'s module-level comment for why that
    is enough on tret's single-instance deployment), so this is still safe
    for short-lived, per-request use (the picker-token endpoint, browse,
    import) without either a hot loop of refreshes or a token outliving the
    provider's own grant.
    """
    access_token, _expires_in = await _get_access_token(db, workspace_id, provider)
    return access_token


async def get_access_token_with_expiry(
    db: AsyncSession, workspace_id: uuid.UUID, provider: str
) -> tuple[str, int]:
    """Same as `get_access_token`, additionally returning how long the
    access token is good for — what `GET /api/connections/{provider}/token`
    hands the client-side Picker so it knows how long the token is good for.
    On a cache hit this is the seconds remaining on the cached entry (the
    token's own lifetime less `_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS`), not the
    original grant length; on a miss (including anything not cached — see
    `_ACCESS_TOKEN_SAFETY_MARGIN_SECONDS`) it is the provider's own
    `expires_in` verbatim, falling back to 3600s (both providers' actual
    default) if a provider ever omits the field."""
    return await _get_access_token(db, workspace_id, provider)


# ── m365 browse (Phase 1) ────────────────────────────────────────────────────
async def _graph_get(access_token: str, path: str, *, params: dict | None = None) -> dict:
    """GET one Graph path, bearer-authenticated. Raises `RuntimeError` on
    anything that keeps the call from completing (egress denial, a network
    error, or a non-2xx response) — a transport failure, not an auth one, so
    it deliberately does not use `ConnectionAuthError`: that type is reserved
    for `get_access_token` itself, which is what maps to the 409 a caller
    should read as "reconnect this provider"."""
    url = f"{GRAPH_API_BASE}{path}"
    try:
        async with build_client(_EGRESS_CLASS, policy=_policy(url), timeout=15.0) as client:
            response = await client.get(
                url, params=params, headers={"Authorization": f"Bearer {access_token}"}
            )
            response.raise_for_status()
            return response.json()
    except EgressDenied as exc:
        raise RuntimeError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Microsoft Graph: {exc}") from exc


def _m365_site_out(site: dict) -> dict:
    return {"id": site.get("id"), "name": site.get("displayName") or site.get("name") or "", "kind": "site"}


def _m365_drive_out(drive: dict, *, site_id: str) -> dict:
    return {
        "id": drive.get("id"),
        "name": drive.get("name") or "",
        "kind": "drive",
        "drive_id": drive.get("id"),
        "site_id": site_id,
    }


def _m365_child_out(item: dict, *, drive_id: str) -> dict:
    is_folder = "folder" in item
    out = {
        "id": item.get("id"),
        "name": item.get("name") or "",
        "kind": "folder" if is_folder else "file",
        "drive_id": drive_id,
    }
    if not is_folder:
        out["mime_type"] = (item.get("file") or {}).get("mimeType")
        out["size"] = item.get("size")
    modified_at = item.get("lastModifiedDateTime")
    if modified_at:
        out["modified_at"] = modified_at
    return out


async def browse_m365(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    *,
    scope: str = "sites",
    site_id: str | None = None,
    drive_id: str | None = None,
    item_id: str | None = None,
) -> list[dict]:
    """Server-side Graph browsing for the m365 import picker.

    `scope="sites"` (the default) lists every site the connected account can
    reach plus a pseudo-entry for their own OneDrive — its `drive_id` is
    resolved here (via `/me/drive`) so the frontend can drill straight into
    it with `scope=drive_children` like any other drive, no special-cased id.
    `scope="drive_children"` lists what is one level inside whichever id the
    caller drilled into: a bare `site_id` lists that site's drives; a
    `drive_id` (with an optional `item_id`) lists a drive's or folder's
    children. Each navigation calls `get_access_token` again, but a short
    lived per-process cache (see that function's docstring) means clicking
    through several folders in a row does not each time trade a refresh
    token for a fresh access token — only the first call in the cache's
    window does. Raises `ConnectionAuthError` (propagated straight from
    `get_access_token`) if the m365 connection cannot be refreshed, and
    `ValueError` for a scope/params combination that names nothing.
    """
    token = await get_access_token(db, workspace_id, M365)
    if scope == "sites":
        body = await _graph_get(token, "/sites", params={"search": "*"})
        items = [_m365_site_out(s) for s in body.get("value", [])]
        my_drive = await _graph_get(token, "/me/drive", params={"$select": "id,name"})
        if my_drive.get("id"):
            items.append(
                {"id": my_drive["id"], "name": "OneDrive", "kind": "drive", "drive_id": my_drive["id"]}
            )
        return items
    if scope == "drive_children":
        if drive_id:
            # `quote(..., safe="")` — every id below is caller-supplied
            # (a query param on GET /api/connections/m365/browse) and lands
            # straight in a URL path segment; unescaped, a value containing
            # "/" (or "../") would let it name a different Graph path than
            # the one this call is scoped to.
            path = (
                f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/children"
                if item_id
                else f"/drives/{quote(drive_id, safe='')}/root/children"
            )
            body = await _graph_get(token, path)
            return [_m365_child_out(c, drive_id=drive_id) for c in body.get("value", [])]
        if site_id:
            body = await _graph_get(token, f"/sites/{quote(site_id, safe='')}/drives")
            return [_m365_drive_out(d, site_id=site_id) for d in body.get("value", [])]
        raise ValueError("scope=drive_children requires site_id or drive_id")
    raise ValueError(f"unknown browse scope {scope!r}")


# ── document import (Phase 1) ────────────────────────────────────────────────
# The single cap `services/documents.py::MAX_DOCUMENT_BYTES` defines — kept
# under this name because `api/documents.py` imports it as `IMPORT_MAX_BYTES`
# and every message/test here already reads by that name. See that
# constant's own comment for why one number covers both the upload and
# import paths.
IMPORT_MAX_BYTES = MAX_DOCUMENT_BYTES
_DOWNLOAD_CHUNK_BYTES = 256 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 60.0
_MAX_DOWNLOAD_REDIRECTS = 4

# Google-native files `files.get?alt=media` refuses to serve raw — exported
# via `files.export` instead, to the office format the contract names.
GDRIVE_EXPORT_MIME: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", "docx",
    ),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "xlsx",
    ),
    "application/vnd.google-apps.presentation": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation", "pptx",
    ),
}


class DownloadTooLargeError(Exception):
    """A provider file exceeded the import size cap — from its declared size
    or from the streamed byte count once no size was declared. A distinct
    type so a caller can read "too big" out of an import failure if it ever
    wants to, though the import endpoint currently just stringifies it like
    any other per-item error."""


def gdrive_download_content_type(mime_type: str | None) -> str | None:
    """The content type of the bytes `download_gdrive_file` actually
    returns: the export mime type for a Google-native file (what the caller
    stores/renders — Drive's own `mimeType` for it names the *editor*, not
    the exported document), else `mime_type` unchanged."""
    export = GDRIVE_EXPORT_MIME.get(mime_type or "")
    return export[0] if export else mime_type


def gdrive_export_filename(name: str, mime_type: str | None) -> str:
    """`name` with the right office extension appended for a Google-native
    file being exported — so it lands in `ingest_document` as `Report.docx`
    rather than `Report`, which is what `services/documents.py::extract_text`
    dispatches its parser on. Any other file (already has a real extension
    from Drive) passes through unchanged."""
    export = GDRIVE_EXPORT_MIME.get(mime_type or "")
    if not export:
        return name
    _export_mime, ext = export
    return name if name.lower().endswith(f".{ext}") else f"{name}.{ext}"


async def gdrive_file_metadata(access_token: str, *, file_id: str) -> dict:
    """`{id, name, mimeType, size, modifiedTime}` for one Drive file — the
    pre-download size check and the mime type the import path needs to
    decide raw download vs. `files.export`, plus `modified_at` provenance."""
    url = f"{GDRIVE_API_BASE}/files/{quote(file_id, safe='')}"
    try:
        async with build_client(_EGRESS_CLASS, policy=_policy(url), timeout=15.0) as client:
            response = await client.get(
                url,
                params={"fields": "id,name,mimeType,size,modifiedTime"},
                headers={"Authorization": f"Bearer {access_token}"},
            )
            response.raise_for_status()
            return response.json()
    except EgressDenied as exc:
        raise RuntimeError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Google Drive: {exc}") from exc


async def m365_item_metadata(access_token: str, *, drive_id: str, item_id: str) -> dict:
    """`{id, name, size, lastModifiedDateTime, file}` for one drive item —
    same role as `gdrive_file_metadata`: the pre-download size check and the
    `modified_at` provenance field."""
    return await _graph_get(
        access_token,
        f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}",
        params={"$select": "id,name,size,lastModifiedDateTime,file,folder"},
    )


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """The body of an already-2xx streaming `response`, refused mid-stream if
    it would exceed `max_bytes` — the backstop half of the size cap, for
    whichever of the two providers did not declare (or lied about) a size."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise DownloadTooLargeError(
            f"{int(declared) // (1024 * 1024)}MB exceeds the {max_bytes // (1024 * 1024)}MB import limit"
        )
    body = bytearray()
    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise DownloadTooLargeError(
                f"exceeds the {max_bytes // (1024 * 1024)}MB import limit"
            )
    return bytes(body)


async def download_gdrive_file(
    access_token: str, *, file_id: str, mime_type: str | None, max_bytes: int = IMPORT_MAX_BYTES
) -> bytes:
    """A Drive file's bytes: `files.export` for a Google-native mime type
    (Docs/Sheets/Slides — the only three the contract maps), `files.get?
    alt=media` for everything else. Raises `ValueError` for a Google-native
    type with no export mapping (Forms, Sites, Apps Script, ...)."""
    export = GDRIVE_EXPORT_MIME.get(mime_type or "")
    if (mime_type or "").startswith("application/vnd.google-apps.") and not export:
        raise ValueError(f"Google file type {mime_type!r} cannot be exported for import")
    quoted_file_id = quote(file_id, safe="")
    if export:
        export_mime, _ext = export
        url = f"{GDRIVE_API_BASE}/files/{quoted_file_id}/export"
        params = {"mimeType": export_mime}
    else:
        url = f"{GDRIVE_API_BASE}/files/{quoted_file_id}"
        params = {"alt": "media"}
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        async with build_client(_EGRESS_CLASS, policy=_policy(url), timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client:
            async with client.stream("GET", url, params=params, headers=headers) as response:
                response.raise_for_status()
                return await _read_capped(response, max_bytes)
    except EgressDenied as exc:
        raise RuntimeError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Google Drive: {exc}") from exc


async def download_m365_file(
    access_token: str, *, drive_id: str, item_id: str, max_bytes: int = IMPORT_MAX_BYTES
) -> bytes:
    """A drive item's content. Graph's `/content` endpoint 302s to a
    pre-signed download URL on a different host, so the redirect is followed
    by hand — same reasoning as `net/fetch/fetch.py`'s own redirect loop —
    and each hop gets its own single-host policy (`_policy(url)`), since a
    client built for graph.microsoft.com must not also be trusted with
    whatever host the redirect names.

    The first hop's policy is `VERIFY_NONE`: `url` still names Graph's own
    fixed endpoint, a `PROVIDER_SPECS`-derived host the allowlist already
    pins. Every hop after that lands on a host *Microsoft's response* named,
    not this module's own config — a policy whose `allow_hosts` is
    recomputed from that same untrusted `url` is not an allowlist at all, it
    is an assertion that whatever the redirect says is fine. So hop 2 onward
    additionally verifies the resolved address is public (`VERIFY_PUBLIC`),
    the same SSRF check `net/fetch/fetch.py::fetch_page` applies to *its*
    redirects, for the same reason: closing off a pre-signed "download" URL
    that actually points at the deployment's own metadata endpoint or LAN.
    The bearer token is sent to Graph only: it is dropped before following
    the redirect, since a pre-signed URL needs none and a Graph access token
    has no business leaving Microsoft's host.
    """
    url = f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/content"
    headers = {"Authorization": f"Bearer {access_token}"}
    for hop in range(_MAX_DOWNLOAD_REDIRECTS):
        verify = VERIFY_NONE if hop == 0 else VERIFY_PUBLIC
        try:
            async with build_client(
                _EGRESS_CLASS, policy=_policy(url, verify_addresses=verify), timeout=_DOWNLOAD_TIMEOUT_SECONDS
            ) as client:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise RuntimeError("Microsoft Graph redirected with no Location header")
                        url = str(httpx.URL(url).join(location))
                        headers = {}
                        continue
                    response.raise_for_status()
                    return await _read_capped(response, max_bytes)
        except EgressDenied as exc:
            raise RuntimeError(f"connections egress is unavailable: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"could not reach Microsoft Graph: {exc}") from exc
    raise RuntimeError(f"more than {_MAX_DOWNLOAD_REDIRECTS} redirects downloading the m365 file")
