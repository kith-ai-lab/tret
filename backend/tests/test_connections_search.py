"""Live SharePoint/OneDrive access (`services/connections.py`): the read
allowlist (`list_connected_sources`, restricted vs. derived, its own
in-process cache), full-text search over it (`search_connected_files`, the
Search-API-vs-personal-account fallback), pulling one hit into a project's
documents (`materialize_connected_file`, dedupe/size-cap/allowlist), and the
`item_ref` codec the three share.

Same harness as `test_connections_service.py` (real sqlite, respx-mocked
Graph) plus a `storage` fixture for `materialize_connected_file`'s
filesystem side, borrowed from `test_connections_import_api.py`.
"""
from __future__ import annotations

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
from tret.db.models import Base, Document, Project, Workspace, WorkspaceConnection
from tret.engine import extensions as extensions_module
from tret.net import guard as net_guard
from tret.services import connections as connections_module
from tret.services.connections import (
    GRAPH_API_BASE,
    IMPORT_MAX_BYTES,
    ConnectionUnavailable,
    DownloadTooLargeError,
    clear_access_token_cache,
    invalidate_connected_sources,
    list_connected_sources,
    make_item_ref,
    materialize_connected_file,
    parse_item_ref,
    search_connected_files,
)
from tret.services.credentials import get_fernet

GRAPH = GRAPH_API_BASE
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"


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
    """Same isolation test_connections_service.py gives itself."""
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
def storage(tmp_path, monkeypatch):
    directory = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(directory))
    return directory


@pytest.fixture
def resolves_public(monkeypatch):
    """Every hostname resolves to a public address — the DNS side of
    `VERIFY_PUBLIC`/`VERIFY_NONE` egress checks a real test run must not
    depend on a real lookup for. Same technique
    `test_connections_import_api.py` uses."""

    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)


def make_workspace(name: str = "Alpha") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_project(workspace: Workspace, name: str = "P") -> Project:
    return Project(id=uuid.uuid4(), workspace_id=workspace.id, name=name)


def make_connection(
    workspace: Workspace, *, selected_resources: dict | None = None, status: str = "active"
) -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider="m365",
        account_label="person@example.com",
        encrypted_refresh_token=get_fernet().encrypt(b"stored-refresh-token"),
        granted_scopes=["offline_access", "User.Read", "Files.Read.All", "Sites.Read.All"],
        selected_resources=selected_resources or {},
        status=status,
    )


def mock_m365_refresh(mock: respx.MockRouter, *, access_token: str = "m365-access-token") -> None:
    mock.post(M365_TOKEN_URL).mock(
        return_value=httpx.Response(200, json={"access_token": access_token, "expires_in": 3600})
    )


# ── item_ref codec ────────────────────────────────────────────────────────────
def test_make_item_ref_and_parse_item_ref_round_trip():
    ref = make_item_ref("m365", "drive-1", "item-1")
    assert ref == "m365:drive-1:item-1"
    assert parse_item_ref(ref) == ("m365", "drive-1", "item-1")


@pytest.mark.parametrize(
    "bad_ref", ["", "m365", "m365:drive-1", "m365::item-1", ":drive-1:item-1", "m365:drive-1:"]
)
def test_parse_item_ref_rejects_malformed_refs(bad_ref):
    with pytest.raises(ValueError):
        parse_item_ref(bad_ref)


def test_parse_item_ref_rejects_an_unknown_provider():
    with pytest.raises(ValueError):
        parse_item_ref("dropbox:drive-1:item-1")


