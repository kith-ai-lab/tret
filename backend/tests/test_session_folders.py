"""Session folders: a chat conversation restricted to OneDrive/SharePoint folders.

Covers the whole backend slice — `services/connections.py` (resolve, the
ancestor-walk containment check and its per-run cache, folder-scoped search,
folder listing, the materialize guard, personal-account tolerance),
`api/chat.py` (create / PUT folders / send_message), `engine/tools.py` (the
`list_connected_folder` tool and the scoped search/read wiring), the harness
helpers, and child-run inheritance. Graph is faked by one respx route that
answers from an in-memory drive tree and records every path it was asked for,
so "verified, not assumed" and "bounded Graph calls" are assertions on that
call log. The end-to-end engine offer lives in
tests/evals/test_session_folders_engine.py (it needs that directory's `world`).
"""
from __future__ import annotations

import re
import uuid
from urllib.parse import unquote

import httpx
import pytest
import pytest_asyncio
import respx
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import chat as chat_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    ConnectionActivity,
    Conversation,
    Document,
    Harness,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceConnection,
    WorkspaceMember,
)
from tret.engine import extensions as extensions_module
from tret.engine import tools as tools_module
from tret.engine.delegation import SESSION_FOLDERS_KEY, SUBAGENT_TASK_PROFILE, SUBAGENT_TASK_TYPE
from tret.engine.harness import (
    MAX_SESSION_FOLDERS,
    _session_folders,
    _subagent_tool_names,
    _with_folder_context,
)
from tret.engine.tools import (
    CONNECTOR_TOOL_NAMES,
    RunContext,
    ToolError,
    _prepare_child,
    list_connected_folder,
    list_connected_sources,
    read_connected_file,
    search_connected_files,
    withheld_connector_tools,
)
from tret.services import connections as connections_module
from tret.services.connections import (
    GRAPH_API_BASE,
    ConnectionUnavailable,
    browse_m365,
    clear_access_token_cache,
    item_in_folders,
    list_connected_folder as service_list_folder,
    list_connected_sources as service_list_sources,
    materialize_connected_file,
    resolve_session_folders,
    search_connected_files as service_search,
)
from tret.services.credentials import get_fernet

GRAPH = GRAPH_API_BASE
M365_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"
DRIVE = "drive-1"


# ── a fake OneDrive ──────────────────────────────────────────────────────────
class FakeGraph:
    """An in-memory drive tree behind one respx route. `calls` is the log of
    decoded request paths (below `/v1.0/drives/`), so a test can assert what
    was — and was not — fetched. Folder search deliberately answers with every
    name match in the drive, the worst case the code must filter."""

    def __init__(self, drive: str = DRIVE):
        self.drive = drive
        self.nodes: dict[str, dict] = {}
        self.calls: list[str] = []
        self.children_next_link = False
        # item id -> parent id to *report* in search results only (a hit that
        # lies about where it lives), and spelling aliases for canonical ids.
        self.search_parent_override: dict[str, str] = {}
        self.aliases: dict[str, str] = {}
        self.fail_status: dict[str, int] = {}  # item id -> status to answer with

    def add(self, item_id, name, parent=None, *, folder=False, root=False, size=10):
        self.nodes[item_id] = {
            "id": item_id, "name": name, "parent": parent, "folder": folder, "root": root, "size": size,
        }

    def _path_of(self, node):
        names = []
        cur = node
        while cur["parent"] and not self.nodes[cur["parent"]]["root"]:
            cur = self.nodes[cur["parent"]]
            names.append(cur["name"])
        return f"/drives/{self.drive}/root:" + "".join(f"/{n}" for n in reversed(names))

    def _meta(self, node):
        out = {
            "id": node["id"],
            "name": node["name"],
            "size": node["size"],
            "eTag": f'"etag-{node["id"]}"',
            "webUrl": f"https://contoso.example/{node['name']}",
            "lastModifiedDateTime": "2026-08-01T00:00:00Z",
        }
        if node["folder"]:
            out["folder"] = {"childCount": 1}
        else:
            out["file"] = {"mimeType": "text/plain"}
        if node["root"]:
            out["root"] = {}
            out["parentReference"] = {"driveId": self.drive}
        else:
            out["parentReference"] = {
                "driveId": self.drive, "id": node["parent"], "path": self._path_of(node),
            }
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        rest = path.split(f"/drives/{self.drive}/items/", 1)[1]
        self.calls.append(rest)
        item_id, _, tail = rest.partition("/")
        item_id = self.aliases.get(item_id, item_id)
        if item_id in self.fail_status and tail == "":
            return httpx.Response(self.fail_status[item_id], json={})
        node = self.nodes.get(item_id)
        if node is None:
            return httpx.Response(404, json={})
        if tail == "":
            return httpx.Response(200, json=self._meta(node))
        if tail == "children":
            kids = [self._meta(n) for n in self.nodes.values() if n["parent"] == item_id]
            body: dict = {"value": kids}
            if self.children_next_link:
                body["@odata.nextLink"] = f"{GRAPH}/drives/{self.drive}/items/{item_id}/children?skip=1"
            return httpx.Response(200, json=body)
        if tail == "content":
            return httpx.Response(200, content=b"hello from onedrive")
        match = re.fullmatch(r"search\(q='(.*)'\)", tail)
        if match:
            needle = match.group(1).replace("''", "'").lower()
            hits = []
            for n in self.nodes.values():
                if needle not in n["name"].lower():
                    continue
                meta = self._meta(n)
                if n["id"] in self.search_parent_override:
                    meta["parentReference"]["id"] = self.search_parent_override[n["id"]]
                hits.append(meta)
            return httpx.Response(200, json={"value": hits})
        return httpx.Response(400, json={})

    def fetches(self, item_id: str) -> int:
        return sum(1 for c in self.calls if c.split("?")[0] == item_id)


