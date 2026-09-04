"""Phase 1 of workspace connections: `GET /api/connections/m365/browse` (in
`tret/api/connections.py`) and `POST /api/projects/{project_id}/documents/
import` (in `tret/api/documents.py`, alongside the upload endpoint it shares
`services/documents.py::ingest_document` with).

Same harness as `test_connections_api.py`: real sqlite, session-cookie login,
respx-mocked provider HTTP — plus a `storage` fixture for the filesystem side
of ingestion, borrowed from `test_documents_api.py`.
"""
from __future__ import annotations

import base64
import hashlib
import uuid

import httpx
import pytest
import pytest_asyncio
import respx
from argon2 import PasswordHasher
from cryptography.fernet import Fernet
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, connections as connections_api, documents as documents_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Base, Document, Project, User, Workspace, WorkspaceConnection, WorkspaceMember
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult
from tret.net import guard as net_guard
from tret.services import connections as connections_module
from tret.services.connections import clear_access_token_cache, get_connection
from tret.services.credentials import get_fernet

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"

GDRIVE_TOKEN_URL = "https://oauth2.googleapis.com/token"
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH = "https://graph.microsoft.com/v1.0"
GDRIVE_API = "https://www.googleapis.com/drive/v3"
BLOB_CONTENT_URL = "https://contoso-my.sharepoint.com/download/signed-blob"


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    extensions_module._registry = None
    clear_access_token_cache()
    yield
    extensions_module._registry = None
    clear_access_token_cache()


@pytest.fixture(autouse=True)
def _configured_clients(monkeypatch):
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_ID", "gdrive-cid")
    monkeypatch.setenv("TRET_GDRIVE_CLIENT_SECRET", "gdrive-secret")
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    directory = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(directory))
    return directory


@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def seed(session_factory):
    async def _seed(*rows):
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

    return _seed


@pytest_asyncio.fixture
async def client(session_factory, storage):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(connections_api.router)
    app.include_router(documents_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ── fixture builders ─────────────────────────────────────────────────────────
def make_user(email: str, *, role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role=role,
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_project(workspace: Workspace, name: str = "P") -> Project:
    return Project(id=uuid.uuid4(), workspace_id=workspace.id, name=name)


def make_connection(
    workspace: Workspace,
    *,
    provider: str,
    refresh_token: str = "stored-refresh-token",
    status: str = "active",
) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider=provider,
        account_label="person@example.com",
        encrypted_refresh_token=get_fernet().encrypt(refresh_token.encode()),
        granted_scopes=["openid", "email"],
        status=status,
    )


def _encrypt_with_a_different_key(refresh_token: str) -> bytes:
    """A refresh token Fernet-sealed under a key that is not the current
    TRET_SECRET_KEY's derived key — simulates a row that survived a key
    rotation (see test_connections_service.py's own copy of this helper)."""
    digest = hashlib.sha256(b"a-different-secret-key-entirely").digest()
    return Fernet(base64.urlsafe_b64encode(digest)).encrypt(refresh_token.encode())


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def mock_m365_refresh(mock: respx.MockRouter, *, access_token: str = "m365-access-token") -> None:
    mock.post(M365_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": access_token, "expires_in": 3600})
    )


def mock_gdrive_refresh(mock: respx.MockRouter, *, access_token: str = "gdrive-access-token") -> None:
    mock.post(GDRIVE_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": access_token, "expires_in": 3600})
    )


# ── GET /api/connections/m365/browse ─────────────────────────────────────────
async def test_m365_browse_root_lists_sites_and_onedrive(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-root@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(
                200, json={"value": [{"id": "site-1", "displayName": "Marketing"}]}
            )
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "name": "OneDrive"})
        )
        response = await client.get("/api/connections/m365/browse")
    assert response.status_code == 200
    items = response.json()["items"]
    assert {"id": "site-1", "name": "Marketing", "kind": "site"} in items
    assert {"id": "drive-me", "name": "OneDrive", "kind": "drive", "drive_id": "drive-me"} in items


async def test_m365_browse_site_lists_its_drives(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-site@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites/site-1/drives").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "drive-1", "name": "Documents"}]})
        )
        response = await client.get(
            "/api/connections/m365/browse", params={"scope": "drive_children", "site_id": "site-1"}
        )
    assert response.status_code == 200
    assert response.json()["items"] == [
        {"id": "drive-1", "name": "Documents", "kind": "drive", "drive_id": "drive-1", "site_id": "site-1"}
    ]