# ── list_connected_sources: restricted vs. derived, cache + invalidation ────
async def test_list_connected_sources_returns_the_restricted_allowlist_verbatim(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(
        workspace,
        selected_resources={
            "read": [
                {
                    "site_id": "site-1",
                    "drive_id": "drive-1",
                    "label": "Finance",
                    "kind": "site_drive",
                    "web_url": "https://contoso.sharepoint.com/sites/finance/Documents",
                }
            ]
        },
    )
    await seed(workspace, conn)

    # No Graph call is made at all for the restricted branch — respx isn't
    # even entered, so any accidental HTTP call here fails the test outright.
    async with session_factory() as db:
        sources = await list_connected_sources(db, workspace.id)

    assert len(sources) == 1
    source = sources[0]
    assert source.provider == "m365"
    assert source.kind == "site_drive"
    assert source.label == "Finance"
    assert source.site_id == "site-1"
    assert source.drive_id == "drive-1"
    assert source.web_url == "https://contoso.sharepoint.com/sites/finance/Documents"
    assert source.slug  # non-empty, url-safe by construction


async def test_list_connected_sources_derives_every_site_drive_plus_onedrive(
    configured_m365, session_factory, seed
):
    """Unrestricted (`selected_resources` empty, the default): every site's
    default document library plus the account's own OneDrive — a site with
    no document library provisioned (`/drive` 404s) is skipped rather than
    failing the whole derive."""
    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "site-1", "displayName": "Finance"},
                        {"id": "site-2", "displayName": "No Library"},
                    ]
                },
            )
        )
        mock.get(f"{GRAPH}/sites/site-1/drive").mock(
            return_value=httpx.Response(
                200,
                json={
                    "id": "drive-1", "name": "Documents",
                    "webUrl": "https://contoso.sharepoint.com/sites/finance/Documents",
                },
            )
        )
        mock.get(f"{GRAPH}/sites/site-2/drive").mock(return_value=httpx.Response(404, json={}))
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(
                200, json={"id": "drive-me", "webUrl": "https://contoso-my.sharepoint.com/personal/x"}
            )
        )
        mock.get(f"{GRAPH}/me").mock(
            return_value=httpx.Response(200, json={"userPrincipalName": "person@tenant.onmicrosoft.com"})
        )

        async with session_factory() as db:
            sources = await list_connected_sources(db, workspace.id)

    by_kind = {s.kind: s for s in sources}
    assert len(sources) == 2  # site-2 skipped: no document library
    assert by_kind["site_drive"].drive_id == "drive-1"
    assert by_kind["site_drive"].label == "Finance / Documents"
    assert by_kind["onedrive"].drive_id == "drive-me"
    assert by_kind["onedrive"].label == "OneDrive (person@tenant.onmicrosoft.com)"


async def test_derive_connected_sources_follows_odata_next_link_pages(
    configured_m365, session_factory, seed
):
    """`/sites?search=*` can page — a derive that stops at the first page
    would silently miss every site after it."""
    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    next_link = f"{GRAPH}/sites?$skiptoken=page2"
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        page1 = mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [{"id": "site-1", "displayName": "One"}],
                    "@odata.nextLink": next_link,
                },
            )
        )
        page2 = mock.get(next_link).mock(
            return_value=httpx.Response(200, json={"value": [{"id": "site-2", "displayName": "Two"}]})
        )
        mock.get(f"{GRAPH}/sites/site-1/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-1", "name": "Documents"})
        )
        mock.get(f"{GRAPH}/sites/site-2/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-2", "name": "Documents"})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))

        async with session_factory() as db:
            sources = await list_connected_sources(db, workspace.id)

    drive_ids = {s.drive_id for s in sources if s.kind == "site_drive"}
    assert drive_ids == {"drive-1", "drive-2"}  # both pages' sites made it in
    assert page1.call_count == 1
    assert page2.call_count == 1


async def test_derive_connected_sources_skips_a_site_whose_drive_lookup_fails(
    configured_m365, session_factory, seed
):
    """A site with no default document library, or one Graph otherwise
    fails to answer for, must not take down the whole derive — it is
    skipped, and every other site's drive still comes back."""
    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "site-ok", "displayName": "OK"},
                        {"id": "site-flaky", "displayName": "Flaky"},
                    ]
                },
            )
        )
        mock.get(f"{GRAPH}/sites/site-ok/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-ok", "name": "Documents"})
        )
        mock.get(f"{GRAPH}/sites/site-flaky/drive").mock(return_value=httpx.Response(500, json={}))
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))

        async with session_factory() as db:
            sources = await list_connected_sources(db, workspace.id)

    site_drives = [s for s in sources if s.kind == "site_drive"]
    assert len(site_drives) == 1
    assert site_drives[0].drive_id == "drive-ok"


async def test_derive_connected_sources_caps_site_count_and_logs_once(
    configured_m365, session_factory, seed, monkeypatch, caplog
):
    """An unbounded tenant directory must not turn into an unbounded number
    of per-site `/drive` lookups — the derived site list is capped, and
    capping is logged exactly once, not once per trimmed site."""
    monkeypatch.setattr(connections_module, "_MAX_DERIVED_SITES", 2)

    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": [{"id": f"site-{i}"} for i in range(5)]})
        )
        mock.get(url__regex=rf"{GRAPH}/sites/site-\d/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-x", "name": "Documents"})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))

        with caplog.at_level("WARNING", logger="tret.connections"):
            async with session_factory() as db:
                sources = await list_connected_sources(db, workspace.id)

    assert len([s for s in sources if s.kind == "site_drive"]) == 2
    capped_records = [r for r in caplog.records if "capped" in r.message]
    assert len(capped_records) == 1