def build_tree() -> FakeGraph:
    g = FakeGraph()
    g.add("R", "root", folder=True, root=True)
    g.add("C", "Coursework", "R", folder=True)
    g.add("W1", "Week 1", "C", folder=True)
    g.add("N1", "notes.txt", "W1")
    g.add("S1", "syllabus.txt", "C")
    g.add("P", "Private", "R", folder=True)
    g.add("X1", "secrets.txt", "P")
    g.add("X2", "more-secrets.txt", "P")
    g.add("L1", "loose notes.txt", "R")
    return g


def folder_entry(item_id="W1", name="Week 1", path="OneDrive/Coursework/Week 1", drive=DRIVE) -> dict:
    return {"drive_id": drive, "item_id": item_id, "name": name, "path": path, "web_url": None}


@pytest.fixture
def graph() -> FakeGraph:
    return build_tree()


@pytest.fixture
def mock_graph(graph):
    with respx.mock(assert_all_called=False) as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.route(url__regex=rf"{re.escape(GRAPH)}/drives/.*").mock(side_effect=graph.handler)
        yield mock


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
def _clean_state(monkeypatch):
    extensions_module._registry = None
    monkeypatch.setenv("TRET_M365_CLIENT_ID", "m365-cid")
    monkeypatch.setenv("TRET_M365_CLIENT_SECRET", "m365-secret")
    get_settings.cache_clear()
    clear_access_token_cache()
    connections_module.invalidate_connected_sources(uuid.uuid4())  # no-op, import use
    connections_module._sources_cache.clear()
    yield
    extensions_module._registry = None
    get_settings.cache_clear()
    clear_access_token_cache()
    connections_module._sources_cache.clear()


@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))


@pytest.fixture
def resolves_public(monkeypatch):
    from tret.net import guard as net_guard

    async def _fake_resolve(host):
        return ("8.8.8.8",)

    monkeypatch.setattr(net_guard, "_resolve", _fake_resolve)


def make_connection(workspace, *, drives=(DRIVE,), status="active") -> WorkspaceConnection:
    return WorkspaceConnection(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        provider="m365",
        account_label="student@example.com",
        encrypted_refresh_token=get_fernet().encrypt(b"stored-refresh-token"),
        granted_scopes=["offline_access", "User.Read", "Files.Read.All"],
        selected_resources={
            "read": [{"drive_id": d, "label": "OneDrive", "kind": "onedrive"} for d in drives]
        },
        status=status,
    )


@pytest_asyncio.fixture
async def world(seed):
    """A workspace + project + m365 connection restricted to DRIVE."""
    workspace = Workspace(id=uuid.uuid4(), name="Alpha", kind="team")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project, make_connection(workspace))
    return workspace, project


# ── resolve_session_folders ──────────────────────────────────────────────────
async def test_resolve_reads_name_path_and_url_from_graph(world, mock_graph, session_factory):
    workspace, _ = world
    async with session_factory() as db:
        out = await resolve_session_folders(
            db, workspace.id, [{"drive_id": DRIVE, "item_id": "W1", "name": "forged", "path": "forged"}]
        )
    assert out == [
        {
            "drive_id": DRIVE,
            "item_id": "W1",
            "name": "Week 1",
            "path": "OneDrive/Coursework/Week 1",
            "web_url": "https://contoso.example/Week 1",
        }
    ]


async def test_resolve_dedupes_and_labels_the_drive_root(world, mock_graph, session_factory, graph):
    workspace, _ = world
    async with session_factory() as db:
        out = await resolve_session_folders(
            db,
            workspace.id,
            [{"drive_id": DRIVE, "item_id": "R"}, {"drive_id": DRIVE, "item_id": "R"}],
        )
    assert [f["path"] for f in out] == ["OneDrive"]
    assert graph.fetches("R") == 1


async def test_resolve_rejects_a_file(world, mock_graph, session_factory):
    workspace, _ = world
    async with session_factory() as db:
        with pytest.raises(ValueError, match="not a folder"):
            await resolve_session_folders(db, workspace.id, [{"drive_id": DRIVE, "item_id": "N1"}])


async def test_resolve_rejects_a_drive_outside_the_allowed_sources(world, mock_graph, session_factory, graph):
    workspace, _ = world
    async with session_factory() as db:
        with pytest.raises(ValueError, match="outside the workspace's allowed sources"):
            await resolve_session_folders(db, workspace.id, [{"drive_id": "other", "item_id": "W1"}])
    assert graph.calls == []


async def test_resolve_rejects_more_than_ten_folders_before_calling_graph(
    world, mock_graph, session_factory, graph
):
    workspace, _ = world
    refs = [{"drive_id": DRIVE, "item_id": f"F{i}"} for i in range(11)]
    async with session_factory() as db:
        with pytest.raises(ValueError, match="at most 10"):
            await resolve_session_folders(db, workspace.id, refs)
    assert graph.calls == []


async def test_resolve_maps_a_missing_item_to_value_error(world, mock_graph, session_factory):
    workspace, _ = world
    async with session_factory() as db:
        with pytest.raises(ValueError, match="could not be found"):
            await resolve_session_folders(db, workspace.id, [{"drive_id": DRIVE, "item_id": "gone"}])


async def test_resolve_without_a_connection_raises_connection_unavailable(seed, session_factory):
    workspace = Workspace(id=uuid.uuid4(), name="Bare", kind="team")
    await seed(workspace)
    async with session_factory() as db:
        with pytest.raises(ConnectionUnavailable):
            await resolve_session_folders(db, workspace.id, [{"drive_id": DRIVE, "item_id": "W1"}])


# ── item_in_folders ──────────────────────────────────────────────────────────
async def test_item_in_folders_walks_the_parent_chain(mock_graph, graph):
    folders = [folder_entry("C", "Coursework", "OneDrive/Coursework")]
    cache: dict = {}
    assert await item_in_folders("tok", DRIVE, "C", folders, cache) is True  # the folder itself
    assert graph.calls == []
    assert await item_in_folders("tok", DRIVE, "N1", folders, cache) is True  # N1 -> W1 -> C
    assert await item_in_folders("tok", DRIVE, "X1", folders, cache) is False  # X1 -> P -> R


async def test_item_in_folders_different_drive_makes_no_call(mock_graph, graph):
    assert await item_in_folders("tok", "other-drive", "N1", [folder_entry()], {}) is False
    assert graph.calls == []