async def test_m365_browse_drive_root_lists_children(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-drive@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/root/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "folder-1", "name": "Reports", "folder": {"childCount": 2}},
                        {
                            "id": "file-1",
                            "name": "budget.xlsx",
                            "size": 4096,
                            "lastModifiedDateTime": "2026-08-01T00:00:00Z",
                            "file": {"mimeType": "application/vnd.ms-excel"},
                        },
                    ]
                },
            )
        )
        response = await client.get(
            "/api/connections/m365/browse", params={"scope": "drive_children", "drive_id": "drive-1"}
        )
    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0] == {"id": "folder-1", "name": "Reports", "kind": "folder", "drive_id": "drive-1"}
    assert items[1] == {
        "id": "file-1",
        "name": "budget.xlsx",
        "kind": "file",
        "drive_id": "drive-1",
        "mime_type": "application/vnd.ms-excel",
        "size": 4096,
        "modified_at": "2026-08-01T00:00:00Z",
    }


async def test_m365_browse_folder_lists_children(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-folder@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/folder-1/children").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "file-2", "name": "notes.txt", "file": {"mimeType": "text/plain"}}]})
        )
        response = await client.get(
            "/api/connections/m365/browse",
            params={"scope": "drive_children", "drive_id": "drive-1", "item_id": "folder-1"},
        )
    assert response.status_code == 200
    assert response.json()["items"][0]["id"] == "file-2"


async def test_m365_browse_with_no_connection_is_409(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-noconn@example.com")
    await seed(workspace, user, make_member(user, workspace, role="analyst"))
    await login(client, user.email)

    response = await client.get("/api/connections/m365/browse")
    assert response.status_code == 409
    assert "reconnect" in response.json()["detail"].lower()


async def test_m365_browse_with_a_revoked_connection_is_409(client, seed):
    workspace = make_workspace("Alpha")
    user = make_user("browse-revoked@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(
                400, json={"error": "invalid_grant", "error_description": "revoked"}
            )
        )
        response = await client.get("/api/connections/m365/browse")
    assert response.status_code == 409


async def test_m365_browse_with_an_undecryptable_connection_is_409(client, seed):
    """A refresh token that no longer decrypts under the current
    TRET_SECRET_KEY (e.g. after a key rotation) must not 500 — same 409 as
    invalid_grant — and `GET /api/connections` must reflect the connection
    as broken (status='error', a non-empty error_detail) rather than still
    reporting it active."""
    workspace = make_workspace("Alpha")
    user = make_user("browse-undecryptable@example.com")
    conn = make_connection(workspace, provider="m365")
    conn.encrypted_refresh_token = _encrypt_with_a_different_key("stored-refresh-token")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    response = await client.get("/api/connections/m365/browse")
    assert response.status_code == 409

    listed = await client.get("/api/connections")
    entry = next(c for c in listed.json()["connections"] if c["provider"] == "m365")
    assert entry["status"] == "error"
    assert entry["error_detail"]


async def test_m365_browse_with_an_errored_connection_is_409_without_contacting_the_provider(client, seed):
    """A connection already flipped to `status='error'` by a previous
    failure must not be retried against the provider on every browse call —
    409 immediately, no token request at all."""
    workspace = make_workspace("Alpha")
    user = make_user("browse-errored@example.com")
    conn = make_connection(workspace, provider="m365", status="error")
    conn.error_detail = "the m365 connection is in an error state and must be reconnected"
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(M365_TOKEN_URL).mock(return_value=httpx.Response(200))
        response = await client.get("/api/connections/m365/browse")
        assert token_route.called is False
    assert response.status_code == 409