async def test_list_connected_sources_is_cached_and_invalidation_forces_a_re_derive(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        sites_route = mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))

        async with session_factory() as db:
            await list_connected_sources(db, workspace.id)
        async with session_factory() as db:
            await list_connected_sources(db, workspace.id)
        assert sites_route.call_count == 1  # second call served from cache

        invalidate_connected_sources(workspace.id, "m365")

        async with session_factory() as db:
            await list_connected_sources(db, workspace.id)
        assert sites_route.call_count == 2  # invalidation forced a re-derive


async def test_list_connected_sources_gate_is_checked_even_when_the_list_is_cached(
    configured_m365, session_factory, seed
):
    """The cache must not let a workspace whose connection has since gone
    unusable keep reading through a stale list — `ensure_connection_usable`
    has to run on every call, cache or not, not only on the call that
    misses the cache."""
    workspace = make_workspace()
    conn = make_connection(workspace)
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/sites", params={"search": "*"}).mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        mock.get(f"{GRAPH}/me/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-me", "webUrl": None})
        )
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))

        async with session_factory() as db:
            sources = await list_connected_sources(db, workspace.id)
        assert len(sources) == 1  # just the derived OneDrive; proves the call got cached

    # Flip the connection to a state ensure_connection_usable refuses — no
    # Graph call is mocked for this section, so if the cache short-circuited
    # the gate and tried to re-derive, this would fail with a routing error
    # rather than the ConnectionUnavailable we actually expect.
    async with session_factory() as db:
        db_conn = await db.get(WorkspaceConnection, conn.id)
        db_conn.status = "error"
        db_conn.error_detail = "token revoked"
        await db.commit()

    async with session_factory() as db:
        with pytest.raises(ConnectionUnavailable):
            await list_connected_sources(db, workspace.id)


async def test_list_connected_sources_raises_connection_unavailable_like_ensure_connection_usable(
    session_factory, seed
):
    workspace = make_workspace()
    await seed(workspace)  # no m365 connection at all

    async with session_factory() as db:
        with pytest.raises(ConnectionUnavailable):
            await list_connected_sources(db, workspace.id)


# ── search_connected_files ───────────────────────────────────────────────────
def _search_query_response(*, drive_id: str, item_id: str, name: str, summary: str | None) -> dict:
    return {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            {
                                "hitId": item_id,
                                "summary": summary,
                                "resource": {
                                    "id": item_id,
                                    "name": name,
                                    "webUrl": f"https://contoso.sharepoint.com/{name}",
                                    "lastModifiedDateTime": "2026-08-01T00:00:00Z",
                                    "size": 4096,
                                    "parentReference": {
                                        "driveId": drive_id,
                                        "path": f"/drives/{drive_id}/root:/Reports",
                                    },
                                },
                            }
                        ]
                    }
                ]
            }
        ]
    }


async def test_search_empty_query_raises_value_error(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, conn)

    async with session_factory() as db:
        with pytest.raises(ValueError):
            await search_connected_files(db, workspace.id, "   ")


async def test_search_clamps_max_results_above_twenty(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, conn)

    body = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            {
                                "summary": None,
                                "resource": {
                                    "id": f"item-{i}",
                                    "name": f"file-{i}.txt",
                                    "parentReference": {"driveId": "drive-1", "path": "/drive/root:"},
                                },
                            }
                            for i in range(30)
                        ]
                    }
                ]
            }
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(return_value=httpx.Response(200, json=body))
        async with session_factory() as db:
            hits = await search_connected_files(db, workspace.id, "budget", max_results=999)
    assert len(hits) == 20


