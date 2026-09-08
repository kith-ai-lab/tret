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
import hashlib
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

import httpx
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from tret.config import get_settings
from tret.db.models import ConnectionActivity, Document, WorkspaceConnection
from tret.engine.extensions import get_extension_registry
from tret.net import EgressDenied, build_client
from tret.net.policy import MODE_OFF, VERIFY_NONE, VERIFY_PUBLIC, ClassPolicy, master_mode
from tret.services.credentials import get_fernet
from tret.services.documents import MAX_DOCUMENT_BYTES, ingest_document

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

# The scopes an m365 connection needs to write back to SharePoint/OneDrive —
# a strict superset of PROVIDER_SPECS[M365].scopes (offline_access/User.Read
# unchanged; Files.Read.All/Sites.Read.All widened to their ReadWrite
# counterparts). Never the *default* scope set a bare `authorize` requests —
# see SCOPE_SETS and `POST /{provider}/authorize`'s optional `scope_set`
# body in api/connections.py — so a workspace that only ever wanted read
# access is never asked to grant more than that.
M365_WRITE_SCOPES = ("offline_access", "User.Read", "Files.ReadWrite.All", "Sites.ReadWrite.All")

# What `POST /api/connections/{provider}/authorize`'s optional `scope_set`
# body selects between — m365 only (gdrive's `drive.file` grant is already
# read/write per-file by construction, see docs/connections.md's Security
# notes, so there is no separate "write" scope set for it to request; the
# route 400s a gdrive `scope_set="write"` rather than looking it up here).
SCOPE_SETS: dict[str, tuple[str, ...]] = {"read": PROVIDER_SPECS[M365].scopes, "write": M365_WRITE_SCOPES}

# The two workspace-gate actions `api/connections.py` (`require_connections_
# gate`, for the HTTP surface) and `ensure_connection_usable` below (for
# anything — the engine included — that needs a plain "is this connection
# usable right now" answer with no HTTP layer downstream to turn a refusal
# into) both ask `check_workspace_gate` about. Defined here, not in
# api/connections.py, so both call sites share the one literal rather than
# `services/connections.py` importing them back from the router package it
# is itself imported by (a cycle) or the two modules drifting to different
# strings. `api/connections.py` re-exports both names unchanged — `api/
# documents.py` already imports `USE_ACTION` from there, and that import
# keeps working exactly as before.
#
# `CONNECT_ACTION` is the front door — asked once, in `authorize`, at the
# moment a workspace is about to gain a new connection. `USE_ACTION` is asked
# on every route (or call) that exercises a connection *already on file*: a
# gate registered for `CONNECT_ACTION` alone would only ever stop a workspace
# from connecting in the first place, and once connected the row just sits
# there working forever, plan or no plan. Asking `USE_ACTION` every time
# means a workspace whose plan lapses loses the connection's usefulness on
# its very next call. No code here needs to revoke the provider token or
# mutate the stored `WorkspaceConnection` row to make that happen, and none
# of it does; the row is left exactly as it was. If the plan comes back,
# `check_workspace_gate` starts returning `allowed=True` again and the same
# row works again, with nothing to reconnect.
CONNECT_ACTION = "connections.connect"  # asked once, when a connection is established
USE_ACTION = "connections.use"  # asked every time a stored connection is exercised
# Asked by `upload_connected_file` on every write-back call, in addition to
# (never instead of) `USE_ACTION` — `ensure_connection_usable` already asks
# `USE_ACTION` as part of establishing the connection is usable at all, and
# write-back is a *further* narrowing on top of that: a plan may allow read
# access without allowing write-back, so a gate registered for `USE_ACTION`
# alone would let a lapsed-for-write-but-not-for-read workspace keep writing.
WRITE_ACTION = "connections.write"


def connection_has_write_scopes(conn: WorkspaceConnection) -> bool:
    """Whether `conn.granted_scopes` actually carries both ReadWrite scopes
    `M365_WRITE_SCOPES` adds over the read-only default — the token-shape
    check `upload_connected_file` runs before ever attempting a Graph write,
    since a connection authorized under the plain "read" scope set (or one
    authorized before write-back existed at all) has a valid, `active` token
    that Graph will still happily 403 on any write call. Checked against
    what the provider actually granted, not what `POST /authorize` most
    recently *requested*: a tenant admin consent policy can silently drop a
    scope a user asked for, so the only trustworthy source is the token
    response itself, already persisted here by the OAuth callback."""
    granted = set(conn.granted_scopes or [])
    return {"Files.ReadWrite.All", "Sites.ReadWrite.All"}.issubset(granted)

# `Document.source_kind` for a file materialised from a live connection via
# `materialize_connected_file` — distinct from `"upload"` (a human put it
# here) and `"web"` (an agent fetched it from the open web): a connected
# document came from a specific, workspace-scoped provider drive a run was
# explicitly allowed to read, which is neither of those trust tiers.
CONNECTED_SOURCE_KIND = "connected"

_PROVIDER_LABEL: dict[str, str] = {GDRIVE: "Google Drive", M365: "Microsoft 365"}


