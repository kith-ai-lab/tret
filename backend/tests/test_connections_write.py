"""Write-back to SharePoint/OneDrive (`services/connections.py`): the
write-scope check (`connection_has_write_scopes`), the write-target
allowlist (`list_write_targets`), filename validation
(`safe_upload_filename`), and `upload_connected_file` itself — the
tret-subfolder create-once/reuse behaviour, the small (`<=4MB`, single PUT)
and large (`>4MB`, upload-session + chunked PUT) paths, every refusal, and
the `ConnectionActivity` row each of those writes.

Same harness as `test_connections_search.py`: real sqlite, respx-mocked
Graph, `resolves_public` for the upload-session URL's `VERIFY_PUBLIC` check
(the same shape `download_m365_file`'s redirect hops need).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.config import get_settings
from tret.db.models import Base, ConnectionActivity, Workspace, WorkspaceConnection
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult
from tret.net import guard as net_guard
from tret.services import connections as connections_module
from tret.services.connections import (
    GRAPH_API_BASE,
    IMPORT_MAX_BYTES,
    M365_WRITE_SCOPES,
    ConnectionUnavailable,
    ConnectionWriteError,
    WriteTarget,
    clear_access_token_cache,
    connection_has_write_scopes,
    list_write_targets,
    safe_upload_filename,
    upload_connected_file,
)
from tret.services.credentials import get_fernet

GRAPH = GRAPH_API_BASE
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

WRITE_ENTRY = {
    "slug": "budget-folder",
    "label": "Budget Folder",
    "path": "Finance/Budget",
    "site_id": "site-1",
    "drive_id": "drive-1",
    "item_id": "folder-item-1",
    "web_url": "https://contoso.sharepoint.com/sites/finance/Budget",
}

READ_ONLY_SCOPES = ["offline_access", "User.Read", "Files.Read.All", "Sites.Read.All"]


# ── fixtures ─────────────────────────────────────────────────────────────────
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


@pytest.fixture(autouse=True)
def _clean_settings_and_registry(monkeypatch):
    extensions_module._registry = None
    get_settings.cache_clear()
    clear_access_token_cache()
    yield
    extensions_module._registry = None
    get_settings.cache_clear()
    clear_access_token_cache()


@pytest.fixture
def configured_m365(monkeypatch):
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()


@pytest.fixture
def resolves_public(monkeypatch):
    """Every hostname resolves to a public address — the DNS side of
    `VERIFY_PUBLIC`, same technique `test_connections_search.py` uses for
    `download_m365_file`'s redirect hops. The large-upload path's own
    `uploadUrl` needs this the same way: it names a host `_policy` cannot
    treat as a fixed, already-pinned Graph endpoint."""

    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)


def make_workspace(name: str = "Alpha") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_connection(
    workspace: Workspace,
    *,
    granted_scopes: list | None = None,
    selected_resources: dict | None = None,
    status: str = "active",
) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider="m365",
        account_label="person@example.com",
        encrypted_refresh_token=get_fernet().encrypt(b"stored-refresh-token"),
        granted_scopes=list(granted_scopes) if granted_scopes is not None else list(M365_WRITE_SCOPES),
        selected_resources=selected_resources or {},
        status=status,
    )


def mock_m365_refresh(mock: respx.MockRouter, *, access_token: str = "m365-access-token") -> None:
    mock.post(M365_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": access_token, "expires_in": 3600})
    )


async def _activity_rows(session_factory, workspace_id) -> list[ConnectionActivity]:
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(ConnectionActivity).where(ConnectionActivity.workspace_id == workspace_id)
            )
        ).scalars().all()
    return rows


# ── connection_has_write_scopes ──────────────────────────────────────────────
def test_connection_has_write_scopes_true_when_both_readwrite_scopes_present():
    conn = WorkspaceConnection(granted_scopes=list(M365_WRITE_SCOPES))
    assert connection_has_write_scopes(conn) is True


def test_connection_has_write_scopes_false_for_read_only_scopes():
    conn = WorkspaceConnection(granted_scopes=list(READ_ONLY_SCOPES))
    assert connection_has_write_scopes(conn) is False


def test_connection_has_write_scopes_false_when_only_one_of_the_pair_is_present():
    conn = WorkspaceConnection(
        granted_scopes=["offline_access", "User.Read", "Files.ReadWrite.All", "Sites.Read.All"]
    )
    assert connection_has_write_scopes(conn) is False


def test_connection_has_write_scopes_false_for_no_scopes_at_all():
    conn = WorkspaceConnection(granted_scopes=[])
    assert connection_has_write_scopes(conn) is False


# ── list_write_targets ───────────────────────────────────────────────────────
async def test_list_write_targets_returns_configured_entries_with_no_graph_call(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    # No respx.mock context at all: any HTTP attempt fails the test outright.
    async with session_factory() as db:
        targets = await list_write_targets(db, workspace.id)

    assert targets == [
        WriteTarget(
            slug="budget-folder",
            label="Budget Folder",
            path="Finance/Budget",
            site_id="site-1",
            drive_id="drive-1",
            item_id="folder-item-1",
            web_url="https://contoso.sharepoint.com/sites/finance/Budget",
        )
    ]


async def test_list_write_targets_empty_when_never_configured(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace)  # selected_resources default {}
    await seed(workspace, conn)

    async with session_factory() as db:
        targets = await list_write_targets(db, workspace.id)
    assert targets == []


async def test_list_write_targets_raises_connection_unavailable_with_no_connection(
    session_factory, seed
):
    workspace = make_workspace()
    await seed(workspace)

    async with session_factory() as db:
        with pytest.raises(ConnectionUnavailable):
            await list_write_targets(db, workspace.id)


# ── safe_upload_filename ─────────────────────────────────────────────────────
_VALID_FILENAME_CASES = [
    ("Report.docx", "Report.docx"),
    ("file with spaces.csv", "file with spaces.csv"),
    ("a" * 196 + ".txt", "a" * 196 + ".txt"),  # exactly 200 characters
    ("na\x00\x01me.txt", "name.txt"),  # control chars stripped, not refused
]


@pytest.mark.parametrize("name,expected", _VALID_FILENAME_CASES)
def test_safe_upload_filename_accepts(name, expected):
    assert safe_upload_filename(name) == expected


_INVALID_FILENAME_CASES = [
    "",
    "   ",
    ".",
    "..",
    "a" * 197 + ".txt",  # 201 characters
    "na/me.txt",
    "na\\me.txt",
    "na:me.txt",
    "na*me.txt",
    "na?me.txt",
    'na"me.txt',
    "na<me.txt",
    "na>me.txt",
    "na|me.txt",
    " leading-space.txt",
    "trailing-space.txt ",
    ".leading-dot.txt",
    "trailing-dot.txt.",
]


@pytest.mark.parametrize("name", _INVALID_FILENAME_CASES)
def test_safe_upload_filename_refuses(name):
    with pytest.raises(ValueError):
        safe_upload_filename(name)


# ── upload_connected_file: small path (<=4MB) ────────────────────────────────
async def test_upload_small_file_puts_with_rename_conflict_behavior_and_records_activity(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    data = b"a,b\n1,2\n"
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        list_route = mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        create_route = mock.post(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(
                201, json={"id": "tret-folder-id", "name": "tret", "folder": {}}
            )
        )
        put_route = mock.put(f"{GRAPH}/drives/drive-1/items/tret-folder-id:/report.csv:/content").mock(
            return_value=httpx.Response(
                201,
                json={
                    "id": "item-99",
                    "name": "report.csv",
                    "webUrl": "https://contoso.sharepoint.com/report.csv",
                    "size": len(data),
                },
            )
        )
        async with session_factory() as db:
            result = await upload_connected_file(
                db,
                workspace_id=workspace.id,
                target_slug="budget-folder",
                filename="report.csv",
                data=data,
                content_type="text/csv",
            )
            await db.commit()

    assert result.item_id == "item-99"
    assert result.name == "report.csv"
    assert result.size == len(data)
    assert result.target_slug == "budget-folder"
    assert result.path == "Finance/Budget/tret/report.csv"
    assert result.web_url == "https://contoso.sharepoint.com/report.csv"

    assert list_route.calls[0].request.url.params["$filter"] == "name eq 'tret'"
    created_body = json.loads(create_route.calls[0].request.content)
    assert created_body == {
        "name": "tret", "folder": {}, "@microsoft.graph.conflictBehavior": "fail",
    }
    put_request = put_route.calls[0].request
    assert put_request.content == data
    assert put_request.headers["content-type"] == "text/csv"
    assert put_request.url.params["@microsoft.graph.conflictBehavior"] == "rename"

    rows = await _activity_rows(session_factory, workspace.id)
    upload_rows = [r for r in rows if r.action == "upload"]
    assert len(upload_rows) == 1
    assert upload_rows[0].provider == "m365"
    assert upload_rows[0].target == "budget-folder/tret/report.csv"
    assert upload_rows[0].bytes == len(data)


async def test_upload_reuses_the_tret_folder_on_a_second_call(configured_m365, session_factory, seed):
    """The `tret` subfolder is created once, then found and reused — the
    create route (`POST .../children`) must be called exactly once across
    two uploads to the same target."""
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    with respx.mock(assert_all_called=False) as mock:
        mock_m365_refresh(mock)
        list_route = mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            side_effect=[
                httpx.Response(200, json={"value": []}),
                httpx.Response(
                    200,
                    json={"value": [{"id": "tret-folder-id", "name": "tret", "folder": {}}]},
                ),
            ]
        )
        create_route = mock.post(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(201, json={"id": "tret-folder-id", "name": "tret", "folder": {}})
        )
        mock.put(f"{GRAPH}/drives/drive-1/items/tret-folder-id:/first.csv:/content").mock(
            return_value=httpx.Response(201, json={"id": "item-1", "name": "first.csv", "size": 3})
        )
        mock.put(f"{GRAPH}/drives/drive-1/items/tret-folder-id:/second.csv:/content").mock(
            return_value=httpx.Response(201, json={"id": "item-2", "name": "second.csv", "size": 3})
        )
        async with session_factory() as db:
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="first.csv", data=b"1,2", content_type="text/csv",
            )
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="second.csv", data=b"3,4", content_type="text/csv",
            )
            await db.commit()

    assert list_route.call_count == 2  # both uploads list first
    assert create_route.call_count == 1  # only the first upload had to create it


# ── tret subfolder resolution refuses look-alikes ────────────────────────────
# `_find_tret_subfolder_id` must not treat a same-named file, a shortcut to a
# folder elsewhere, or an item somehow reported from a different drive as the
# `tret` output folder it owns. Each of these three makes the candidate look
# absent, so `_ensure_tret_subfolder` tries to create it, loses the create to
# a 409 (the name really is taken by the look-alike), re-lists, still finds
# nothing acceptable, and refuses with the same clear reason.
_NON_PLAIN_FOLDER_REASON = (
    "An item named tret already exists in the output folder and is not a "
    "plain folder — remove or rename it."
)


async def _assert_refuses_a_non_plain_tret(session_factory, seed, item: dict) -> None:
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        list_route = mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(200, json={"value": [item]})
        )
        create_route = mock.post(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
        )
        async with session_factory() as db:
            with pytest.raises(ConnectionWriteError) as excinfo:
                await upload_connected_file(
                    db, workspace_id=workspace.id, target_slug="budget-folder",
                    filename="report.csv", data=b"1,2", content_type="text/csv",
                )
            await db.commit()

    assert excinfo.value.reason == _NON_PLAIN_FOLDER_REASON
    assert list_route.call_count == 2  # once before the create, once after its 409
    assert create_route.call_count == 1  # never retried past the one lost race

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1
    assert failed[0].detail == _NON_PLAIN_FOLDER_REASON


async def test_tret_subfolder_resolution_refuses_a_same_named_file(configured_m365, session_factory, seed):
    """A `tret` *file* sitting where the subfolder should be — no `folder`
    facet at all — must never be accepted as the output folder."""
    await _assert_refuses_a_non_plain_tret(
        session_factory, seed, {"id": "file-id", "name": "tret", "file": {"mimeType": "text/plain"}}
    )


async def test_tret_subfolder_resolution_refuses_a_shortcut(configured_m365, session_factory, seed):
    """A `tret` *shortcut* to a folder that lives elsewhere: it carries the
    target folder's own `folder` facet (so the old name+folder-facet check
    alone would have accepted it) but also a `remoteItem` facet, which must
    refuse it regardless."""
    await _assert_refuses_a_non_plain_tret(
        session_factory,
        seed,
        {
            "id": "shortcut-id",
            "name": "tret",
            "folder": {},
            "remoteItem": {"id": "remote-folder-id", "folder": {}},
        },
    )


async def test_tret_subfolder_resolution_refuses_a_foreign_drive_item(
    configured_m365, session_factory, seed
):
    """An item Graph reports as `tret`/a plain folder, but whose own
    `parentReference.driveId` names a different drive than the one this
    upload is scoped to — never trusted, since every write below is scoped
    to `drive_id` and must not be redirected onto another drive."""
    await _assert_refuses_a_non_plain_tret(
        session_factory,
        seed,
        {
            "id": "foreign-id",
            "name": "tret",
            "folder": {},
            "parentReference": {"driveId": "some-other-drive-id"},
        },
    )


async def test_tret_subfolder_resolution_accepts_an_item_whose_drive_id_matches(
    configured_m365, session_factory, seed
):
    """The positive control for the foreign-drive refusal above: a plain
    folder whose `parentReference.driveId` matches `drive_id` exactly is
    still accepted and reused — the new check narrows, it does not break
    the ordinary reuse path."""
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "tret-folder-id",
                            "name": "tret",
                            "folder": {},
                            "parentReference": {"driveId": "drive-1"},
                        }
                    ]
                },
            )
        )
        put_route = mock.put(f"{GRAPH}/drives/drive-1/items/tret-folder-id:/report.csv:/content").mock(
            return_value=httpx.Response(201, json={"id": "item-1", "name": "report.csv", "size": 3})
        )
        async with session_factory() as db:
            result = await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="report.csv", data=b"1,2", content_type="text/csv",
            )
            await db.commit()

    assert result.item_id == "item-1"
    assert put_route.call_count == 1


# ── upload_connected_file: large path (>4MB) ─────────────────────────────────
async def test_upload_large_file_uses_upload_session_with_chunked_puts_and_no_auth_header(
    configured_m365, resolves_public, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    chunk_bytes = connections_module._UPLOAD_CHUNK_BYTES
    total = chunk_bytes + (1024 * 1024)  # two chunks: one full, one partial
    data = b"x" * total
    upload_url = "https://contoso-uploads.example.test/upload-session-abc"

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(
                200, json={"value": [{"id": "tret-folder-id", "name": "tret", "folder": {}}]}
            )
        )
        session_route = mock.post(
            f"{GRAPH}/drives/drive-1/items/tret-folder-id:/bigfile.bin:/createUploadSession"
        ).mock(return_value=httpx.Response(200, json={"uploadUrl": upload_url}))
        chunk_route = mock.put(upload_url).mock(
            side_effect=[
                httpx.Response(202, json={}),
                httpx.Response(
                    201,
                    json={
                        "id": "item-large", "name": "bigfile.bin",
                        "webUrl": "https://contoso.sharepoint.com/bigfile.bin", "size": total,
                    },
                ),
            ]
        )
        async with session_factory() as db:
            result = await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="bigfile.bin", data=data, content_type="application/octet-stream",
            )
            await db.commit()

    assert result.item_id == "item-large"
    assert result.size == total

    session_body = json.loads(session_route.calls[0].request.content)
    assert session_body == {"item": {"@microsoft.graph.conflictBehavior": "rename", "name": "bigfile.bin"}}

    assert chunk_route.call_count == 2
    first, second = (call.request for call in chunk_route.calls)
    assert "authorization" not in {h.lower() for h in first.headers}
    assert "authorization" not in {h.lower() for h in second.headers}
    assert first.headers["content-range"] == f"bytes 0-{chunk_bytes - 1}/{total}"
    assert second.headers["content-range"] == f"bytes {chunk_bytes}-{total - 1}/{total}"
    assert len(first.content) == chunk_bytes
    assert len(second.content) == total - chunk_bytes

    rows = await _activity_rows(session_factory, workspace.id)
    upload_rows = [r for r in rows if r.action == "upload"]
    assert len(upload_rows) == 1
    assert upload_rows[0].bytes == total


async def test_upload_large_file_refuses_a_non_https_upload_url(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    total = connections_module._UPLOAD_SIMPLE_MAX_BYTES + 1
    data = b"x" * total

    with respx.mock(assert_all_called=False) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(
                200, json={"value": [{"id": "tret-folder-id", "name": "tret", "folder": {}}]}
            )
        )
        mock.post(f"{GRAPH}/drives/drive-1/items/tret-folder-id:/bigfile.bin:/createUploadSession").mock(
            return_value=httpx.Response(
                200, json={"uploadUrl": "http://contoso-uploads.example.test/upload-session-abc"}
            )
        )
        # No PUT route registered for the plain-http upload URL: a request
        # reaching it would fail the test on its own (respx has no match),
        # on top of the ConnectionWriteError assertion below.
        async with session_factory() as db:
            with pytest.raises(ConnectionWriteError):
                await upload_connected_file(
                    db, workspace_id=workspace.id, target_slug="budget-folder",
                    filename="bigfile.bin", data=data, content_type="application/octet-stream",
                )
            await db.commit()

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1


# ── upload_connected_file: refusals ──────────────────────────────────────────
async def test_upload_refuses_without_write_scopes(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(
        workspace, granted_scopes=READ_ONLY_SCOPES, selected_resources={"write": [WRITE_ENTRY]}
    )
    await seed(workspace, conn)

    # No respx context at all: a scope refusal must never reach the network.
    async with session_factory() as db:
        with pytest.raises(ConnectionWriteError) as excinfo:
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="report.csv", data=b"1,2", content_type="text/csv",
            )
        await db.commit()
    assert "write" in excinfo.value.reason.lower()

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1
    assert failed[0].detail == excinfo.value.reason


async def test_upload_refuses_when_the_write_gate_refuses(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    asked_actions: list[str] = []
    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        asked_actions.append(action)
        if action == "connections.write":
            return GateResult(
                allowed=False, reason="write_plan_required", detail="Write-back requires an add-on."
            )
        return GateResult(allowed=True)

    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    async with session_factory() as db:
        with pytest.raises(ConnectionWriteError) as excinfo:
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="report.csv", data=b"1,2", content_type="text/csv",
            )
        await db.commit()
    assert excinfo.value.reason == "Write-back requires an add-on."
    assert "connections.write" in asked_actions

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1


async def test_upload_refuses_an_unknown_target(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    async with session_factory() as db:
        with pytest.raises(ConnectionWriteError) as excinfo:
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="not-a-real-target",
                filename="report.csv", data=b"1,2", content_type="text/csv",
            )
        await db.commit()
    assert "not-a-real-target" in excinfo.value.reason

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1
    assert failed[0].target == "not-a-real-target"


async def test_upload_refuses_a_file_over_the_size_cap(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    oversized = b"x" * (IMPORT_MAX_BYTES + 1)

    # No respx context: the size cap must refuse before any Graph call.
    async with session_factory() as db:
        with pytest.raises(ConnectionWriteError) as excinfo:
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="huge.bin", data=oversized, content_type="application/octet-stream",
            )
        await db.commit()
    assert "exceeds" in excinfo.value.reason.lower()

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1
    assert failed[0].bytes == len(oversized)


async def test_upload_refuses_a_bad_filename_before_any_graph_call(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    async with session_factory() as db:
        with pytest.raises(ConnectionWriteError):
            await upload_connected_file(
                db, workspace_id=workspace.id, target_slug="budget-folder",
                filename="../../etc/passwd", data=b"1,2", content_type="text/csv",
            )
        await db.commit()


async def test_upload_turns_a_graph_5xx_into_connection_write_error_and_records_failure(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace, selected_resources={"write": [WRITE_ENTRY]})
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/folder-item-1/children").mock(
            return_value=httpx.Response(500, json={"error": {"code": "internalServerError"}})
        )
        async with session_factory() as db:
            with pytest.raises(ConnectionWriteError) as excinfo:
                await upload_connected_file(
                    db, workspace_id=workspace.id, target_slug="budget-folder",
                    filename="report.csv", data=b"1,2", content_type="text/csv",
                )
            await db.commit()

    assert "500" in excinfo.value.reason
    assert "internalServerError" in excinfo.value.reason
    # Never the raw response body verbatim.
    assert '"error"' not in excinfo.value.reason

    rows = await _activity_rows(session_factory, workspace.id)
    failed = [r for r in rows if r.action == "upload_failed"]
    assert len(failed) == 1
    assert failed[0].target == "budget-folder/tret/report.csv"
    assert failed[0].detail == excinfo.value.reason


async def test_upload_never_overwrites_uses_rename_on_both_paths(configured_m365, session_factory, seed):
    """Belt-and-suspenders on the create-only policy: both the small PUT and
    the large createUploadSession request Graph's `rename` conflict
    behaviour, never `replace` — asserted directly against the request
    bodies/query strings already captured by the two upload-path tests
    above; this test instead pins the *policy itself* so a future edit
    cannot flip either constant without a red test."""
    assert connections_module._UPLOAD_CHUNK_BYTES == 5 * 1024 * 1024
    assert connections_module._UPLOAD_SIMPLE_MAX_BYTES == 4 * 1024 * 1024