async def test_search_via_search_api_filters_hits_to_allowed_drives(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(
        workspace,
        selected_resources={
            "read": [{"drive_id": "drive-allowed", "label": "Finance", "kind": "site_drive"}]
        },
    )
    await seed(workspace, conn)

    body = {
        "value": [
            {
                "hitsContainers": [
                    {
                        "hits": [
                            {
                                "summary": "<c0>Q3</c0> budget details",
                                "resource": {
                                    "id": "item-allowed",
                                    "name": "Q3 Budget.xlsx",
                                    "webUrl": "https://contoso.sharepoint.com/Q3%20Budget.xlsx",
                                    "lastModifiedDateTime": "2026-08-01T00:00:00Z",
                                    "size": 4096,
                                    "parentReference": {
                                        "driveId": "drive-allowed",
                                        "path": "/drives/drive-allowed/root:/Reports",
                                    },
                                },
                            },
                            {
                                "summary": "not visible to this workspace",
                                "resource": {
                                    "id": "item-other",
                                    "name": "Secret.xlsx",
                                    "parentReference": {
                                        "driveId": "drive-not-allowed",
                                        "path": "/drives/drive-not-allowed/root:",
                                    },
                                },
                            },
                        ]
                    }
                ]
            }
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(return_value=httpx.Response(200, json=body))
        async with session_factory() as db:
            hits = await search_connected_files(db, workspace.id, "budget")

    assert len(hits) == 1
    hit = hits[0]
    assert hit.item_ref == "m365:drive-allowed:item-allowed"
    assert hit.name == "Q3 Budget.xlsx"
    assert hit.path == "Finance/Reports/Q3 Budget.xlsx"
    assert hit.snippet == "Q3 budget details"  # markup stripped
    assert hit.source_slug  # resolved to the allowed source


async def test_search_falls_back_to_per_drive_search_for_a_personal_account(
    configured_m365, session_factory, seed
):
    """A 4xx from `/search/query` (the shape a personal Microsoft account's
    connection gets, since Search is a tenant-only surface) falls back to
    `GET /drives/{id}/root/search(q=...)` per allowed drive."""
    workspace = make_workspace()
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "My files", "kind": "onedrive"}]}
    )
    await seed(workspace, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(
            return_value=httpx.Response(400, json={"error": {"message": "not supported"}})
        )
        mock.get(url__regex=rf"{GRAPH}/drives/drive-1/root/search.*").mock(
            return_value=httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": "item-1",
                            "name": "notes.txt",
                            "parentReference": {"driveId": "drive-1", "path": "/drive/root:"},
                        }
                    ]
                },
            )
        )
        async with session_factory() as db:
            hits = await search_connected_files(db, workspace.id, "notes")

    assert len(hits) == 1
    assert hits[0].item_ref == "m365:drive-1:item-1"
    assert hits[0].name == "notes.txt"


async def test_drive_fallback_pins_hits_to_the_source_drive_and_drops_disallowed(
    configured_m365, session_factory, seed
):
    """Per-drive `/root/search` fallback (the personal-account path): a
    hit's own `parentReference.driveId` can name a shared-in item from a
    different drive than the one actually queried. A hit naming another
    *allowed* drive must be pinned to the drive this call actually searched
    — never the resource's own claim, which a consumer OneDrive does not
    make trustworthy just because it happens to name an allowed drive — and
    a hit naming a drive this workspace never allowed at all must be
    dropped outright."""
    workspace = make_workspace()
    conn = make_connection(
        workspace,
        selected_resources={
            "read": [
                {"drive_id": "drive-1", "label": "Drive One", "kind": "onedrive"},
                {"drive_id": "drive-2", "label": "Drive Two", "kind": "onedrive"},
            ]
        },
    )
    await seed(workspace, conn)

    drive1_body = {
        "value": [
            {
                "id": "item-own",
                "name": "own.txt",
                "parentReference": {"driveId": "drive-1", "path": "/drive/root:"},
            },
            {
                # Shared into drive-1 from drive-2 — also an allowed drive,
                # but not the one this /root/search call queried.
                "id": "item-shared-allowed",
                "name": "shared-allowed.txt",
                "parentReference": {"driveId": "drive-2", "path": "/drive/root:"},
            },
            {
                # Shared in from a drive this workspace never allowed.
                "id": "item-shared-outside",
                "name": "shared-outside.txt",
                "parentReference": {"driveId": "drive-outside", "path": "/drive/root:"},
            },
        ]
    }
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(return_value=httpx.Response(400, json={}))
        mock.get(url__regex=rf"{GRAPH}/drives/drive-1/root/search.*").mock(
            return_value=httpx.Response(200, json=drive1_body)
        )
        mock.get(url__regex=rf"{GRAPH}/drives/drive-2/root/search.*").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        async with session_factory() as db:
            hits = await search_connected_files(db, workspace.id, "anything")

    by_name = {h.name: h for h in hits}
    # The outside-drive hit is dropped entirely.
    assert set(by_name) == {"own.txt", "shared-allowed.txt"}
    # Both surviving hits were found via the drive-1 endpoint, so both are
    # pinned to drive-1 — the shared-allowed one included, despite naming
    # drive-2 in its own parentReference.
    assert by_name["own.txt"].item_ref == "m365:drive-1:item-own"
    assert by_name["shared-allowed.txt"].item_ref == "m365:drive-1:item-shared-allowed"