async def test_item_in_folders_cache_bounds_graph_calls(mock_graph, graph):
    folders = [folder_entry("W1")]
    cache: dict = {}
    assert await item_in_folders("tok", DRIVE, "N1", folders, cache) is True
    assert len(graph.calls) == 1  # N1's own parent link; W1 is a folder, no fetch
    assert await item_in_folders("tok", DRIVE, "N1", folders, cache) is True
    assert len(graph.calls) == 1  # memoised

    assert await item_in_folders("tok", DRIVE, "X1", folders, cache) is False
    assert len(graph.calls) == 4  # X1, P, R
    assert await item_in_folders("tok", DRIVE, "X1", folders, cache) is False
    assert await item_in_folders("tok", DRIVE, "P", folders, cache) is False
    assert len(graph.calls) == 4  # nothing new
    assert await item_in_folders("tok", DRIVE, "X2", folders, cache) is False
    assert len(graph.calls) == 5  # only X2's own link; P's verdict was cached


async def test_item_in_folders_stops_after_25_hops(mock_graph, graph):
    graph.add("D0", "d0", "R", folder=True)
    for i in range(1, 40):
        graph.add(f"D{i}", f"d{i}", f"D{i - 1}", folder=True)
    folders = [folder_entry("D0", "d0", "OneDrive/d0")]
    assert await item_in_folders("tok", DRIVE, "D39", folders, {}) is False
    assert len(graph.calls) <= 25
    # ...but the same folder is found when it is within reach.
    assert await item_in_folders("tok", DRIVE, "D20", folders, {}) is True


async def test_item_in_folders_is_false_for_an_item_graph_does_not_know(mock_graph, graph):
    assert await item_in_folders("tok", DRIVE, "ghost", [folder_entry()], {}) is False


# ── search within folders ────────────────────────────────────────────────────
async def test_search_in_folders_drops_hits_outside_and_skips_the_tenant_api(
    world, mock_graph, session_factory, graph
):
    workspace, _ = world
    # "notes" matches N1 (inside Week 1) and L1 (drive root, outside).
    async with session_factory() as db:
        hits = await service_search(
            db, workspace.id, "notes", folders=[folder_entry("W1")], scope_cache={}, actor_run_id=None
        )
        activity = (await db.execute(select(ConnectionActivity))).scalars().all()  # flushed, uncommitted
    assert [h.name for h in hits] == ["notes.txt"]
    assert hits[0].item_ref == f"m365:{DRIVE}:N1"
    assert hits[0].path == "OneDrive/Coursework/Week 1/notes.txt"
    # Folder-scoped endpoint used; the tenant-wide Search API was never called
    # (any POST would have hit an unmocked route and raised).
    assert any(c.startswith("W1/search(") for c in graph.calls)
    assert [(a.action, a.target, a.detail) for a in activity] == [("search", "notes", "1")]


async def test_search_in_folders_escapes_apostrophes_and_respects_max_results(
    world, mock_graph, session_factory, graph
):
    workspace, _ = world
    graph.add("A1", "o'brien a.txt", "W1")
    graph.add("A2", "o'brien b.txt", "W1")
    graph.add("A3", "o'brien c.txt", "W1")
    async with session_factory() as db:
        hits = await service_search(
            db, workspace.id, "o'brien", max_results=2, folders=[folder_entry("W1")], scope_cache={}
        )
    assert len(hits) == 2
    assert any("search(q='o''brien')" in c for c in graph.calls)


async def test_search_ignores_source_slug_and_drops_folders_of_a_removed_drive(
    world, mock_graph, session_factory, graph
):
    workspace, _ = world
    async with session_factory() as db:
        gone = await service_search(
            db, workspace.id, "notes", folders=[folder_entry("W1", drive="removed")], scope_cache={}
        )
        scoped = await service_search(
            db, workspace.id, "notes", source_slug="no-such-slug", folders=[folder_entry("W1")], scope_cache={}
        )
    assert gone == []
    assert [h.name for h in scoped] == ["notes.txt"]


async def test_search_in_folders_shares_the_scope_cache_across_calls(
    world, mock_graph, session_factory, graph
):
    workspace, _ = world
    cache: dict = {}
    async with session_factory() as db:
        await service_search(db, workspace.id, "notes", folders=[folder_entry("W1")], scope_cache=cache)
        first = len(graph.calls)
        await service_search(db, workspace.id, "notes", folders=[folder_entry("W1")], scope_cache=cache)
    # Second search: one folder-search call, every containment verdict cached.
    assert len(graph.calls) == first + 1


# ── list_connected_folder (service) ──────────────────────────────────────────
async def test_list_folder_without_ref_lists_the_session_folders(world, mock_graph, session_factory, graph):
    workspace, _ = world
    async with session_factory() as db:
        entries = await service_list_folder(
            db, workspace.id, [folder_entry("W1"), folder_entry("C", drive="removed")],
            None, scope_cache={},
        )
    assert [(e["name"], e["kind"], e["item_ref"]) for e in entries] == [
        ("Week 1", "folder", f"m365:{DRIVE}:W1")
    ]
    assert graph.calls == []


async def test_list_folder_lists_children_of_an_inside_folder(world, mock_graph, session_factory, graph):
    workspace, _ = world
    cache: dict = {}
    async with session_factory() as db:
        entries = await service_list_folder(
            db, workspace.id, [folder_entry("C", "Coursework", "OneDrive/Coursework")],
            f"m365:{DRIVE}:W1", scope_cache=cache,
        )
    assert [(e["name"], e["kind"], e["size"]) for e in entries] == [("notes.txt", "file", 10)]
    assert entries[0]["item_ref"] == f"m365:{DRIVE}:N1"
    assert entries.truncated is False
    # Children are known to be inside: a follow-up containment check is free.
    before = len(graph.calls)
    assert await item_in_folders("tok", DRIVE, "N1", [folder_entry("C")], cache) is True
    assert len(graph.calls) == before