class ConnectionUnavailable(Exception):
    """A workspace cannot use a connection right now — no HTTP layer
    downstream to turn this into a 409, so `.reason` is written as one short
    prose sentence safe to hand straight to a model or show a user: 'No
    Microsoft 365 connection is active for this workspace.' / 'Connections
    are not included in this workspace's plan.' / 'The Microsoft 365
    connection is in an error state: <detail>.' / 'Outbound access to
    connected services is disabled on this deployment.'"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ConnectedSource:
    """One drive a workspace's m365 connection is allowed to read from —
    either an admin-picked entry from `selected_resources["read"]`, or (when
    that allowlist is empty) one Graph reported the connected account can
    see at all. `slug` is what the rest of the surface (search, materialize,
    the frontend's source picker) addresses this drive by — stable across
    calls for the same drive, url-safe, and unique per workspace (see
    `_source_slug`)."""

    slug: str
    provider: str  # "m365"
    kind: str  # "site_drive" | "onedrive"
    label: str
    site_id: str | None
    drive_id: str
    web_url: str | None


@dataclass(frozen=True)
class SearchHit:
    item_ref: str  # opaque "m365:{drive_id}:{item_id}" — see make_item_ref
    name: str
    path: str
    web_url: str | None
    modified: str | None  # ISO 8601, from lastModifiedDateTime
    size: int | None
    snippet: str | None
    source_slug: str


def make_item_ref(provider: str, drive_id: str, item_id: str) -> str:
    """The opaque id `SearchHit.item_ref` and `materialize_connected_file`'s
    `item_ref` argument share: `"{provider}:{drive_id}:{item_id}"`. Not a
    Graph id itself — a provider tag glued to one, so a run handling several
    `item_ref`s never has to guess which provider (or which drive) a bare id
    came from."""
    return f"{provider}:{drive_id}:{item_id}"


def parse_item_ref(item_ref: str) -> tuple[str, str, str]:
    """The inverse of `make_item_ref`: `(provider, drive_id, item_id)`.
    Raises `ValueError` for anything that isn't exactly three `:`-separated,
    non-empty segments, or whose provider isn't one this module knows about
    — a malformed or forged `item_ref` must fail loudly here rather than
    silently resolve to the wrong drive."""
    parts = (item_ref or "").split(":", 2)
    if len(parts) != 3 or not all(parts):
        raise ValueError(f"malformed item_ref: {item_ref!r}")
    provider, drive_id, item_id = parts
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider in item_ref: {provider!r}")
    return provider, drive_id, item_id

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
def authorize_url(
    provider: str,
    *,
    client: OAuthClientConfig,
    redirect_uri: str,
    state: str,
    scopes: tuple[str, ...] | None = None,
) -> str:
    """The provider consent-screen URL. `scopes` defaults to `spec.scopes`
    (unchanged from before write-back existed) — `api/connections.py`'s
    `authorize` route is the one caller that ever passes something else,
    picking `SCOPE_SETS[scope_set]` for an m365 write upgrade."""
    spec = PROVIDER_SPECS[provider]
    scope_tuple = spec.scopes if scopes is None else scopes
    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "scope": " ".join(scope_tuple),
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


async def ensure_connection_usable(
    db: AsyncSession, workspace_id: uuid.UUID, provider: str = M365
) -> WorkspaceConnection:
    """The workspace's `provider` connection, verified usable right now — the
    one check `list_connected_sources`, `search_connected_files`, and
    `materialize_connected_file` all share, and what an engine tool wrapper
    calls before offering live access to a connection inside a run. Raises
    `ConnectionUnavailable` (never `ConnectionAuthError`/`RuntimeError`:
    those read as HTTP-shaped failures for a caller with no HTTP layer
    downstream, `.reason` is written to be shown as-is) when any of:

    - the deployment's own egress kill switch (`TRET_EGRESS`) has outbound
      provider calls off — checked first, since it needs no database access
      and is true regardless of which workspace is asking;
    - no `provider` connection row exists for this workspace;
    - the row exists but `status != "active"` (a refresh has already failed
      with `invalid_grant`, or the stored token no longer decrypts — see
      `_refresh`'s own docstring);
    - the workspace's `connections.use` gate refuses (a plan gate tret_cloud
      registers, most commonly) — the same action every HTTP route that
      exercises an existing connection asks via `api/connections.py::
      require_connections_gate`, checked last here since it is the one
      condition that costs a network-free but still async round trip through
      the extension registry.

    Returns the row on success — every caller above needs it (for
    `selected_resources`, or simply to prove there is one to work with), so
    there is no reason to make each of them reload it separately.
    """
    label = _PROVIDER_LABEL.get(provider, provider)
    if master_mode() == MODE_OFF:
        raise ConnectionUnavailable(
            "Outbound access to connected services is disabled on this deployment."
        )
    conn = await get_connection(db, workspace_id, provider)
    if conn is None:
        raise ConnectionUnavailable(f"No {label} connection is active for this workspace.")
    if conn.status != "active":
        detail = conn.error_detail or "it must be reconnected"
        raise ConnectionUnavailable(f"The {label} connection is in an error state: {detail}.")
    gate = await get_extension_registry().check_workspace_gate(db, workspace_id, USE_ACTION)
    if not gate.allowed:
        raise ConnectionUnavailable(
            gate.detail or "Connections are not included in this workspace's plan."
        )
    return conn


async def record_connection_activity(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    provider: str,
    action: str,
    actor_user_id: uuid.UUID | None = None,
    actor_run_id: uuid.UUID | None = None,
    target: str | None = None,
    bytes_count: int | None = None,
    detail: str | None = None,
) -> None:
    """Insert one `ConnectionActivity` row for this workspace's connections
    audit trail — every read-side call this module makes (`search_
    connected_files`, `materialize_connected_file`), every write-side call
    (`upload_connected_file`), and `api/connections.py`'s connect/
    disconnect/resources routes all funnel through here rather than
    constructing the row themselves, so the shape stays in one place.

    `db.add` + `db.flush` only, same contract `materialize_connected_
    file`'s own `Document` insert follows: the caller owns the transaction
    (commits it, rolls it back, or folds it into a larger unit of work
    already in flight), so a failed request whose caller rolls back loses
    its activity row along with everything else it did — which is correct:
    an activity log entry for a call that never actually happened would be
    a lie. `target` is never truncated or otherwise reshaped here — each
    caller already writes it in the shape this row should keep (a search
    query capped to 200 chars, a `{slug}/tret/{name}` write-back path, …).
    """
    db.add(
        ConnectionActivity(
            workspace_id=workspace_id,
            provider=provider,
            action=action,
            actor_user_id=actor_user_id,
            actor_run_id=actor_run_id,
            target=target,
            bytes=bytes_count,
            detail=detail,
        )
    )
    await db.flush()


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


# Per-process cache of `list_connected_sources` results, keyed by
# (workspace_id, provider) exactly like `_access_token_cache` above — same
# single-instance reasoning (see that dict's own comment). Value is
# (expires_at, sources): `expires_at` a `time.monotonic()` deadline, not a
# wall-clock time, for the same reason the access-token cache uses one.
_sources_cache: dict[tuple[uuid.UUID, str], tuple[float, list[ConnectedSource]]] = {}


def invalidate_connected_sources(workspace_id: uuid.UUID, provider: str = M365) -> None:
    """Drop this (workspace, provider)'s cached `list_connected_sources`
    result, if any. Called from `invalidate_access_token` below (so every
    existing call site of that — `_refresh` flipping a connection to
    `status='error'`, disconnect, the OAuth callback — also drops a now-
    possibly-stale derived sources list) and directly from `PUT /api/
    connections/m365/resources` (an admin narrowing or widening the read
    allowlist must be visible on the very next call, not up to
    `_SOURCES_CACHE_SECONDS` later)."""
    _sources_cache.pop((workspace_id, provider), None)


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

    Also drops any cached `list_connected_sources` result for the same key
    — every reason a cached access token stops being trustworthy (the
    connection erroring, disconnecting, or being reconnected to a possibly
    different account) is equally a reason a derived "everything the account
    can see" sources list stops being trustworthy.
    """
    _access_token_cache.pop((workspace_id, provider), None)
    key = (workspace_id, provider)
    _access_token_epochs[key] = _access_token_epochs.get(key, 0) + 1
    invalidate_connected_sources(workspace_id, provider)


def clear_access_token_cache() -> None:
    """Drop every cached access token, epoch, per-key lock, and cached
    `list_connected_sources` result. Test isolation; also safe to call at
    process startup, though an empty process-local dict already starts
    empty."""
    _access_token_cache.clear()
    _access_token_epochs.clear()
    _access_token_locks.clear()
    _sources_cache.clear()


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
    any other per-item error.

    `size_bytes`, when known, is whichever of the two sizes above tripped
    the cap (the declared size read from provider metadata/headers, or the
    actual number of bytes streamed before the cap cut the transfer off) —
    `None` only where neither was available at the raise site. A caller
    that spends a per-run byte budget on failed attempts (engine/tools.py's
    `read_connected_file`) uses this to charge the budget for bytes that
    were, in fact, moved (or declared and then refused) rather than
    silently treating a too-large download as free."""

    def __init__(self, message: str, *, size_bytes: int | None = None) -> None:
        super().__init__(message)
        self.size_bytes = size_bytes


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
    """`{id, name, size, lastModifiedDateTime, file, eTag, webUrl,
    parentReference}` for one drive item — the pre-download size check and
    `modified_at` provenance for the Phase 1 import path, plus (`eTag`,
    `webUrl`, `parentReference.path`) what `materialize_connected_file`
    needs for its own dedupe check and the `Document.meta` it records."""
    return await _graph_get(
        access_token,
        f"/drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}",
        params={
            "$select": "id,name,size,lastModifiedDateTime,file,folder,eTag,webUrl,parentReference"
        },
    )


async def _read_capped(response: httpx.Response, max_bytes: int) -> bytes:
    """The body of an already-2xx streaming `response`, refused mid-stream if
    it would exceed `max_bytes` — the backstop half of the size cap, for
    whichever of the two providers did not declare (or lied about) a size."""
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise DownloadTooLargeError(
            f"{int(declared) // (1024 * 1024)}MB exceeds the {max_bytes // (1024 * 1024)}MB import limit",
            size_bytes=int(declared),
        )
    body = bytearray()
    async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
        body.extend(chunk)
        if len(body) > max_bytes:
            raise DownloadTooLargeError(
                f"exceeds the {max_bytes // (1024 * 1024)}MB import limit",
                size_bytes=len(body),
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


# ── live SharePoint/OneDrive access in runs ──────────────────────────────────
# What a run gets when it is allowed to reach a workspace's m365 connection
# live, rather than only through the Phase 1 import picker above: a read
# allowlist (`list_connected_sources`), full-text search over it
# (`search_connected_files`), and pulling one hit's bytes into the project's
# documents on demand (`materialize_connected_file`). gdrive is untouched by
# any of this — `drive.file`'s own picker-scoped grant (see docs/
# connections.md's Security notes) already means there is nothing Google
# would let a connection "see" beyond what a human explicitly picked, so
# there is no "everything the account can see" to derive and nothing for a
# read allowlist to narrow.
_SOURCES_CACHE_SECONDS = 300
_SEARCH_API_PAGE_SIZE = 50
_MAX_SEARCH_RESULTS = 20
_MAX_FALLBACK_DRIVES = 5
# `_derive_connected_sources` bounds: how many `@odata.nextLink` pages of
# `/sites?search=*` to follow, how many derived sites to keep before running
# any per-site `/drive` lookup, and how many of those lookups run at once.
_MAX_SITE_PAGES = 5
_MAX_DERIVED_SITES = 200
_SITE_DRIVE_CONCURRENCY = 8
_GRAPH_SEARCH_QUERY_URL = f"{GRAPH_API_BASE}/search/query"
# What a Graph `parentReference.path` starts with for an item inside a
# drive's own root — stripped off before a path is shown to a model/user, in
# favour of the source's own human label (see `_humanize_path`).
_DRIVE_ROOT_PATH_PREFIX = "root:"

_UNSAFE_FILENAME_CHARS = re.compile(r"[^\w.() \[\]+&,-]")
_MEDIA_TYPE_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9!#$&\-\^_.+]*/[a-zA-Z0-9][a-zA-Z0-9!#$&\-\^_.+]*")
_DEFAULT_CONTENT_TYPE = "application/octet-stream"


def _source_slug(provider: str, drive_id: str, label: str) -> str:
    """A stable, url-safe, per-workspace-unique slug for one drive: a
    slugified prefix of `label` (so it reads as something, in a URL or a
    frontend dropdown) plus a short hash of `(provider, drive_id)` (so it is
    actually unique and stable across calls even when two drives share a
    label, or a label is empty/all-punctuation). Never recomputed from
    anything other than `provider`/`drive_id` for the hash half, so the same
    drive gets the same slug on every call regardless of which of the two
    `list_connected_sources` branches produced it."""
    digest = hashlib.sha256(f"{provider}:{drive_id}".encode()).hexdigest()[:10]
    base = re.sub(r"[^a-z0-9]+", "-", (label or provider).lower()).strip("-")[:40]
    return f"{base}-{digest}" if base else f"{provider}-{digest}"


def _connected_source_from_selection(entry: dict) -> ConnectedSource:
    """One `selected_resources["read"]` entry (already pydantic-validated by
    `PUT /api/connections/m365/resources` before it was persisted) as a
    `ConnectedSource`."""
    drive_id = entry["drive_id"]
    label = entry.get("label") or drive_id
    return ConnectedSource(
        slug=_source_slug(M365, drive_id, label),
        provider=M365,
        kind=entry.get("kind") or "site_drive",
        label=label,
        site_id=entry.get("site_id"),
        drive_id=drive_id,
        web_url=entry.get("web_url"),
    )


async def _fetch_site_drive_source(
    sem: asyncio.Semaphore, token: str, site: dict
) -> ConnectedSource | None:
    """One site's default document library as a `ConnectedSource`, or
    `None` if the site has no default drive (Graph 404s `/drive` for it —
    not every SharePoint site has one provisioned) or the lookup otherwise
    fails. Failures are swallowed (logged at debug) rather than raised:
    called under `asyncio.gather`, so one mis-provisioned or momentarily
    unreachable site must not fail the whole derive."""
    site_id = site.get("id")
    if not site_id:
        return None
    async with sem:
        try:
            drive = await _graph_get(token, f"/sites/{quote(site_id, safe='')}/drive")
        except RuntimeError:
            log.debug("connections: drive lookup failed for site %s", site_id, exc_info=True)
            return None
    drive_id = drive.get("id")
    if not drive_id:
        return None
    site_name = site.get("displayName") or site.get("name") or site_id
    drive_name = drive.get("name") or "Documents"
    return ConnectedSource(
        slug=_source_slug(M365, drive_id, f"{site_name} {drive_name}"),
        provider=M365,
        kind="site_drive",
        label=f"{site_name} / {drive_name}",
        site_id=site_id,
        drive_id=drive_id,
        web_url=drive.get("webUrl"),
    )


async def _derive_connected_sources(db: AsyncSession, workspace_id: uuid.UUID) -> list[ConnectedSource]:
    """"Everything the account can see": each site's default document
    library (`GET /sites/{id}/drive`) plus the account's own OneDrive (`GET
    /me/drive`) — the unrestricted default `list_connected_sources` falls
    back to when a workspace has never narrowed its `selected_resources
    ["read"]` allowlist. Reuses `browse_m365`'s own `/sites` listing and
    `_fetch_m365_upn` for the OneDrive label, rather than re-deriving either.

    `/sites?search=*` is followed across up to `_MAX_SITE_PAGES` pages of
    `@odata.nextLink` (a tenant with more sites than that stops paging
    rather than looping forever), and the resulting site list is capped at
    `_MAX_DERIVED_SITES` (logged once when the cap actually trims
    something) before any per-site drive lookup runs — an unbounded tenant
    directory must not turn into an unbounded number of `/drive` calls. The
    per-site `/drive` lookups themselves run with bounded concurrency
    (`_SITE_DRIVE_CONCURRENCY` at a time) rather than sequentially, one
    request per site, which is what made this derive slow enough to matter
    in the first place; a site whose drive lookup fails is skipped, not
    fatal to the rest (`_fetch_site_drive_source`).
    """
    token = await get_access_token(db, workspace_id, M365)
    sources: list[ConnectedSource] = []

    sites: list[dict] = []
    next_link: str | None = None
    for _ in range(_MAX_SITE_PAGES):
        if next_link is None:
            body = await _graph_get(token, "/sites", params={"search": "*"})
        elif next_link.startswith(GRAPH_API_BASE):
            # @odata.nextLink is a full URL; _graph_get only takes a path,
            # so strip the base it always shares with every other Graph call
            # here (same host `_policy` already pins, not a new one).
            body = await _graph_get(token, next_link[len(GRAPH_API_BASE):])
        else:
            # Graph has never been observed to return a nextLink off-base;
            # if it ever did, following it blind would sidestep `_policy`'s
            # host pin, so stop paging instead.
            break
        sites.extend(body.get("value", []))
        next_link = body.get("@odata.nextLink")
        if not next_link or len(sites) >= _MAX_DERIVED_SITES:
            break

    # Capped either by trimming an overshoot in one page, or by stopping
    # early (site cap or page cap) while Graph still had more to give
    # (`next_link` left over from the loop's last fetch says so either way).
    capped = len(sites) > _MAX_DERIVED_SITES or bool(next_link)
    if capped:
        log.warning(
            "connections: capped derived sites at %d for workspace %s (tenant has more)",
            _MAX_DERIVED_SITES,
            workspace_id,
        )
        sites = sites[:_MAX_DERIVED_SITES]

    sem = asyncio.Semaphore(_SITE_DRIVE_CONCURRENCY)
    site_sources = await asyncio.gather(*(_fetch_site_drive_source(sem, token, site) for site in sites))
    sources.extend(source for source in site_sources if source is not None)

    my_drive = await _graph_get(token, "/me/drive")
    my_drive_id = my_drive.get("id")
    if my_drive_id:
        upn = await _fetch_m365_upn(token)
        label = f"OneDrive ({upn})" if upn else "OneDrive"
        sources.append(
            ConnectedSource(
                slug=_source_slug(M365, my_drive_id, label),
                provider=M365,
                kind="onedrive",
                label=label,
                site_id=None,
                drive_id=my_drive_id,
                web_url=my_drive.get("webUrl"),
            )
        )
    return sources


async def list_connected_sources(db: AsyncSession, workspace_id: uuid.UUID) -> list[ConnectedSource]:
    """The m365 drives this workspace is allowed to read from — an admin's
    explicit `selected_resources["read"]` allowlist when one is set, else
    every drive `_derive_connected_sources` finds. Result is cached
    in-process for `_SOURCES_CACHE_SECONDS`, keyed by `(workspace_id, m365)`
    (see `invalidate_connected_sources`/`invalidate_access_token` for what
    drops that cache early), so a run doing several searches in a row does
    not re-list every site on every call.

    Raises `ConnectionUnavailable` via `ensure_connection_usable` — no
    connection, an errored one, or a plan gate refusal all read the same way
    to a caller of this function as "there is nothing to list right now".
    """
    # The gate must run every call, cache or not — a cached list must not
    # let a workspace whose connection has since gone unusable (errored,
    # disconnected, plan downgraded) keep reading through a stale result.
    conn = await ensure_connection_usable(db, workspace_id, M365)

    key = (workspace_id, M365)
    now = time.monotonic()
    cached = _sources_cache.get(key)
    if cached is not None and cached[0] > now:
        return cached[1]

    selected = (conn.selected_resources or {}).get("read")
    if selected:
        sources = [_connected_source_from_selection(entry) for entry in selected]
    else:
        sources = await _derive_connected_sources(db, workspace_id)

    _sources_cache[key] = (now + _SOURCES_CACHE_SECONDS, sources)
    return sources


def _strip_html(text: str | None) -> str | None:
    """The Search API's `summary` field carries `<c0>`/`</c0>`-style
    highlight markup around matched terms — stripped to plain text, since
    nothing downstream of `SearchHit.snippet` renders HTML."""
    if not text:
        return None
    stripped = re.sub(r"<[^>]+>", "", text).strip()
    return stripped or None


def _humanize_path(parent_path: str, name: str, source_label: str) -> str:
    """`parentReference.path` (e.g. `/drives/{id}/root:/Reports/2026`) as a
    human path prefixed by the source's own label instead of a raw drive id
    — `{source_label}/Reports/2026/{name}`. The `root:` segment Graph always
    includes right before the actual folder path is what gets stripped; a
    drive-root item (no folder path at all) collapses to just
    `{source_label}/{name}`."""
    folder = parent_path or ""
    idx = folder.find(_DRIVE_ROOT_PATH_PREFIX)
    if idx != -1:
        folder = folder[idx + len(_DRIVE_ROOT_PATH_PREFIX):]
    folder = folder.strip("/")
    segment = f"{folder}/{name}" if folder else name
    return f"{source_label}/{segment}" if segment else source_label


def _search_hit_from_resource(
    resource: dict, *, summary: str | None, source: ConnectedSource
) -> SearchHit | None:
    item_id = resource.get("id")
    drive_id = (resource.get("parentReference") or {}).get("driveId") or source.drive_id
    if not item_id or not drive_id:
        return None
    parent_path = (resource.get("parentReference") or {}).get("path") or ""
    return SearchHit(
        item_ref=make_item_ref(M365, drive_id, item_id),
        name=resource.get("name") or "",
        path=_humanize_path(parent_path, resource.get("name") or "", source.label),
        web_url=resource.get("webUrl"),
        modified=resource.get("lastModifiedDateTime"),
        size=resource.get("size"),
        snippet=_strip_html(summary),
        source_slug=source.slug,
    )


class _SearchApiUnsupported(Exception):
    """The Microsoft Search API answered 4xx — most commonly because the
    connected account is a personal Microsoft account, which the Search API
    does not support at all (it is a Microsoft 365/Entra-tenant-only
    surface). Internal to `search_connected_files`: callers never see this,
    only its per-drive `/root/search` fallback result."""


async def _search_query(access_token: str, query: str) -> dict:
    """POST one `/search/query` request for `query`, scoped to drive items.
    Raises `_SearchApiUnsupported` for a 4xx response (see that class) and
    `RuntimeError` for anything else that keeps the call from completing —
    same transport-vs-support split `_graph_get` draws, just with the two
    outcomes needing to be told apart here instead of always being the same
    `RuntimeError`."""
    body = {
        "requests": [
            {
                "entityTypes": ["driveItem"],
                "query": {"queryString": query},
                "from": 0,
                "size": _SEARCH_API_PAGE_SIZE,
            }
        ]
    }
    try:
        async with build_client(
            _EGRESS_CLASS, policy=_policy(_GRAPH_SEARCH_QUERY_URL), timeout=15.0
        ) as client:
            response = await client.post(
                _GRAPH_SEARCH_QUERY_URL,
                json=body,
                headers={"Authorization": f"Bearer {access_token}"},
            )
    except EgressDenied as exc:
        raise RuntimeError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Microsoft Graph: {exc}") from exc
    if 400 <= response.status_code < 500:
        raise _SearchApiUnsupported(f"Microsoft Search API returned {response.status_code}")
    try:
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"could not reach Microsoft Graph: {exc}") from exc
    return response.json()


async def _search_via_search_api(
    access_token: str, query: str, sources_by_drive: dict[str, ConnectedSource]
) -> list[SearchHit]:
    body = await _search_query(access_token, query)
    hits: list[SearchHit] = []
    for req in body.get("value", []):
        for container in req.get("hitsContainers", []):
            for hit in container.get("hits", []):
                resource = hit.get("resource") or {}
                drive_id = (resource.get("parentReference") or {}).get("driveId")
                source = sources_by_drive.get(drive_id)
                if source is None:
                    continue  # post-filter: not one of this workspace's allowed drives
                parsed = _search_hit_from_resource(resource, summary=hit.get("summary"), source=source)
                if parsed is not None:
                    hits.append(parsed)
    return hits


async def _search_via_drive_fallback(
    access_token: str, query: str, sources_by_drive: dict[str, ConnectedSource]
) -> list[SearchHit]:
    """Per-drive `GET /drives/{id}/root/search(q='{query}')`, for the
    personal-Microsoft-account case the tenant-only Search API refuses.
    Capped to the first `_MAX_FALLBACK_DRIVES` allowed drives — a personal
    account's connection realistically has one drive (its own OneDrive) or a
    small handful, never the dozens a tenant-wide `/search/query` call
    covers in one request, so fanning out to every allowed drive here would
    trade one request for many without a workspace ever having enough drives
    to make that cost worth it.

    Each hit is pinned to the drive actually searched (`source.drive_id`),
    never the `parentReference.driveId` Graph reports on the resource: for a
    consumer OneDrive that field can name a *different* drive — a folder
    shared into this drive from elsewhere — which would otherwise let a hit
    resolve against a drive this workspace was never allowed to read. A hit
    whose `parentReference.driveId` is present and isn't one of this
    workspace's allowed drives is dropped outright rather than merely
    repinned, on the same "don't trust it enough to launder it" reasoning.
    """
    encoded_query = quote(query, safe="")
    hits: list[SearchHit] = []
    for source in list(sources_by_drive.values())[:_MAX_FALLBACK_DRIVES]:
        path = f"/drives/{quote(source.drive_id, safe='')}/root/search(q='{encoded_query}')"
        try:
            body = await _graph_get(access_token, path)
        except RuntimeError:
            continue
        for item in body.get("value", []):
            parent_drive_id = (item.get("parentReference") or {}).get("driveId")
            if parent_drive_id is not None and parent_drive_id not in sources_by_drive:
                continue  # shared-in item from a drive this workspace never allowed
            parsed = _search_hit_from_resource(item, summary=None, source=source)
            if parsed is None:
                continue
            hits.append(replace(parsed, item_ref=make_item_ref(M365, source.drive_id, item.get("id"))))
    return hits


async def search_connected_files(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    query: str,
    *,
    source_slug: str | None = None,
    max_results: int = 20,
    actor_user_id: uuid.UUID | None = None,
    actor_run_id: uuid.UUID | None = None,
) -> list[SearchHit]:
    """Full-text search over the workspace's allowed m365 drives
    (`list_connected_sources`), narrowed to one source when `source_slug` is
    given. Tries the tenant-wide Microsoft Search API first, and falls back
    to a per-drive `/root/search` call (capped to
    `_MAX_FALLBACK_DRIVES` drives) only when the Search API itself refuses
    with a 4xx — the shape a personal Microsoft account's connection gets
    back, since Search is a tenant-only surface. `max_results` is clamped to
    `[1, 20]` regardless of what is asked for. Raises `ValueError` for an
    empty (or whitespace-only) `query`, and `ConnectionUnavailable` (via
    `list_connected_sources`) under the same conditions that function does.

    Records one `ConnectionActivity` row (`action="search"`) on every
    successful call — `target` is the query itself, truncated to 200
    characters (an activity row is for "what was searched for," not a
    verbatim log an oversized query could bloat), `detail` the number of
    hits actually returned. `actor_user_id`/`actor_run_id` are optional and
    default to `None`: a caller that knows who's asking (a future API route)
    can pass them; `engine/tools.py`'s tool wrapper today does not, so a
    run's own searches record with no actor — still workspace- and
    query-attributed, just not person/run-attributed.
    """
    query = (query or "").strip()
    if not query:
        raise ValueError("query must not be empty")
    max_results = max(1, min(_MAX_SEARCH_RESULTS, max_results))

    sources = await list_connected_sources(db, workspace_id)
    if source_slug is not None:
        sources = [s for s in sources if s.slug == source_slug]
    if not sources:
        return []
    sources_by_drive = {s.drive_id: s for s in sources}

    token = await get_access_token(db, workspace_id, M365)
    try:
        hits = await _search_via_search_api(token, query, sources_by_drive)
    except _SearchApiUnsupported:
        hits = await _search_via_drive_fallback(token, query, sources_by_drive)
    result = hits[:max_results]
    await record_connection_activity(
        db,
        workspace_id=workspace_id,
        provider=M365,
        action="search",
        target=query[:200],
        detail=str(len(result)),
        actor_user_id=actor_user_id,
        actor_run_id=actor_run_id,
    )
    return result


def _safe_connected_filename(raw: str | None) -> str:
    """A filename that can only ever name a file inside the storage
    directory — the same threat model `api/documents.py::safe_filename`
    guards against (a provider-supplied name is exactly as untrusted as a
    multipart client's), reimplemented here rather than imported: that
    function lives in the API layer, and this service must not import
    upward into it."""
    name = PurePosixPath((raw or "").replace("\\", "/")).name
    name = _UNSAFE_FILENAME_CHARS.sub("_", name.replace("\x00", ""))
    name = name.strip(". ")
    return name or "file"


def _safe_connected_content_type(raw: str | None) -> str:
    candidate = (raw or "").split(";", 1)[0].strip().lower()
    return candidate if _MEDIA_TYPE_RE.fullmatch(candidate) else _DEFAULT_CONTENT_TYPE


async def _find_existing_connected_document(
    db: AsyncSession, project_id: uuid.UUID, *, item_id: str, etag: str | None
) -> Document | None:
    """A `Document` this project already has for `item_id` at exactly
    `etag`, if any — `materialize_connected_file`'s dedupe check. Filtered
    in Python rather than in SQL on `meta->>'item_id'`: a project's document
    count is small enough that this costs nothing, and it sidesteps the
    sqlite-vs-Postgres JSON-operator differences the rest of this codebase
    works around with `.op("->>")` (see `services/emission_settings.py`)
    when there's no such small-N escape hatch. No etag at all (an item Graph
    reports with none) means nothing to dedupe against — always download
    fresh."""
    if not etag:
        return None
    rows = (
        await db.execute(
            select(Document).where(
                Document.project_id == project_id,
                Document.source_kind == CONNECTED_SOURCE_KIND,
            )
        )
    ).scalars().all()
    for doc in rows:
        meta = doc.meta or {}
        if meta.get("provider") == M365 and meta.get("item_id") == item_id and meta.get("etag") == etag:
            return doc
    return None


async def materialize_connected_file(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    item_ref: str,
    uploaded_by: uuid.UUID | None = None,
    actor_run_id: uuid.UUID | None = None,
) -> Document:
    """Pull one `item_ref` (from a `SearchHit`, or anything else that names
    an m365 drive item) into `project_id`'s documents, downloading it only
    if this project does not already have it at its current version.

    Raises `ValueError` for a malformed `item_ref` or one naming a folder
    rather than a file; `ConnectionUnavailable` if the connection itself
    cannot be used right now, or if `item_ref`'s drive is not one of this
    workspace's allowed sources (`list_connected_sources`) — a caller must
    not be able to materialize a file merely by guessing/forging a
    `drive_id:item_id` pair Graph itself would happily resolve but this
    workspace was never allowed to read; `DownloadTooLargeError` if the
    item's declared size alone exceeds `IMPORT_MAX_BYTES` (checked before
    any bytes are fetched — `download_m365_file`'s own streamed cap is the
    backstop for a size Graph didn't declare or lied about, not the first
    line of defence).

    Dedupe: if this project already has a `Document` recorded from this
    exact `item_id` at its current `eTag`, that row is returned unchanged
    and nothing is downloaded — a second search hit (or a second run) for a
    file nobody edited since the last materialize is free.

    Records one `ConnectionActivity` row (`action="read"`) on every
    successful call, deduped or not — `target` the file's humanized path,
    `bytes_count` its size, `detail="cached"` only on the dedupe branch (a
    fresh download leaves `detail` unset), so the activity log can tell a
    real transfer from a free repeat apart. `actor_user_id` for the row is
    `uploaded_by` — this function already threads that through to the
    `Document` it creates, so a second "who was this for" parameter would
    be redundant; `actor_run_id` has no existing equivalent to reuse, hence
    the new parameter.
    """
    provider, drive_id, item_id = parse_item_ref(item_ref)
    if provider != M365:
        raise ValueError(f"unsupported provider in item_ref: {provider!r}")

    sources = await list_connected_sources(db, workspace_id)
    source = next((s for s in sources if s.drive_id == drive_id), None)
    if source is None:
        raise ConnectionUnavailable("That file is outside the folders this workspace allows.")

    token = await get_access_token(db, workspace_id, M365)
    metadata = await m365_item_metadata(token, drive_id=drive_id, item_id=item_id)
    if "folder" in metadata:
        raise ValueError(f"item_ref {item_ref!r} names a folder, not a file")

    name = metadata.get("name") or item_id
    etag = metadata.get("eTag")
    size = metadata.get("size")
    web_url = metadata.get("webUrl")
    modified = metadata.get("lastModifiedDateTime")
    path = _humanize_path((metadata.get("parentReference") or {}).get("path") or "", name, source.label)

    existing = await _find_existing_connected_document(db, project_id, item_id=item_id, etag=etag)
    if existing is not None:
        await record_connection_activity(
            db,
            workspace_id=workspace_id,
            provider=M365,
            action="read",
            target=path,
            bytes_count=existing.byte_size,
            detail="cached",
            actor_user_id=uploaded_by,
            actor_run_id=actor_run_id,
        )
        return existing

    if isinstance(size, int) and size > IMPORT_MAX_BYTES:
        raise DownloadTooLargeError(
            f"{size // (1024 * 1024)}MB exceeds the {IMPORT_MAX_BYTES // (1024 * 1024)}MB import limit",
            size_bytes=size,
        )

    data = await download_m365_file(token, drive_id=drive_id, item_id=item_id, max_bytes=IMPORT_MAX_BYTES)

    filename = _safe_connected_filename(name)
    content_type = _safe_connected_content_type((metadata.get("file") or {}).get("mimeType"))
    sha = hashlib.sha256(data).hexdigest()
    storage_dir = Path(get_settings().storage_dir).resolve()
    storage_dir.mkdir(parents=True, exist_ok=True)
    storage_path = (storage_dir / f"{sha}-{filename}").resolve()
    if storage_path.parent != storage_dir:
        raise ValueError("Invalid filename")

    # Same "xb" check-and-create race handling as `api/documents.py::
    # _import_one` — see that function's own comment for the full reasoning.
    # Same bytes (same sha) means another Document row already owns this
    # file on disk; this call must not touch it either way.
    try:
        with storage_path.open("xb") as sink:
            sink.write(data)
        created = True
    except FileExistsError:
        created = False

    try:
        doc = await ingest_document(
            project_id=project_id,
            filename=filename,
            content_type=content_type,
            storage_path=storage_path,
            sha256=sha,
            byte_size=len(data),
            data=data,
            uploaded_by=uploaded_by,
            source_kind=CONNECTED_SOURCE_KIND,
            meta_extra={
                "provider": M365,
                "source_slug": source.slug,
                "site_id": source.site_id,
                "drive_id": drive_id,
                "item_id": item_id,
                "etag": etag,
                "web_url": web_url,
                "path": path,
                "modified": modified,
            },
        )
        # ingest_document does not commit or add to the session — the caller
        # owns the transaction (see its own docstring). Without this, the
        # returned Document has id=None: nothing is persisted, dedupe above
        # never finds it on a later call, and callers that stash the id
        # (e.g. engine/tools.py appending to ctx.document_ids) stash None.
        # Mirrors tret/net/fetch/snapshot.py's own add+flush.
        db.add(doc)
        await db.flush()
        await record_connection_activity(
            db,
            workspace_id=workspace_id,
            provider=M365,
            action="read",
            target=path,
            bytes_count=len(data),
            actor_user_id=uploaded_by,
            actor_run_id=actor_run_id,
        )
        return doc
    except BaseException:
        if created:
            storage_path.unlink(missing_ok=True)
        raise


# ── write-back to SharePoint/OneDrive ────────────────────────────────────────
# The mirror image of "live access in runs" above: instead of a read
# allowlist a run may pull *from*, an admin picks a small set of folders a
# run (or a person, through a future UI) may push files *into* —
# `selected_resources["write"]` on the same `WorkspaceConnection` row the
# read allowlist lives on, set by the same `PUT /api/connections/m365/
# resources` route (a new `write` key alongside the existing `read` one).
# m365 only, same reasoning as live read access: gdrive's `drive.file` grant
# is already scoped to files a human picked through the Picker, so there is
# no "everything the account can write to" to narrow and nothing for a write
# allowlist to mean for that provider.
#
# Unlike the read allowlist, an empty (or absent) write list is never
# "everything" — there is no sensible "every folder the account can write
# to" default for write-back the way "every drive the account can read"
# is for `list_connected_sources`: writing is the one operation that can
# change what's in a connected account, so it is opt-in, per-folder, always.
_TRET_SUBFOLDER_NAME = "tret"
# <=4MB: a single PUT .../content call. Above that Graph requires (and this
# module uses) an upload session with 5MiB chunks — 4MiB is Graph's own
# documented ceiling for the simple PUT path, not a number this module chose.
_UPLOAD_SIMPLE_MAX_BYTES = 4 * 1024 * 1024
_UPLOAD_CHUNK_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class WriteTarget:
    """One folder a workspace's m365 connection is allowed to write into —
    an admin-picked entry from `selected_resources["write"]`, unlike
    `ConnectedSource` above there is no "everything the account can see"
    fallback to derive (see the module note above this dataclass). `slug`
    is admin-supplied and validated at `PUT /api/connections/m365/
    resources` time (unique within the list, `^[a-z0-9][a-z0-9-]{1,39}$`) —
    unlike `ConnectedSource.slug`, which this module derives itself, a
    write target's slug is exactly what the admin who picked the folder
    typed, so `upload_connected_file`'s `target_slug` argument is legible
    in a way a hash-suffixed derived slug would not be."""

    slug: str
    label: str
    path: str
    site_id: str | None
    drive_id: str
    item_id: str
    web_url: str | None


class ConnectionWriteError(Exception):
    """A write-back call could not be completed — no HTTP layer downstream
    to turn this into a 4xx/5xx itself, so `.reason` is one short prose
    sentence safe to show a model or a user as-is: 'Write-back is not
    enabled for this connection…', ''budget' is not an allowed output
    folder.', '32MB exceeds the 25MB write-back limit', 'Microsoft Graph
    returned 403 (accessDenied)'. Mirrors `ConnectionUnavailable`'s own
    contract for the read side — a distinct type rather than reusing that
    one because a write refusal (gate, missing write scopes, an unknown
    target, a Graph write 4xx/5xx) is not the same claim as "this
    connection cannot be used *at all* right now", and callers (the API
    route, an engine tool wrapper) need to tell the two apart."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class UploadResult:
    """What `upload_connected_file` hands back on success: the final
    `driveItem`'s id/webUrl/name/size (name and size read from Graph's own
    response, not echoed back from the request — a `rename` conflict
    behaviour, per this module's own create-only policy, means the name
    Graph actually used can differ from the one asked for), plus which
    target it landed in and its full humanized path under that target's
    `tret/` subfolder."""

    item_id: str
    web_url: str | None
    name: str
    size: int
    target_slug: str
    path: str


async def list_write_targets(db: AsyncSession, workspace_id: uuid.UUID) -> list[WriteTarget]:
    """The folders this workspace's runs (or a future write-back UI) may
    upload into — `selected_resources["write"]` on the connection row,
    verbatim, as `WriteTarget`s. `[]` when the admin has never set any (see
    the module note above `WriteTarget` for why that means "nothing", not
    "everything", unlike the read side's `list_connected_sources`).

    `ensure_connection_usable` first, same as `list_connected_sources` —
    raises `ConnectionUnavailable` under the same conditions that function
    does (no connection, an errored one, egress off, the `connections.use`
    gate refusing). No Graph call: unlike a `ConnectedSource`, a
    `WriteTarget`'s every field was already supplied by the admin who set
    it, so there is nothing here to resolve against the provider — a target
    that no longer exists (deleted at the provider) only surfaces the next
    time `upload_connected_file` actually tries to write into it.
    """
    conn = await ensure_connection_usable(db, workspace_id, M365)
    entries = (conn.selected_resources or {}).get("write") or []
    return [
        WriteTarget(
            slug=entry["slug"],
            label=entry.get("label") or entry["slug"],
            path=entry.get("path") or "",
            site_id=entry.get("site_id"),
            drive_id=entry["drive_id"],
            item_id=entry["item_id"],
            web_url=entry.get("web_url"),
        )
        for entry in entries
    ]


_UPLOAD_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
# Illegal in a OneDrive/SharePoint item name outright — Graph itself refuses
# a create/rename that contains any of these, so refusing here is refusing
# earlier with a clearer reason, not a stricter rule than the provider's own.
_UPLOAD_FORBIDDEN_CHARS_RE = re.compile(r'[/\\:*?"<>|]')


def safe_upload_filename(name: str) -> str:
    """A filename Graph will accept for a create-only write-back upload —
    refused outright (`ValueError`) rather than silently rewritten, unlike
    `_safe_connected_filename`'s read-side handling of a provider-supplied
    name: a write-back name comes from a tool call or a form an operator
    controls, not an untrusted response this module must make *something*
    safe out of no matter what, so there is no silent-rewrite convenience
    to offer, and every reason for refusal is one the caller can act on.

    Control characters are stripped first — cosmetic noise, never itself a
    reason to refuse. What is left is refused if it is empty, is exactly
    `"."` or `".."`, contains any of ``/ \\ : * ? " < > |`` (illegal in a
    OneDrive/SharePoint item name), has leading or trailing whitespace or
    dots, or exceeds 200 characters. Nothing here ever touches an
    extension: nothing above rewrites the name at all, so whatever
    extension `name` carried in survives exactly as given.
    """
    cleaned = _UPLOAD_CONTROL_CHARS_RE.sub("", name or "")
    if not cleaned:
        raise ValueError("filename must not be empty")
    if cleaned in (".", ".."):
        raise ValueError(f"{cleaned!r} is not a valid filename")
    if _UPLOAD_FORBIDDEN_CHARS_RE.search(cleaned):
        raise ValueError('filename must not contain any of / \\ : * ? " < > |')
    if cleaned.strip(" .") != cleaned:
        raise ValueError("filename must not have leading or trailing spaces or dots")
    if len(cleaned) > 200:
        raise ValueError("filename must not exceed 200 characters")
    return cleaned


def _graph_write_error_reason(response: httpx.Response) -> str:
    """A short, safe-to-show reason for a Graph 4xx/5xx encountered while
    writing back: the status code, plus Graph's own machine `error.code`
    when the body parses as one — never the full response body, which can
    carry an internal-state-revealing message meant for a developer console,
    not a run transcript or a user-facing error."""
    code = None
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        code = (body.get("error") or {}).get("code")
    return f"Microsoft Graph returned {response.status_code}" + (f" ({code})" if code else "")


async def _graph_write_call(
    method: str,
    url: str,
    *,
    policy: ClassPolicy,
    headers: dict,
    json_body: dict | None = None,
    content: bytes | None = None,
    params: dict | None = None,
) -> httpx.Response:
    """One write-back HTTP call — GET/POST/PUT against a fixed Graph path or
    (for an upload-session chunk) an upload URL Microsoft's own response
    named. Raises `ConnectionWriteError` only for a transport failure
    (egress denied, a network error): a non-2xx response is returned as-is
    for the caller to interpret, since one call site (`_ensure_tret_
    subfolder`'s create) needs to tell a 409 (lost a creation race — re-list
    and recover) apart from every other failure (refuse outright), and
    collapsing that distinction into an exception here would take it away.
    """
    try:
        async with build_client(_EGRESS_CLASS, policy=policy, timeout=_DOWNLOAD_TIMEOUT_SECONDS) as client:
            return await client.request(
                method, url, params=params, json=json_body, content=content, headers=headers
            )
    except EgressDenied as exc:
        raise ConnectionWriteError(f"connections egress is unavailable: {exc}") from exc
    except httpx.HTTPError as exc:
        raise ConnectionWriteError(f"could not reach Microsoft Graph: {exc}") from exc


def _raise_for_graph_write_status(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise ConnectionWriteError(_graph_write_error_reason(response))


async def _find_tret_subfolder_id(token: str, *, drive_id: str, parent_item_id: str) -> str | None:
    """The item id of the existing `tret` child folder directly under
    `parent_item_id`, or `None` if there isn't one yet. `$filter=name eq
    'tret'` narrows server-side, but the result is still matched by hand
    rather than trusted blind — a case-insensitive or partial match from the
    filter must not be mistaken for the exact folder this module owns, and
    neither must something that merely *looks* like it from this listing:

    - `"folder" in item` — a plain folder, not a file.
    - `"remoteItem" not in item` — not a shortcut to a folder that lives
      elsewhere (a OneDrive/SharePoint shortcut carries the target's own
      `folder` facet under `remoteItem`, which would otherwise pass the
      check above while pointing this module at a folder it does not own
      and was never granted to write into).
    - `parentReference.driveId`, when Graph reports one on the item, must
      equal `drive_id` — a defensive check against a listing that somehow
      surfaces an item from a different drive; every write below is scoped
      to `drive_id` and must never be redirected onto another one.

    A candidate that fails any of these is treated exactly like no match at
    all — the caller (`_ensure_tret_subfolder`) then tries to create the
    folder and lets the resulting 409 (the name is unavailable for a
    genuine create) turn into a clear refusal, rather than this function
    silently reusing something it must not."""
    url = f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/items/{quote(parent_item_id, safe='')}/children"
    response = await _graph_write_call(
        "GET", url, policy=_policy(url),
        headers={"Authorization": f"Bearer {token}"},
        params={"$filter": "name eq 'tret'"},
    )
    _raise_for_graph_write_status(response)
    for item in response.json().get("value", []):
        if item.get("name") != _TRET_SUBFOLDER_NAME or "folder" not in item or "remoteItem" in item:
            continue
        parent_drive_id = (item.get("parentReference") or {}).get("driveId")
        if parent_drive_id is not None and parent_drive_id != drive_id:
            continue
        return item.get("id")
    return None


async def _ensure_tret_subfolder(token: str, *, drive_id: str, parent_item_id: str) -> str:
    """The item id of the `tret` folder directly under `parent_item_id`,
    creating it (`conflictBehavior=fail`) if it does not exist yet. A 409
    on that create means another concurrent uploader won the race between
    this call's own list and its create — re-listed once rather than
    treated as a failure, since the folder that "conflicted" is exactly the
    one this call wanted. Every other outcome (a genuine failure, or the
    409 recovery itself somehow finding nothing) raises
    `ConnectionWriteError`."""
    existing = await _find_tret_subfolder_id(token, drive_id=drive_id, parent_item_id=parent_item_id)
    if existing is not None:
        return existing
    url = f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/items/{quote(parent_item_id, safe='')}/children"
    response = await _graph_write_call(
        "POST", url, policy=_policy(url),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json_body={"name": _TRET_SUBFOLDER_NAME, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
    )
    if response.status_code == 409:
        existing = await _find_tret_subfolder_id(token, drive_id=drive_id, parent_item_id=parent_item_id)
        if existing is not None:
            return existing
        # Lost the create race to *something* named `tret`, but the re-list
        # still finds nothing `_find_tret_subfolder_id` will accept — the
        # thing occupying that name is a file, a shortcut, or otherwise not
        # a plain folder this module owns. Name the real problem rather than
        # the generic "could not create or find" this used to say.
        raise ConnectionWriteError(
            "An item named tret already exists in the output folder and is not a plain "
            "folder — remove or rename it."
        )
    _raise_for_graph_write_status(response)
    folder_id = response.json().get("id")
    if not folder_id:
        raise ConnectionWriteError("Microsoft Graph did not return an id for the new tret folder")
    return folder_id


async def _upload_small(
    token: str, *, drive_id: str, folder_item_id: str, name: str, data: bytes, content_type: str
) -> dict:
    """<=4MB path: a single `PUT .../content`, `conflictBehavior=rename` so
    an existing same-named file is never overwritten (see module note) —
    Graph renames the *new* upload instead, and the returned `driveItem`
    carries whatever name it actually landed under."""
    url = (
        f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/items/"
        f"{quote(folder_item_id, safe='')}:/{quote(name, safe='')}:/content"
    )
    response = await _graph_write_call(
        "PUT", url, policy=_policy(url),
        headers={"Authorization": f"Bearer {token}", "Content-Type": content_type},
        content=data,
        params={"@microsoft.graph.conflictBehavior": "rename"},
    )
    _raise_for_graph_write_status(response)
    return response.json()


async def _upload_large(token: str, *, drive_id: str, folder_item_id: str, name: str, data: bytes) -> dict:
    """>4MB path: `createUploadSession` (also `conflictBehavior=rename` —
    same never-overwrite policy as the simple path), then the bytes in
    `_UPLOAD_CHUNK_BYTES`-sized `PUT`s to the session's own `uploadUrl`.

    The upload URL's host is chosen by Microsoft's response, not this
    module's own config — the same shape `download_m365_file`'s redirect
    hops are in, and the same fix: a fresh single-host `_policy` scoped to
    whatever `uploadUrl` actually says, with `verify_addresses=
    VERIFY_PUBLIC` (the SSRF check, since nothing here vouches for that
    host being Microsoft's own the way `GRAPH_API_BASE` is) rather than the
    `VERIFY_NONE` every fixed-Graph-endpoint call above uses. `allow_http=
    False` on that same policy (see `_policy`'s own default) is what
    refuses a non-https upload URL — not a special case here, just what
    building the policy the normal way already gets. No `Authorization`
    header is sent to the chunk PUTs: the session URL is itself the
    credential (it is pre-signed and short-lived), and Microsoft's own docs
    say sending a bearer token to it is at best redundant and at worst
    rejected.
    """
    session_url = (
        f"{GRAPH_API_BASE}/drives/{quote(drive_id, safe='')}/items/"
        f"{quote(folder_item_id, safe='')}:/{quote(name, safe='')}:/createUploadSession"
    )
    session_response = await _graph_write_call(
        "POST", session_url, policy=_policy(session_url),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json_body={"item": {"@microsoft.graph.conflictBehavior": "rename", "name": name}},
    )
    _raise_for_graph_write_status(session_response)
    upload_url = session_response.json().get("uploadUrl")
    if not upload_url:
        raise ConnectionWriteError("Microsoft Graph did not return an upload session URL")

    total = len(data)
    upload_policy = _policy(upload_url, verify_addresses=VERIFY_PUBLIC)
    item: dict | None = None
    for start in range(0, total, _UPLOAD_CHUNK_BYTES):
        chunk = data[start : start + _UPLOAD_CHUNK_BYTES]
        end = start + len(chunk) - 1
        chunk_response = await _graph_write_call(
            "PUT", upload_url, policy=upload_policy,
            headers={"Content-Range": f"bytes {start}-{end}/{total}"},
            content=chunk,
        )
        _raise_for_graph_write_status(chunk_response)
        if chunk_response.status_code in (200, 201):
            item = chunk_response.json()
    if item is None:
        raise ConnectionWriteError("Microsoft Graph did not confirm the upload's final chunk")
    return item


async def _refuse_upload(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target: str,
    bytes_count: int,
    reason: str,
    actor_user_id: uuid.UUID | None,
    actor_run_id: uuid.UUID | None,
) -> ConnectionWriteError:
    """Record `action="upload_failed"` for a refusal `upload_connected_file`
    is about to raise, and hand back the `ConnectionWriteError` to raise —
    `raise await _refuse_upload(...)` at every one of that function's
    refusal points, so recording the failure and raising it can never drift
    apart (one forgotten `record_connection_activity` call would otherwise
    be an easy, silent way for the activity log to under-report failures)."""
    await record_connection_activity(
        db,
        workspace_id=workspace_id,
        provider=M365,
        action="upload_failed",
        target=target,
        bytes_count=bytes_count,
        detail=reason,
        actor_user_id=actor_user_id,
        actor_run_id=actor_run_id,
    )
    return ConnectionWriteError(reason)


async def upload_connected_file(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    target_slug: str,
    filename: str,
    data: bytes,
    content_type: str,
    actor_user_id: uuid.UUID | None = None,
    actor_run_id: uuid.UUID | None = None,
) -> UploadResult:
    """Write `data` into workspace `workspace_id`'s m365 write target
    `target_slug`, under a `tret/` subfolder created on first use and reused
    after. Never overwrites an existing file: both the small (`PUT .../
    content`) and large (`createUploadSession`) paths request Graph's
    `rename` conflict behaviour, so a same-named file already there is left
    alone and the new upload lands under whatever name Graph assigns
    instead. Never deletes, moves, or shares anything — the only Graph
    write calls this module ever makes are the `tret` folder's own
    create-if-absent and the upload itself.

    Refusal order — every one of these raises `ConnectionWriteError` (never
    `ConnectionUnavailable`, which only `ensure_connection_usable`'s own
    checks below can raise) and records `action="upload_failed"` first, via
    `_refuse_upload`, except the `ensure_connection_usable` checks
    themselves, which behave exactly as they do for every other caller:

    1. `ensure_connection_usable` — no connection, an errored one, egress
       off, or the `connections.use` gate refusing.
    2. The `connections.write` workspace gate — a plan may allow read
       access without allowing write-back.
    3. `connection_has_write_scopes` — the connection's granted scopes
       don't include the write pair, most likely because it was authorized
       under the plain "read" scope set (or before write-back existed).
    4. `target_slug` not found in `list_write_targets` — an unknown, or no
       longer allowed, output folder.
    5. `filename` fails `safe_upload_filename`.
    6. `data` exceeds `IMPORT_MAX_BYTES` (the same 25MB cap the import path
       enforces — reused, not reinvented, for write-back's own cap).

    Only past all six does this module ever touch the network: ensuring the
    `tret` subfolder, then the small- or large-file upload path depending on
    `len(data)`. A Graph failure at either of those records `action=
    "upload_failed"` with Graph's own short reason (`_graph_write_error_
    reason`) and re-raises the same `ConnectionWriteError`.

    On success, records `action="upload"` (`target` the write-back path
    actually used, `bytes_count=len(data)`) and returns an `UploadResult`
    built from the final `driveItem` Graph reported.
    """
    conn = await ensure_connection_usable(db, workspace_id, M365)

    gate = await get_extension_registry().check_workspace_gate(db, workspace_id, WRITE_ACTION)
    if not gate.allowed:
        reason = gate.detail or "Write-back is not included in this workspace's plan."
        raise await _refuse_upload(
            db, workspace_id=workspace_id, target=target_slug, bytes_count=len(data),
            reason=reason, actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        )
    if not connection_has_write_scopes(conn):
        reason = (
            "Write-back is not enabled for this connection — an admin must "
            "reconnect it with write access."
        )
        raise await _refuse_upload(
            db, workspace_id=workspace_id, target=target_slug, bytes_count=len(data),
            reason=reason, actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        )

    targets = await list_write_targets(db, workspace_id)
    target = next((t for t in targets if t.slug == target_slug), None)
    if target is None:
        reason = f"{target_slug!r} is not an allowed output folder."
        raise await _refuse_upload(
            db, workspace_id=workspace_id, target=target_slug, bytes_count=len(data),
            reason=reason, actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        )

    try:
        name = safe_upload_filename(filename)
    except ValueError as exc:
        raise await _refuse_upload(
            db, workspace_id=workspace_id, target=target.slug, bytes_count=len(data),
            reason=str(exc), actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        ) from exc

    if len(data) > IMPORT_MAX_BYTES:
        reason = (
            f"{len(data) // (1024 * 1024)}MB exceeds the "
            f"{IMPORT_MAX_BYTES // (1024 * 1024)}MB write-back limit"
        )
        raise await _refuse_upload(
            db, workspace_id=workspace_id, target=target.slug, bytes_count=len(data),
            reason=reason, actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        )

    token = await get_access_token(db, workspace_id, M365)
    activity_target = f"{target.slug}/{_TRET_SUBFOLDER_NAME}/{name}"
    try:
        folder_id = await _ensure_tret_subfolder(token, drive_id=target.drive_id, parent_item_id=target.item_id)
        if len(data) <= _UPLOAD_SIMPLE_MAX_BYTES:
            item = await _upload_small(
                token, drive_id=target.drive_id, folder_item_id=folder_id,
                name=name, data=data, content_type=content_type,
            )
        else:
            item = await _upload_large(
                token, drive_id=target.drive_id, folder_item_id=folder_id, name=name, data=data
            )
    except ConnectionWriteError as exc:
        await record_connection_activity(
            db, workspace_id=workspace_id, provider=M365, action="upload_failed",
            target=activity_target, bytes_count=len(data), detail=exc.reason,
            actor_user_id=actor_user_id, actor_run_id=actor_run_id,
        )
        raise

    result_name = item.get("name") or name
    result_size = item.get("size") if isinstance(item.get("size"), int) else len(data)
    result_path = f"{target.path}/{_TRET_SUBFOLDER_NAME}/{result_name}".strip("/")
    result = UploadResult(
        item_id=item.get("id"),
        web_url=item.get("webUrl"),
        name=result_name,
        size=result_size,
        target_slug=target.slug,
        path=result_path,
    )
    await record_connection_activity(
        db, workspace_id=workspace_id, provider=M365, action="upload",
        target=f"{target.slug}/{_TRET_SUBFOLDER_NAME}/{result_name}", bytes_count=len(data),
        actor_user_id=actor_user_id, actor_run_id=actor_run_id,
    )
    return result