async def test_search_fallback_is_capped_to_five_drives(configured_m365, session_factory, seed):
    workspace = make_workspace()
    selected = [
        {"drive_id": f"drive-{i}", "label": f"Drive {i}", "kind": "site_drive"} for i in range(8)
    ]
    conn = make_connection(workspace, selected_resources={"read": selected})
    await seed(workspace, conn)

    with respx.mock(assert_all_called=False) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(return_value=httpx.Response(400, json={}))
        route = mock.get(url__regex=rf"{GRAPH}/drives/drive-\d/root/search.*").mock(
            return_value=httpx.Response(200, json={"value": []})
        )
        async with session_factory() as db:
            await search_connected_files(db, workspace.id, "anything")

    assert route.call_count == 5


async def test_search_narrows_to_one_source_slug(configured_m365, session_factory, seed):
    workspace = make_workspace()
    conn = make_connection(
        workspace,
        selected_resources={
            "read": [
                {"drive_id": "drive-a", "label": "A", "kind": "site_drive"},
                {"drive_id": "drive-b", "label": "B", "kind": "site_drive"},
            ]
        },
    )
    await seed(workspace, conn)

    async with session_factory() as db:
        sources = await list_connected_sources(db, workspace.id)
    target = next(s for s in sources if s.drive_id == "drive-b")

    body = _search_query_response(drive_id="drive-b", item_id="item-b", name="b.txt", summary=None)
    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.post(f"{GRAPH}/search/query").mock(return_value=httpx.Response(200, json=body))
        async with session_factory() as db:
            hits = await search_connected_files(db, workspace.id, "b", source_slug=target.slug)

    assert len(hits) == 1
    assert hits[0].source_slug == target.slug


async def test_search_with_no_allowed_sources_returns_no_hits_without_calling_graph(
    configured_m365, session_factory, seed
):
    workspace = make_workspace()
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-a", "label": "A", "kind": "site_drive"}]}
    )
    await seed(workspace, conn)

    async with session_factory() as db:
        hits = await search_connected_files(db, workspace.id, "q", source_slug="not-a-real-slug")
    assert hits == []


# ── materialize_connected_file ───────────────────────────────────────────────
def _item_metadata(
    *, item_id="item-1", name="Q3 Budget.xlsx", size=8, etag='"etag-1"', drive_id="drive-1"
) -> dict:
    return {
        "id": item_id,
        "name": name,
        "size": size,
        "eTag": etag,
        "webUrl": f"https://contoso.sharepoint.com/{name}",
        "lastModifiedDateTime": "2026-08-01T00:00:00Z",
        "file": {"mimeType": "text/csv"},
        "parentReference": {"driveId": drive_id, "path": f"/drives/{drive_id}/root:/Reports"},
    }


async def test_materialize_downloads_and_ingests_a_new_file(
    configured_m365, storage, resolves_public, session_factory, seed
):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, project, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(200, json=_item_metadata())
        )
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2\n")
        )
        async with session_factory() as db:
            doc = await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-1:item-1",
            )

    assert doc.project_id == project.id
    assert doc.filename == "Q3 Budget.xlsx"
    assert doc.source_kind == connections_module.CONNECTED_SOURCE_KIND
    assert doc.byte_size == len(b"a,b\n1,2\n")