async def test_list_folder_refuses_outside_and_non_folders(world, mock_graph, session_factory, graph):
    workspace, _ = world
    folders = [folder_entry("W1")]
    async with session_factory() as db:
        with pytest.raises(ValueError, match="outside this session's folders"):
            await service_list_folder(db, workspace.id, folders, f"m365:{DRIVE}:P", scope_cache={})
        with pytest.raises(ValueError, match="not a folder"):
            await service_list_folder(db, workspace.id, folders, f"m365:{DRIVE}:N1", scope_cache={})
    assert not any(c.endswith("/children") for c in graph.calls)


async def test_list_folder_reports_truncation(world, mock_graph, session_factory, graph):
    workspace, _ = world
    graph.children_next_link = True
    async with session_factory() as db:
        entries = await service_list_folder(
            db, workspace.id, [folder_entry("W1")], f"m365:{DRIVE}:W1", scope_cache={}
        )
    assert entries.truncated is True


# ── materialize with folders ─────────────────────────────────────────────────
async def test_materialize_refuses_an_outside_item_before_any_download(
    world, mock_graph, session_factory, graph, storage, resolves_public
):
    workspace, project = world
    async with session_factory() as db:
        with pytest.raises(ValueError, match="outside this session's folders"):
            await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref=f"m365:{DRIVE}:X1",
                folders=[folder_entry("W1")],
                scope_cache={},
            )
        count = (await db.execute(select(func.count()).select_from(Document))).scalar_one()
    assert count == 0
    assert not any(c.endswith("/content") for c in graph.calls)


async def test_materialize_reads_an_inside_item_and_no_folders_is_unrestricted(
    world, mock_graph, session_factory, graph, storage, resolves_public
):
    workspace, project = world
    async with session_factory() as db:
        doc = await materialize_connected_file(
            db,
            workspace_id=workspace.id,
            project_id=project.id,
            item_ref=f"m365:{DRIVE}:N1",
            folders=[folder_entry("W1")],
            scope_cache={},
        )
        assert doc.filename == "notes.txt"
        unrestricted = await materialize_connected_file(
            db, workspace_id=workspace.id, project_id=project.id, item_ref=f"m365:{DRIVE}:X1"
        )
        assert unrestricted.filename == "secrets.txt"


# ── personal Microsoft accounts ──────────────────────────────────────────────
async def test_browse_sites_still_returns_onedrive_when_sites_is_4xx(world, session_factory):
    workspace, _ = world
    with respx.mock() as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.get(f"{GRAPH}/sites").mock(return_value=httpx.Response(400, json={"error": {}}))
        mock.get(f"{GRAPH}/me/drive").mock(return_value=httpx.Response(200, json={"id": "drive-me"}))
        async with session_factory() as db:
            items = await browse_m365(db, workspace.id, scope="sites")
    assert items == [{"id": "drive-me", "name": "OneDrive", "kind": "drive", "drive_id": "drive-me"}]


async def test_browse_sites_still_raises_on_a_server_error(world, session_factory):
    workspace, _ = world
    with respx.mock() as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.get(f"{GRAPH}/sites").mock(return_value=httpx.Response(503, json={}))
        async with session_factory() as db:
            with pytest.raises(RuntimeError):
                await browse_m365(db, workspace.id, scope="sites")


async def test_derived_sources_include_onedrive_when_sites_is_4xx(seed, session_factory):
    workspace = Workspace(id=uuid.uuid4(), name="Personal", kind="team")
    conn = make_connection(workspace)
    conn.selected_resources = {}  # unrestricted: sources are derived from Graph
    await seed(workspace, conn)
    with respx.mock() as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.get(f"{GRAPH}/sites").mock(return_value=httpx.Response(400, json={}))
        mock.get(f"{GRAPH}/me/drive").mock(return_value=httpx.Response(200, json={"id": "drive-me"}))
        mock.get(f"{GRAPH}/me").mock(
            return_value=httpx.Response(200, json={"userPrincipalName": "kid@outlook.com"})
        )
        async with session_factory() as db:
            sources = await service_list_sources(db, workspace.id)
    assert [(s.kind, s.drive_id, s.label) for s in sources] == [
        ("onedrive", "drive-me", "OneDrive (kid@outlook.com)")
    ]


# ── API: create, PUT, send ───────────────────────────────────────────────────
class _NoopEngine:
    async def execute(self, run_id):  # pragma: no cover
        return None


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    monkeypatch.setattr(chat_api, "get_harness_engine", lambda: _NoopEngine())
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(chat_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _login(client, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


@pytest_asyncio.fixture
async def chat_env(world, seed, client):
    workspace, project = world
    user = User(
        id=uuid.uuid4(),
        email="student@example.com",
        display_name="Student",
        password_hash=HASHER.hash(PASSWORD),
        role="analyst",
    )
    harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    await seed(user, WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="analyst"), harness)
    await _login(client, user.email)
    return workspace, project, user


async def _new_conversation(client, **body) -> str:
    created = await client.post("/api/chat", json=body)
    assert created.status_code == 200, created.text
    return created.json()["id"]


def _ref(item_id: str, drive: str = DRIVE) -> dict:
    return {"drive_id": drive, "item_id": item_id}


async def test_a_new_conversation_reports_no_folders(chat_env, client):
    created = await client.post("/api/chat", json={})
    assert created.status_code == 200
    assert created.json()["folders"] == []
    listed = await client.get("/api/chat")
    assert listed.json()[0]["folders"] == []


async def test_create_with_folders_stores_the_resolved_entries(chat_env, client, mock_graph, session_factory):
    created = await client.post("/api/chat", json={"folders": [_ref("W1"), _ref("W1"), _ref("P")]})
    assert created.status_code == 200, created.text
    folders = created.json()["folders"]
    assert [(f["item_id"], f["name"], f["path"]) for f in folders] == [
        ("W1", "Week 1", "OneDrive/Coursework/Week 1"),
        ("P", "Private", "OneDrive/Private"),
    ]
    async with session_factory() as db:
        conv = await db.get(Conversation, uuid.UUID(created.json()["id"]))
    assert conv.connected_folders == folders


async def test_create_with_a_bad_folder_creates_nothing(chat_env, client, mock_graph, session_factory):
    response = await client.post("/api/chat", json={"folders": [_ref("N1")]})
    assert response.status_code == 422
    async with session_factory() as db:
        count = (await db.execute(select(func.count()).select_from(Conversation))).scalar_one()
    assert count == 0


async def test_put_folders_stores_resolved_entries_and_returns_the_conversation(
    chat_env, client, mock_graph
):
    cid = await _new_conversation(client)
    response = await client.put(
        f"/api/chat/{cid}/folders", json={"folders": [{**_ref("W1")}]}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["folders"][0]["path"] == "OneDrive/Coursework/Week 1"
    assert body["messages"] == []
    fetched = await client.get(f"/api/chat/{cid}")
    assert fetched.json()["folders"] == body["folders"]


async def test_put_folders_422_when_not_a_folder(chat_env, client, mock_graph):
    cid = await _new_conversation(client)
    response = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("N1")]})
    assert response.status_code == 422
    assert "not a folder" in response.json()["detail"]