async def test_m365_browse_is_blocked_by_a_registered_workspace_gate(client, seed):
    """A lapsed plan must cut off server-side browsing too, not just the
    gdrive client-side token — and the refuse must land before the m365
    connection's own token refresh, so no Graph or token HTTP call happens."""
    workspace = make_workspace("Alpha")
    user = make_user("browse-gate@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "connections.use"
        assert workspace_id == workspace.id
        return GateResult(
            allowed=False,
            reason="connections_plan_required",
            detail="Connections require an active plan.",
        )

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(M365_TOKEN_URL).mock(return_value=httpx.Response(200))
        graph_route = mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        response = await client.get("/api/connections/m365/browse")
        assert token_route.called is False
        assert graph_route.called is False

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "connections_plan_required"


async def test_m365_browse_survives_a_disconnect_during_the_in_flight_refresh(
    client, seed, session_factory, monkeypatch
):
    """`DELETE /api/connections/m365` can delete the connection row while
    this request's own token refresh is still waiting on the provider. That
    must come back as the same 409 "needs to be reconnected" a browse
    against no connection at all already gets — not a raw StaleDataError
    surfacing as an unmapped 500."""
    workspace = make_workspace("Alpha")
    user = make_user("browse-disconnect-race@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    async def deleting_token_request(token_url, data):
        # Simulates DELETE /api/connections/m365 landing on a second session
        # while this request's own refresh is still in flight.
        async with session_factory() as other_db:
            row = await get_connection(other_db, workspace.id, "m365")
            await other_db.delete(row)
            await other_db.commit()
        return {"access_token": "m365-access-token", "expires_in": 3600}

    monkeypatch.setattr(connections_module, "_token_request", deleting_token_request)

    response = await client.get("/api/connections/m365/browse")
    assert response.status_code == 409
    detail = response.json()["detail"].lower()
    assert "reconnect" in detail or "no" in detail


# ── POST /api/projects/{project_id}/documents/import ─────────────────────────
async def test_import_gdrive_binary_happy_path(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    user = make_user("import-gdrive@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1", "name": "notes.txt", "mimeType": "text/plain",
                    "size": 11, "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert len(body["documents"]) == 1
    doc_out = body["documents"][0]
    assert doc_out["filename"] == "notes.txt"
    assert doc_out["byte_size"] == 11

    async with session_factory() as db:
        doc = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().first()
    assert doc.extracted_text == "hello world"
    assert doc.meta["source"] == {
        "provider": "gdrive", "file_id": "file-1", "drive_id": None, "name": "notes.txt",
        "modified_at": "2026-08-01T00:00:00Z", "imported_at": doc.meta["source"]["imported_at"],
    }

    # appears via the existing document-listing endpoint
    listed = await client.get("/api/documents")
    assert [d["filename"] for d in listed.json()] == ["notes.txt"]


async def test_import_gdrive_native_doc_is_exported_to_docx(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    user = make_user("import-native@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    docx_mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/doc-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "doc-1", "name": "Q3 Report", "mimeType": "application/vnd.google-apps.document",
                    "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/doc-1/export", params={"mimeType": docx_mime}).mock(
            return_value=httpx.Response(200, content=b"fake-docx-bytes", headers={"content-type": docx_mime})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "doc-1", "name": "Q3 Report", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["documents"][0]["filename"] == "Q3 Report.docx"
    assert body["documents"][0]["content_type"] == docx_mime


async def test_import_gdrive_uses_the_providers_name_not_the_request_bodys(client, seed, session_factory):
    """The request body's `name` is only the display name the picker showed
    when the file was selected — Drive's own metadata `name` is what actually
    gets stored as the filename and recorded as provenance."""
    workspace = make_workspace("Alpha")
    user = make_user("import-provider-name-gdrive@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1", "name": "renamed-on-drive.txt", "mimeType": "text/plain",
                    "size": 11, "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "stale-picker-name.txt", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["documents"][0]["filename"] == "renamed-on-drive.txt"

    async with session_factory() as db:
        doc = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().first()
    assert doc.filename == "renamed-on-drive.txt"
    assert doc.meta["source"]["name"] == "renamed-on-drive.txt"


async def test_import_gdrive_falls_back_to_the_request_bodys_name_when_metadata_omits_it(
    client, seed, session_factory
):
    """A provider response that omits `name` entirely (not every field in
    the `fields=` request is guaranteed present) falls back to the request
    body's own name rather than producing a nameless document."""
    workspace = make_workspace("Alpha")
    user = make_user("import-fallback-name-gdrive@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={"id": "file-1", "mimeType": "text/plain", "size": 11, "modifiedTime": "2026-08-01T00:00:00Z"},
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "picker-name.txt", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["documents"][0]["filename"] == "picker-name.txt"

    async with session_factory() as db:
        doc = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().first()
    assert doc.meta["source"]["name"] == "picker-name.txt"


async def test_import_m365_uses_the_providers_name_not_the_request_bodys(client, seed, session_factory, monkeypatch):
    """Same authoritative-name contract as gdrive, for Graph's own item
    metadata `name`."""
    workspace = make_workspace("Alpha")
    user = make_user("import-provider-name-m365@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "item-1", "name": "renamed-on-sharepoint.csv", "size": 8,
                    "lastModifiedDateTime": "2026-08-02T00:00:00Z",
                    "file": {"mimeType": "text/csv"},
                },
            )
        )
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2\n")
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "m365", "items": [{"id": "item-1", "name": "stale-picker-name.csv", "drive_id": "drive-1"}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["documents"][0]["filename"] == "renamed-on-sharepoint.csv"

    async with session_factory() as db:
        doc = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().first()
    assert doc.filename == "renamed-on-sharepoint.csv"
    assert doc.meta["source"]["name"] == "renamed-on-sharepoint.csv"


async def test_import_m365_follows_the_content_redirect(client, seed, session_factory, monkeypatch):
    workspace = make_workspace("Alpha")
    user = make_user("import-m365@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    # The redirect target (a signed-blob host Microsoft's own response names,
    # not a `PROVIDER_SPECS` literal) is now verified as a public address
    # (VERIFY_PUBLIC — see services/connections.py::download_m365_file) before
    # this hop is fetched. DNS is stubbed, same as tests/test_egress_guard.py's
    # own `resolves` fixture: this suite is about the redirect-following
    # logic, not the resolver, and a real lookup is a test that fails on a
    # train.
    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "item-1", "name": "budget.csv", "size": 20,
                    "lastModifiedDateTime": "2026-08-02T00:00:00Z",
                    "file": {"mimeType": "text/csv"},
                },
            )
        )
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(302, headers={"location": BLOB_CONTENT_URL})
        )
        mock.get(BLOB_CONTENT_URL).mock(return_value=httpx.Response(200, content=b"a,b\n1,2\n"))
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "m365", "items": [{"id": "item-1", "name": "budget.csv", "drive_id": "drive-1"}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["errors"] == []
    assert body["documents"][0]["filename"] == "budget.csv"

    async with session_factory() as db:
        doc = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().first()
    assert doc.storage_path
    from pathlib import Path

    assert Path(doc.storage_path).read_bytes() == b"a,b\n1,2\n"
    assert doc.meta["source"]["drive_id"] == "drive-1"


async def test_import_size_cap_lands_one_item_in_errors_the_other_succeeds(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    user = make_user("import-cap@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/huge-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "huge-1", "name": "huge.bin", "mimeType": "application/octet-stream",
                    "size": 60 * 1024 * 1024,
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/small-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200, json={"id": "small-1", "name": "small.txt", "mimeType": "text/plain", "size": 5}
            )
        )
        mock.get(f"{GDRIVE_API}/files/small-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={
                "provider": "gdrive",
                "items": [
                    {"id": "huge-1", "name": "huge.bin", "drive_id": None},
                    {"id": "small-1", "name": "small.txt", "drive_id": None},
                ],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["filename"] == "small.txt"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == "huge-1"
    assert "25MB" in body["errors"][0]["detail"]

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert len(docs) == 1  # the oversized item never became a row


async def test_import_db_failure_on_one_item_lands_in_errors_the_other_succeeds(
    client, seed, session_factory, monkeypatch, storage
):
    """A per-item DB failure (here: a NOT NULL violation on `filename`,
    forced by mangling the row `ingest_document` hands back) must land in
    that item's `errors` entry and roll the session back — not blow up the
    request or poison the next item's own add/commit. The failing item is
    processed *first* so this also proves the rollback actually clears the
    session for reuse, not just that a later item happens to succeed on its
    own."""
    workspace = make_workspace("Alpha")
    user = make_user("import-dbfail@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    real_ingest_document = documents_api.ingest_document

    async def _flaky_ingest_document(*, filename, **kwargs):
        doc = await real_ingest_document(filename=filename, **kwargs)
        if filename == "bad.txt":
            doc.filename = None  # violates the NOT NULL column -> IntegrityError on commit
        return doc

    monkeypatch.setattr(documents_api, "ingest_document", _flaky_ingest_document)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        for file_id, name, content in [("bad-1", "bad.txt", b"boom"), ("good-1", "good.txt", b"hello")]:
            mock.get(f"{GDRIVE_API}/files/{file_id}", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "id": file_id, "name": name, "mimeType": "text/plain",
                        "size": len(content), "modifiedTime": "2026-08-01T00:00:00Z",
                    },
                )
            )
            mock.get(f"{GDRIVE_API}/files/{file_id}", params={"alt": "media"}).mock(
                return_value=httpx.Response(200, content=content, headers={"content-type": "text/plain"})
            )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={
                "provider": "gdrive",
                "items": [
                    {"id": "bad-1", "name": "bad.txt", "drive_id": None},
                    {"id": "good-1", "name": "good.txt", "drive_id": None},
                ],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["filename"] == "good.txt"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == "bad-1"

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert [d.filename for d in docs] == ["good.txt"]  # the failed insert never landed

    # The bad item's bytes must not be orphaned on disk once its commit
    # failed and rolled back — only the successful item's file remains.
    stored_files = [p.name for p in storage.iterdir()]
    assert len(stored_files) == 1
    assert stored_files[0].endswith("-good.txt")


async def test_import_ingest_failure_does_not_delete_a_pre_existing_file_at_the_same_sha_path(
    client, seed, session_factory, storage, monkeypatch
):
    """Storage paths are sha-keyed: if a file with the exact bytes being
    imported already sits on disk (another Document row owns it) and this
    import's own ingestion then fails, the pre-existing file must survive —
    only bytes *this* import itself wrote may be cleaned up."""
    workspace = make_workspace("Alpha")
    user = make_user("import-preexisting-sha@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    content = b"already on disk"
    sha = hashlib.sha256(content).hexdigest()
    storage.mkdir(parents=True, exist_ok=True)
    pre_existing_path = storage / f"{sha}-existing.txt"
    pre_existing_path.write_bytes(content)

    async def _failing_ingest_document(*, filename, **kwargs):
        raise RuntimeError("boom: ingestion blew up after the file was written")

    monkeypatch.setattr(documents_api, "ingest_document", _failing_ingest_document)

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1", "name": "existing.txt", "mimeType": "text/plain",
                    "size": len(content), "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=content, headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "existing.txt", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["documents"] == []
    assert len(body["errors"]) == 1

    # The file this import "created" was actually already there — it must
    # not have been unlinked, and its bytes must be untouched.
    assert pre_existing_path.exists()
    assert pre_existing_path.read_bytes() == content


async def test_import_ingest_failure_unlinks_a_file_this_import_itself_created(
    client, seed, session_factory, storage, monkeypatch
):
    """The other half of the sha-named-path contract: when this import is
    the one that actually wrote the file (no pre-existing Document owns that
    sha), a later ingestion failure must unlink it — `created_path` is not
    `None` in that case, so the exception handler in `import_documents`
    cleans it up rather than leaving it orphaned on disk."""
    workspace = make_workspace("Alpha")
    user = make_user("import-created-unlink@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    async def _flaky_ingest_document(*, filename, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(documents_api, "ingest_document", _flaky_ingest_document)

    content = b"brand new bytes, nobody owns this sha yet"
    sha = hashlib.sha256(content).hexdigest()

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1", "name": "new.txt", "mimeType": "text/plain",
                    "size": len(content), "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=content, headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "new.txt", "drive_id": None}]},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["documents"] == []
    assert len(body["errors"]) == 1
    assert body["errors"][0]["id"] == "file-1"

    stored_files = [p.name for p in storage.iterdir()] if storage.exists() else []
    assert f"{sha}-new.txt" not in stored_files

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert docs == []


async def test_import_with_a_revoked_connection_is_409_and_touches_nothing(client, seed, session_factory):
    workspace = make_workspace("Alpha")
    user = make_user("import-revoked@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.post(GDRIVE_TOKEN_URL).mock(
            return_value=httpx.Response(400, json={"error": "invalid_grant", "error_description": "revoked"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
        )
    assert response.status_code == 409
    assert "reconnect" in response.json()["detail"].lower()

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert docs == []


async def test_import_with_an_errored_connection_is_409_and_creates_no_documents(client, seed, session_factory):
    """Same short-circuit as m365 browse, for the import endpoint: a
    connection already in `status='error'` is refused without a provider
    round trip, and no `Document` row is created."""
    workspace = make_workspace("Alpha")
    user = make_user("import-errored@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive", status="error")
    conn.error_detail = "the gdrive connection is in an error state and must be reconnected"
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(GDRIVE_TOKEN_URL).mock(return_value=httpx.Response(200))
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
        )
        assert token_route.called is False
    assert response.status_code == 409
    assert "reconnect" in response.json()["detail"].lower()

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert docs == []


# ── access-token cache (item 3) ───────────────────────────────────────────────
async def test_m365_browse_twice_performs_only_one_token_refresh(client, seed):
    """Two consecutive browse calls against the same connection must not
    each trade the refresh token for a new access token — the second call
    should be served from the short-lived in-process cache."""
    workspace = make_workspace("Alpha")
    user = make_user("browse-cache@example.com")
    conn = make_connection(workspace, provider="m365")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), conn)
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        token_route = mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "m365-access-token", "expires_in": 3600})
        )
        sites_route = mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "name": "OneDrive"})
        )
        first = await client.get("/api/connections/m365/browse")
        second = await client.get("/api/connections/m365/browse")
    assert first.status_code == 200
    assert second.status_code == 200
    assert token_route.call_count == 1
    assert sites_route.call_count == 2


async def test_import_workspace_isolation_returns_404_for_another_workspaces_project(client, seed):
    a = make_workspace("Alpha")
    b = make_workspace("Bravo")
    user_b = make_user("iso-import@example.com")
    project_a = make_project(a)
    conn_b = make_connection(b, provider="gdrive")
    await seed(a, b, user_b, make_member(user_b, b, role="analyst"), project_a, conn_b)
    await login(client, user_b.email)

    response = await client.post(
        f"/api/projects/{project_a.id}/documents/import",
        json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
    )
    assert response.status_code == 404


async def test_import_batch_over_50_items_is_422(client, seed):
    """`ImportBody.items` is capped at 50 (Field max_length) so one request
    can't queue an unbounded pile of imports — 51 items must fail validation
    before anything is touched, not get silently truncated or processed."""
    workspace = make_workspace("Alpha")
    user = make_user("import-batch-limit@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    items = [{"id": f"file-{i}", "name": f"file-{i}.txt", "drive_id": None} for i in range(51)]
    response = await client.post(
        f"/api/projects/{project.id}/documents/import",
        json={"provider": "gdrive", "items": items},
    )
    assert response.status_code == 422


# ── import: the "use" gate ────────────────────────────────────────────────────
async def test_import_is_blocked_by_a_registered_workspace_gate_and_touches_nothing(
    client, seed, session_factory
):
    """A lapsed plan must stop an import before anything is downloaded or
    written: no `Document` row is created and no provider HTTP call is
    made."""
    workspace = make_workspace("Alpha")
    user = make_user("import-gate@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "connections.use"
        assert workspace_id == workspace.id
        return GateResult(
            allowed=False,
            reason="connections_plan_required",
            detail="Connections require an active plan.",
        )

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    with respx.mock(assert_all_called=False) as mock:
        token_route = mock.post(GDRIVE_TOKEN_URL).mock(return_value=httpx.Response(200))
        meta_route = mock.get(f"{GDRIVE_API}/files/file-1").mock(return_value=httpx.Response(200, json={}))
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
        )
        assert token_route.called is False
        assert meta_route.called is False

    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "connections_plan_required"

    async with session_factory() as db:
        docs = (await db.execute(select(Document).where(Document.project_id == project.id))).scalars().all()
    assert docs == []


async def test_import_asks_the_gate_with_the_use_action(client, seed, session_factory):
    """The happy path asks the gate `connections.use`, never `connections.
    connect` — import exercises a connection that already exists."""
    workspace = make_workspace("Alpha")
    user = make_user("import-gate-allow@example.com")
    project = make_project(workspace)
    conn = make_connection(workspace, provider="gdrive")
    await seed(workspace, user, make_member(user, workspace, role="analyst"), project, conn)
    await login(client, user.email)

    asked_actions: list[str] = []
    ext = ExtensionAPI(None)

    async def recorder(db, workspace_id, action):
        asked_actions.append(action)
        return GateResult(allowed=True)

    ext.add_workspace_gate(recorder)
    extensions_module._registry = ext

    with respx.mock(assert_all_called=True) as mock:
        mock_gdrive_refresh(mock)
        mock.get(f"{GDRIVE_API}/files/file-1", params={"fields": "id,name,mimeType,size,modifiedTime"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "file-1", "name": "notes.txt", "mimeType": "text/plain",
                    "size": 11, "modifiedTime": "2026-08-01T00:00:00Z",
                },
            )
        )
        mock.get(f"{GDRIVE_API}/files/file-1", params={"alt": "media"}).mock(
            return_value=httpx.Response(200, content=b"hello world", headers={"content-type": "text/plain"})
        )
        response = await client.post(
            f"/api/projects/{project.id}/documents/import",
            json={"provider": "gdrive", "items": [{"id": "file-1", "name": "notes.txt", "drive_id": None}]},
        )

    assert response.status_code == 200, response.text
    assert asked_actions == ["connections.use"]