async def test_materialize_persists_the_document_so_dedupe_and_callers_see_a_real_id(
    configured_m365, storage, resolves_public, session_factory, seed
):
    """`materialize_connected_file` must `db.add`/`flush` the `Document` it
    returns (`ingest_document` itself deliberately does neither — the caller
    owns the transaction). Without that: `doc.id` is `None`, nothing is
    queryable, and a second call for the same file re-downloads instead of
    deduping on the etag."""
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, project, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(200, json=_item_metadata())
        )
        content_route = mock.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2\n")
        )

        async with session_factory() as db:
            doc = await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-1:item-1",
            )
            assert doc.id is not None

            # Queryable in the same session/transaction — proof this is a
            # real, flushed row, not merely a Document object the caller
            # happens to be holding a reference to.
            found = (await db.execute(select(Document).where(Document.id == doc.id))).scalar_one()
            assert found.filename == "Q3 Budget.xlsx"

            await db.commit()

        # A second call, same etag, from a fresh session: dedupe must find
        # the row that was actually persisted above and must not download
        # again — the /content route's call count stays at 1.
        async with session_factory() as db:
            doc2 = await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-1:item-1",
            )

    assert doc2.id == doc.id
    assert content_route.call_count == 1


async def test_materialize_meta_fields_are_recorded(
    configured_m365, storage, resolves_public, session_factory, seed
):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"site_id": "site-1", "drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, project, conn)

    with respx.mock(assert_all_called=True) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(200, json=_item_metadata())
        )
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1/content").mock(
            return_value=httpx.Response(200, content=b"a,b\n1,2\n")
        )
        async with session_factory() as db:
            doc = await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-1:item-1",
            )

    assert doc.meta["provider"] == "m365"
    assert doc.meta["site_id"] == "site-1"
    assert doc.meta["drive_id"] == "drive-1"
    assert doc.meta["item_id"] == "item-1"
    assert doc.meta["etag"] == '"etag-1"'
    assert doc.meta["web_url"] == "https://contoso.sharepoint.com/Q3 Budget.xlsx"
    assert doc.meta["path"] == "Finance/Reports/Q3 Budget.xlsx"
    assert doc.meta["modified"] == "2026-08-01T00:00:00Z"
    assert doc.meta["source_slug"]


async def test_materialize_dedupes_on_matching_etag_without_downloading(
    configured_m365, storage, resolves_public, session_factory, seed
):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    existing = Document(
        id=uuid.uuid4(),
        project_id=project.id,
        filename="Q3 Budget.xlsx",
        content_type="text/csv",
        byte_size=8,
        storage_path="/tmp/already-there.csv",
        extraction_status="done",
        meta={"provider": "m365", "item_id": "item-1", "etag": '"etag-1"'},
        sha256="0" * 64,
        source_kind=connections_module.CONNECTED_SOURCE_KIND,
    )
    await seed(workspace, project, conn, existing)

    # Content is deliberately not mocked: assert_all_called=False plus never
    # registering the /content route means a download attempt would raise
    # respx's own "no matching route" error, failing the test outright.
    with respx.mock(assert_all_called=False) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(200, json=_item_metadata())
        )
        async with session_factory() as db:
            doc = await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-1:item-1",
            )

    assert doc.id == existing.id


async def test_materialize_refuses_a_file_outside_the_allowlist(
    configured_m365, storage, session_factory, seed
):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-allowed", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, project, conn)

    async with session_factory() as db:
        with pytest.raises(ConnectionUnavailable) as excinfo:
            await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref="m365:drive-not-allowed:item-1",
            )
    assert "outside" in excinfo.value.reason.lower()


async def test_materialize_refuses_a_file_over_the_size_cap(
    configured_m365, storage, session_factory, seed
):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(
        workspace, selected_resources={"read": [{"drive_id": "drive-1", "label": "Finance", "kind": "site_drive"}]}
    )
    await seed(workspace, project, conn)

    with respx.mock(assert_all_called=False) as mock:
        mock_m365_refresh(mock)
        mock.get(f"{GRAPH}/drives/drive-1/items/item-1").mock(
            return_value=httpx.Response(
                200, json=_item_metadata(size=IMPORT_MAX_BYTES + 1, etag='"etag-huge"')
            )
        )
        # No /content route registered — the size check must refuse before
        # any download is attempted.
        async with session_factory() as db:
            with pytest.raises(DownloadTooLargeError):
                await materialize_connected_file(
                    db,
                    workspace_id=workspace.id,
                    project_id=project.id,
                    item_ref="m365:drive-1:item-1",
                )


async def test_materialize_rejects_a_malformed_item_ref(configured_m365, storage, session_factory, seed):
    workspace = make_workspace()
    project = make_project(workspace)
    conn = make_connection(workspace)
    await seed(workspace, project, conn)

    async with session_factory() as db:
        with pytest.raises(ValueError):
            await materialize_connected_file(
                db, workspace_id=workspace.id, project_id=project.id, item_ref="not-a-ref"
            )