async def test_put_folders_422_when_the_drive_is_outside_the_allowed_sources(chat_env, client, mock_graph):
    cid = await _new_conversation(client)
    response = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("W1", "elsewhere")]})
    assert response.status_code == 422
    assert "outside the workspace's allowed sources" in response.json()["detail"]


async def test_put_folders_422_for_more_than_ten_and_for_malformed_bodies(chat_env, client, mock_graph):
    cid = await _new_conversation(client)
    too_many = await client.put(
        f"/api/chat/{cid}/folders", json={"folders": [_ref(f"F{i}") for i in range(11)]}
    )
    assert too_many.status_code == 422
    extra_key = await client.put(
        f"/api/chat/{cid}/folders", json={"folders": [{**_ref("W1"), "name": "forged"}]}
    )
    assert extra_key.status_code == 422
    empty_id = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("")]})
    assert empty_id.status_code == 422


async def test_put_empty_list_clears_folders_without_a_connection(chat_env, client, mock_graph, session_factory):
    cid = await _new_conversation(client)
    assert (await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("W1")]})).status_code == 200
    async with session_factory() as db:
        for conn in (await db.execute(select(WorkspaceConnection))).scalars().all():
            await db.delete(conn)
        await db.commit()
    response = await client.put(f"/api/chat/{cid}/folders", json={"folders": []})
    assert response.status_code == 200
    assert response.json()["folders"] == []


async def test_put_folders_409_without_a_usable_connection(chat_env, client, mock_graph, session_factory):
    cid = await _new_conversation(client)
    async with session_factory() as db:
        for conn in (await db.execute(select(WorkspaceConnection))).scalars().all():
            conn.status = "error"
            conn.error_detail = "token revoked"
        await db.commit()
    response = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("W1")]})
    assert response.status_code == 409
    assert "token revoked" in response.json()["detail"]


async def test_put_folders_502_when_graph_fails(chat_env, client):
    cid = await _new_conversation(client)
    with respx.mock() as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.route(url__regex=rf"{re.escape(GRAPH)}/drives/.*").mock(return_value=httpx.Response(503))
        response = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("W1")]})
    assert response.status_code == 502


async def test_put_folders_honours_the_connections_gate(chat_env, client, mock_graph):
    from tret.engine.extensions import ExtensionAPI, GateResult

    ext = ExtensionAPI(None)

    async def veto(db, workspace_id, action):
        assert action == "connections.use"
        return GateResult(allowed=False, reason="connections_plan_required", detail="Not in your plan.")

    cid = await _new_conversation(client)
    ext.add_workspace_gate(veto)
    extensions_module._registry = ext
    response = await client.put(f"/api/chat/{cid}/folders", json={"folders": [_ref("W1")]})
    assert response.status_code == 403
    # Clearing needs no gate.
    assert (await client.put(f"/api/chat/{cid}/folders", json={"folders": []})).status_code == 200


async def test_put_folders_404_for_another_workspace(chat_env, client, mock_graph, seed, session_factory):
    other = Workspace(id=uuid.uuid4(), name="Other", kind="team")
    other_project = Project(id=uuid.uuid4(), workspace_id=other.id, name="P")
    harness = Harness(
        workspace_id=other.id, name="Chat Assistant", task_profile="chat",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(other, other_project, harness)
    async with session_factory() as db:
        foreign = Conversation(project_id=other_project.id, harness_id=harness.id, messages=[])
        db.add(foreign)
        await db.commit()
        foreign_id = foreign.id
    response = await client.put(f"/api/chat/{foreign_id}/folders", json={"folders": []})
    assert response.status_code == 404
    response = await client.put(f"/api/chat/{uuid.uuid4()}/folders", json={"folders": []})
    assert response.status_code == 404


async def test_send_message_stamps_session_folders_only_when_set(chat_env, client, mock_graph, session_factory):
    plain = await _new_conversation(client)
    sent = await client.post(f"/api/chat/{plain}/messages", json={"text": "Hello"})
    assert sent.status_code == 200, sent.text
    async with session_factory() as db:
        run = await db.get(Run, uuid.UUID(sent.json()["run_id"]))
    assert set(run.task_input) == {"message", "_history", "_capabilities"}

    scoped = await _new_conversation(client, folders=[_ref("W1")])
    sent = await client.post(f"/api/chat/{scoped}/messages", json={"text": "Hello"})
    assert sent.status_code == 200, sent.text
    async with session_factory() as db:
        run = await db.get(Run, uuid.UUID(sent.json()["run_id"]))
        conv = await db.get(Conversation, uuid.UUID(scoped))
    assert run.task_input["_session_folders"] == conv.connected_folders
    assert run.task_input["_session_folders"][0]["item_id"] == "W1"


# ── tools ────────────────────────────────────────────────────────────────────
def _tool_ctx(folders=None) -> RunContext:
    return RunContext(
        db=None,
        run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
        workspace_id=uuid.uuid4(),
        session_folders=folders,
    )


@pytest.fixture
def usable(monkeypatch):
    async def _ok(db, workspace_id, provider="m365"):
        return object()

    monkeypatch.setattr(tools_module.connections_service, "ensure_connection_usable", _ok)


async def test_list_connected_folder_refuses_without_session_folders(usable):
    with pytest.raises(ToolError, match="no attached folders"):
        await list_connected_folder(_tool_ctx())


async def test_list_connected_folder_lists_and_spends_the_search_budget(usable, monkeypatch):
    seen = {}

    async def _list(db, workspace_id, folders, item_ref, *, scope_cache, actor_run_id=None):
        seen.update(folders=folders, item_ref=item_ref, scope_cache=scope_cache, run=actor_run_id)
        entries = connections_module.FolderListing(
            [{"name": "a [b].txt", "kind": "file", "size": 5, "modified": "2026-01-01", "item_ref": "m365:d:i"}]
        )
        entries.truncated = True
        return entries

    monkeypatch.setattr(tools_module.connections_service, "list_connected_folder", _list)
    ctx = _tool_ctx([folder_entry()])
    out = await list_connected_folder(ctx, "m365:d:f")
    assert seen["folders"] == ctx.session_folders
    assert seen["item_ref"] == "m365:d:f"
    assert seen["scope_cache"] is ctx.folder_scope_cache
    assert ctx.connected_searches == 1
    assert "a (b).txt" in out and "item_ref: m365:d:i" in out  # brackets neutralised
    assert "Only the first 1 entries" in out


async def test_list_connected_folder_respects_the_search_budget(usable, monkeypatch):
    monkeypatch.setattr(get_settings(), "connections_max_searches_per_run", 1)
    ctx = _tool_ctx([folder_entry()])
    ctx.connected_searches = 1
    with pytest.raises(ToolError, match="per-run limit"):
        await list_connected_folder(ctx)


async def test_list_connected_folder_turns_service_errors_into_tool_errors(usable, monkeypatch):
    async def _boom(*a, **k):
        raise ValueError("That item is outside this session's folders.")

    monkeypatch.setattr(tools_module.connections_service, "list_connected_folder", _boom)
    with pytest.raises(ToolError, match="outside this session's folders"):
        await list_connected_folder(_tool_ctx([folder_entry()]), "m365:d:x")


async def test_list_connected_sources_reports_the_session_folders(usable):
    out = await list_connected_sources(_tool_ctx([folder_entry()]))
    assert "restricted to these folders" in out
    assert "Week 1 (OneDrive/Coursework/Week 1)" in out
    assert f"m365:{DRIVE}:W1" in out


async def test_search_and_read_pass_the_folders_only_when_the_session_has_them(usable, monkeypatch):
    searches, reads = [], []

    async def _search(db, workspace_id, query, **kwargs):
        searches.append(kwargs)
        return []

    async def _materialize(db, **kwargs):
        reads.append(kwargs)
        raise ValueError("stop here")

    monkeypatch.setattr(tools_module.connections_service, "search_connected_files", _search)
    monkeypatch.setattr(tools_module.connections_service, "materialize_connected_file", _materialize)

    plain = _tool_ctx()
    await search_connected_files(plain, "q")
    with pytest.raises(ToolError):
        await read_connected_file(plain, "m365:d:i")
    assert "folders" not in searches[0] and "scope_cache" not in searches[0]
    assert "folders" not in reads[0] and "scope_cache" not in reads[0]

    scoped = _tool_ctx([folder_entry()])
    out = await search_connected_files(scoped, "q", source="some-slug")
    with pytest.raises(ToolError):
        await read_connected_file(scoped, "m365:d:i")
    assert searches[1]["folders"] == scoped.session_folders
    assert searches[1]["scope_cache"] is scoped.folder_scope_cache
    assert reads[1]["folders"] == scoped.session_folders
    assert reads[1]["scope_cache"] is scoped.folder_scope_cache
    assert "source argument was ignored" in out


# ── tool sets and harness helpers ────────────────────────────────────────────
def test_the_folder_tool_is_a_connector_tool_and_a_subagent_may_hold_it():
    assert "list_connected_folder" in CONNECTOR_TOOL_NAMES
    assert "list_connected_folder" in tools_module.SUBAGENT_ALLOWED_TOOLS
    assert _subagent_tool_names(
        ["search_connected_files", "list_connected_folder"],
        {"_allowed_tools": ["search_connected_files"]},
        delegated=True,
    ) == ["search_connected_files"]


async def test_the_folder_tool_is_withheld_with_the_other_connector_tools(monkeypatch):
    async def _down(db, workspace_id, provider="m365"):
        raise ConnectionUnavailable("No Microsoft 365 connection is active for this workspace.")

    monkeypatch.setattr(tools_module.connections_service, "ensure_connection_usable", _down)
    names = ["read_document", "search_connected_files", "list_connected_folder"]
    withheld, reason = await withheld_connector_tools(None, uuid.uuid4(), names)
    assert withheld == {"search_connected_files", "list_connected_folder"}
    assert "No Microsoft 365 connection" in reason


def test_session_folders_keeps_only_well_formed_entries_and_caps_at_ten():
    good = {"drive_id": "d", "item_id": "i", "name": "n", "path": "p", "web_url": None}
    raw = [
        good,
        "nope",
        {"drive_id": "", "item_id": "i"},
        {"drive_id": "d"},
        {"drive_id": 1, "item_id": "i"},
        {"drive_id": "d", "item_id": ""},
    ]
    assert _session_folders({"_session_folders": raw}) == [good]
    assert _session_folders({}) is None
    assert _session_folders({"_session_folders": "x"}) is None
    assert _session_folders({"_session_folders": [{"drive_id": "d", "item_id": "i"}] * 3}) is not None
    many = [{"drive_id": "d", "item_id": str(i)} for i in range(15)]
    assert len(_session_folders({"_session_folders": many})) == MAX_SESSION_FOLDERS


def test_the_folder_context_paragraph_is_stable_and_sanitised():
    folders = [{"drive_id": "d", "item_id": "i", "name": "Week\n1 [x]", "path": "OneDrive/Week 1"}]
    paragraph = _with_folder_context(None, folders)
    assert paragraph == _with_folder_context(None, folders)
    assert "Week 1 (x) (OneDrive/Week 1)" in paragraph
    assert "list_connected_folder" in paragraph
    assert "\n1" not in paragraph.split("attached:")[1].split("\n")[1]
    assert _with_folder_context("catalog", folders).startswith("catalog\n\n")
    assert _with_folder_context("catalog", None) == "catalog"
    assert _with_folder_context(None, None) is None


# ── delegated runs inherit the folders ───────────────────────────────────────
@pytest_asyncio.fixture
async def delegation_env(session_factory, seed):
    workspace = Workspace(id=uuid.uuid4(), name="W", kind="team")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    chat = Harness(
        workspace_id=workspace.id, name="Chat Assistant", task_profile="chat",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    subagent = Harness(
        workspace_id=workspace.id, name="Subagent", task_profile=SUBAGENT_TASK_PROFILE,
        model_policy={"mode": "auto"}, tool_names=[],
    )
    await seed(workspace, project, chat, subagent)
    parent = Run(project_id=project.id, harness_id=chat.id, task_type="chat", task_input={})
    await seed(parent)
    async with session_factory() as db:
        yield db, workspace, project, parent


def _delegation_ctx(db, workspace, project, parent, folders) -> RunContext:
    return RunContext(
        db=db,
        run_id=parent.id,
        project_id=project.id,
        pack_id=None,
        doctrine_sha=None,
        model_used=None,
        document_ids=[],
        output_schemas={},
        workspace_id=workspace.id,
        session_folders=folders,
    )


async def test_a_subagent_child_inherits_the_session_folders(delegation_env):
    db, workspace, project, parent = delegation_env
    folders = [folder_entry()]
    ctx = _delegation_ctx(db, workspace, project, parent, folders)
    prepared = await _prepare_child(
        ctx, SUBAGENT_TASK_TYPE, {"instructions": "read it"}, kind="subagent"
    )
    child = await db.get(Run, prepared.child_id)
    assert child.task_input[SESSION_FOLDERS_KEY] == folders
    assert child.task_input[SESSION_FOLDERS_KEY] is not folders  # a copy, never aliased


async def test_a_child_of_a_session_without_folders_gets_none_even_if_the_model_asks(delegation_env):
    db, workspace, project, parent = delegation_env
    ctx = _delegation_ctx(db, workspace, project, parent, None)
    prepared = await _prepare_child(
        ctx, SUBAGENT_TASK_TYPE, {"instructions": "read it"}, kind="subagent"
    )
    child = await db.get(Run, prepared.child_id)
    assert SESSION_FOLDERS_KEY not in child.task_input


async def test_a_task_child_never_takes_folders_from_the_model(delegation_env, session_factory, seed):
    """A pack task child: whatever `_session_folders` the model writes into its
    task_input is dropped, and the parent's own folders are stamped instead."""
    from tret.db.models import Pack
    from tret.packs.links import set_harness_packs

    db, workspace, project, parent = delegation_env
    pack = Pack(
        id=uuid.uuid4(), workspace_id=workspace.id, slug="p", version="1.0.0", doctrine_sha="x",
        manifest={"pack": "p", "version": "1.0.0", "display_name": "P",
                  "task_types": [{"slug": "assess", "display_name": "Assess"}]},
        source_path="/tmp/p",
    )
    db.add(pack)
    specialist = Harness(
        workspace_id=workspace.id, name="Analyst", task_profile="assess",
        model_policy={"mode": "auto"}, tool_names=[],
    )
    db.add(specialist)
    await db.flush()
    await set_harness_packs(db, specialist, [pack.id])
    await db.commit()

    forged = [{"drive_id": DRIVE, "item_id": "everything"}]
    folders = [folder_entry()]

    ctx = _delegation_ctx(db, workspace, project, parent, folders)
    prepared = await _prepare_child(ctx, "assess", {"_session_folders": forged})
    child = await db.get(Run, prepared.child_id)
    assert child.task_input[SESSION_FOLDERS_KEY] == folders

    bare = _delegation_ctx(db, workspace, project, parent, None)
    prepared = await _prepare_child(bare, "assess", {"_session_folders": forged})
    child = await db.get(Run, prepared.child_id)
    assert SESSION_FOLDERS_KEY not in child.task_input


# ── review fixes ─────────────────────────────────────────────────────────────
async def test_a_429_during_the_walk_raises_and_caches_no_verdict(mock_graph, graph):
    folders = [folder_entry("W1")]
    cache: dict = {}
    graph.fail_status["X1"] = 429
    with pytest.raises(RuntimeError):
        await item_in_folders("tok", DRIVE, "X1", folders, cache)
    assert cache.get("inside", {}) == {}
    graph.fail_status.clear()
    # The throttle is over: the same item now gets a real answer, not a cached False.
    graph.add("W1b", "inside.txt", "W1")
    assert await item_in_folders("tok", DRIVE, "W1b", folders, cache) is True
    graph.fail_status["N1"] = 401
    with pytest.raises(RuntimeError):
        await item_in_folders("tok", DRIVE, "N1", folders, {})


async def test_400_403_404_in_the_walk_still_mean_not_inside(mock_graph, graph):
    for status in (400, 403, 404):
        graph.fail_status["X1"] = status
        assert await item_in_folders("tok", DRIVE, "X1", [folder_entry("W1")], {}) is False


@pytest.mark.parametrize("status", [401, 429, 503])
async def test_sites_failures_other_than_not_found_are_not_swallowed(seed, session_factory, status):
    workspace = Workspace(id=uuid.uuid4(), name="Flaky", kind="team")
    conn = make_connection(workspace)
    conn.selected_resources = {}
    await seed(workspace, conn)
    with respx.mock() as mock:
        mock.post(M365_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        )
        mock.get(f"{GRAPH}/sites").mock(return_value=httpx.Response(status, json={}))
        mock.get(f"{GRAPH}/me/drive").mock(return_value=httpx.Response(200, json={"id": "drive-me"}))
        mock.get(f"{GRAPH}/me").mock(return_value=httpx.Response(200, json={}))
        async with session_factory() as db:
            with pytest.raises(RuntimeError):
                await browse_m365(db, workspace.id, scope="sites")
            with pytest.raises(RuntimeError):
                await service_list_sources(db, workspace.id)
        # Nothing was cached from the failure: once /sites answers, SharePoint is back.
        mock.get(f"{GRAPH}/sites").mock(
            return_value=httpx.Response(200, json={"value": [{"id": "site-1", "displayName": "S"}]})
        )
        mock.get(f"{GRAPH}/sites/site-1/drive").mock(
            return_value=httpx.Response(200, json={"id": "drive-s", "name": "Documents"})
        )
        async with session_factory() as db:
            sources = await service_list_sources(db, workspace.id)
    assert {s.drive_id for s in sources} == {"drive-s", "drive-me"}


async def test_resolve_maps_only_not_found_statuses_to_value_error(world, mock_graph, session_factory, graph):
    workspace, _ = world
    graph.fail_status["W1"] = 429
    async with session_factory() as db:
        with pytest.raises(RuntimeError):
            await resolve_session_folders(db, workspace.id, [_ref("W1")])


async def test_resolve_stores_graphs_canonical_item_id(world, mock_graph, session_factory, graph):
    workspace, _ = world
    graph.aliases["w1"] = "W1"
    async with session_factory() as db:
        out = await resolve_session_folders(db, workspace.id, [_ref("w1"), _ref("W1")])
    assert [f["item_id"] for f in out] == ["W1"]  # alias collapsed onto the canonical id


async def test_a_search_hit_lying_about_its_parent_is_listed_but_cannot_be_read(
    world, mock_graph, session_factory, graph, storage, resolves_public
):
    workspace, project = world
    graph.search_parent_override["X1"] = "W1"  # X1 really lives in Private
    cache: dict = {}
    async with session_factory() as db:
        hits = await service_search(
            db, workspace.id, "secrets", folders=[folder_entry("W1")], scope_cache=cache
        )
        assert "secrets.txt" in [h.name for h in hits]  # display filtering may trust the hit
        with pytest.raises(ValueError, match="outside this session's folders"):
            await materialize_connected_file(
                db,
                workspace_id=workspace.id,
                project_id=project.id,
                item_ref=f"m365:{DRIVE}:X1",
                folders=[folder_entry("W1")],
                scope_cache=cache,
            )
        with pytest.raises(ValueError, match="outside this session's folders"):
            await service_list_folder(
                db, workspace.id, [folder_entry("W1")], f"m365:{DRIVE}:X1", scope_cache=cache
            )
        count = (await db.execute(select(func.count()).select_from(Document))).scalar_one()
    assert count == 0
    assert "X1" in graph.calls  # the walk started from a fresh GET of the item itself


def test_folder_ref_ids_are_length_capped():
    from pydantic import ValidationError

    chat_api.FolderRef(drive_id="d" * 512, item_id="i")
    with pytest.raises(ValidationError):
        chat_api.FolderRef(drive_id="d" * 513, item_id="i")
    with pytest.raises(ValidationError):
        chat_api.FolderRef(drive_id="d", item_id="i" * 513)


def test_session_folders_requested_distinguishes_absent_from_unusable():
    from tret.engine.harness import _session_folders_requested

    assert _session_folders_requested({}) is False
    assert _session_folders_requested({"_session_folders": []}) is False
    assert _session_folders_requested({"_session_folders": [{"drive_id": ""}]}) is True
    assert _session_folders({"_session_folders": [{"drive_id": ""}]}) is None


async def test_a_child_of_an_unusable_folder_run_fails_closed_too(delegation_env):
    db, workspace, project, parent = delegation_env
    ctx = _delegation_ctx(db, workspace, project, parent, None)
    ctx.folders_unusable = True
    prepared = await _prepare_child(
        ctx, SUBAGENT_TASK_TYPE, {"instructions": "read it"}, kind="subagent"
    )
    child = await db.get(Run, prepared.child_id)
    assert _session_folders(child.task_input) is None
    from tret.engine.harness import _session_folders_requested

    assert _session_folders_requested(child.task_input) is True


# ── connected documents from earlier runs in a folder session ───────────────
def _doc(project, name, *, source_kind=None) -> Document:
    return Document(
        project_id=project.id,
        filename=name,
        content_type="text/plain",
        byte_size=5,
        storage_path=f"x://{name}",
        extracted_text=f"text of {name}",
        extraction_status="completed",
        meta={},
        sha256=uuid.uuid4().hex,
        source_kind=source_kind,
    )


async def test_folder_sessions_hide_earlier_connected_documents_from_the_document_tools(delegation_env):
    db, workspace, project, parent = delegation_env
    uploaded = _doc(project, "upload.txt")
    web = _doc(project, "page.html", source_kind="web")
    old_connected = _doc(project, "old-connected.txt", source_kind=tools_module.CONNECTED_SOURCE_KIND)
    mine = _doc(project, "mine.txt", source_kind=tools_module.CONNECTED_SOURCE_KIND)
    db.add_all([uploaded, web, old_connected, mine])
    await db.commit()

    plain = _delegation_ctx(db, workspace, project, parent, None)
    ids, project_wide = await tools_module._document_scope(plain)
    assert project_wide and {uploaded.id, web.id, old_connected.id, mine.id} <= set(ids)

    scoped = _delegation_ctx(db, workspace, project, parent, [folder_entry()])
    scoped.document_ids = [mine.id]  # materialized by read_connected_file in this run
    ids, project_wide = await tools_module._document_scope(scoped)
    assert project_wide
    assert {uploaded.id, web.id, mine.id} <= set(ids)
    assert old_connected.id not in ids

    with pytest.raises(ToolError, match="not found in this project"):
        await tools_module.read_document(scoped, str(old_connected.id))
    assert "text of mine.txt" in await tools_module.read_document(scoped, str(mine.id))
    assert "text of upload.txt" in await tools_module.read_document(scoped, str(uploaded.id))
