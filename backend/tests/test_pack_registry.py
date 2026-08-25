"""Marketplace registry client (Phase C): `tret/api/packs.py`'s
`/api/packs/registry/*` endpoints — `search`, `{slug}`, `{slug}/{version}`
(read-only proxies to the registry through the egress chokepoint, with a 60s
cache) and `POST /registry/install` (workspace-admin, fetch detail -> stream
download -> `install_pack_from_archive`).

The registry is entirely faked with respx, mirroring test_oidc_login.py's own
docstring: nothing here reaches a real network (the egress chokepoint,
tests/test_egress_chokepoint.py, would fail the suite if `packs.py` ever
bypassed `tret.net` for this). Real database (sqlite, same harness as
test_pack_archive.py) — installing a pack writes real rows and real files.
"""
from __future__ import annotations

import io
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import respx
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, packs as packs_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Base, Pack, Project, User, Workspace, WorkspaceMember
from tret.packs.integrity import pack_content_hash

REGISTRY_HOST = "registry.example.test"
REGISTRY_BASE = f"https://{REGISTRY_HOST}/api/marketplace"

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


# ── a tiny, real, installable pack ───────────────────────────────────────────
def _build_minimal_pack(root: Path, *, description: str = "a tiny sample pack") -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(
        f"pack: sample\nversion: 1.0.0\ndisplay_name: Sample\ndescription: {description}\n"
    )
    return root


def _tar_directory(src: Path) -> bytes:
    """tar.gz `src`'s contents at the archive's own top level (no wrapper
    directory) — built with the stdlib `tarfile` module directly, per the
    spec, rather than reusing test_pack_archive.py's private helpers."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(src).as_posix(), recursive=False)
    return buf.getvalue()


def _detail_json(*, slug: str, version: str, content_hash: str, size_bytes: int) -> dict:
    return {
        "slug": slug,
        "version": version,
        "display_name": "Sample",
        "description": "a tiny sample pack",
        "content_hash": content_hash,
        "doctrine_sha": "irrelevant-for-this-test",
        "size_bytes": size_bytes,
        "has_methods": False,
        "manifest": {"pack": slug, "version": version},
    }


# ── database / app harness (same shape as test_pack_archive.py) ─────────────
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
            for row in rows:
                db.add(row)
                await db.flush()
            await db.commit()

    return _seed


@pytest.fixture
def storage(tmp_path, monkeypatch):
    directory = tmp_path / "storage"
    monkeypatch.setattr(get_settings(), "storage_dir", str(directory))
    return directory


@pytest.fixture(autouse=True)
def registry_settings(monkeypatch):
    """Every test gets a configured registry pointed at the fake host, egress
    on, and a clean cache — individual tests override one of these to exercise
    the disabled/off paths."""
    monkeypatch.setenv("TRET_PACK_REGISTRY_URL", REGISTRY_BASE)
    monkeypatch.setenv("TRET_EGRESS", "on")
    get_settings.cache_clear()
    packs_api._registry_cache.clear()
    yield
    get_settings.cache_clear()
    packs_api._registry_cache.clear()


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(packs_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str, *, global_role: str = "analyst") -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role=global_role,
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


# ── search / detail proxy passthrough + caching ──────────────────────────────
async def test_search_proxies_the_registry_and_passes_through_the_body(client, seed):
    team = make_workspace("Climate Co")
    user = make_user("analyst@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    payload = {"items": [{"slug": "sample", "display_name": "Sample"}], "next_cursor": None}
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(f"{REGISTRY_BASE}/packs", params={"q": "risk"}).mock(
            return_value=httpx.Response(200, json=payload)
        )
        response = await client.get("/api/packs/registry/search", params={"q": "risk"})
    assert response.status_code == 200, response.text
    assert response.json() == payload
    assert route.call_count == 1


async def test_detail_and_version_endpoints_proxy_the_registry(client, seed):
    team = make_workspace("Climate Co")
    user = make_user("analyst2@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    summary = {"slug": "sample", "display_name": "Sample", "versions": ["1.0.0"]}
    detail = _detail_json(slug="sample", version="1.0.0", content_hash="deadbeef" * 8, size_bytes=42)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample").mock(return_value=httpx.Response(200, json=summary))
        mock.get(f"{REGISTRY_BASE}/packs/sample/1.0.0").mock(return_value=httpx.Response(200, json=detail))

        summary_resp = await client.get("/api/packs/registry/sample")
        version_resp = await client.get("/api/packs/registry/sample/1.0.0")

    assert summary_resp.status_code == 200 and summary_resp.json() == summary
    assert version_resp.status_code == 200 and version_resp.json() == detail


async def test_a_cached_search_is_not_refetched_within_the_ttl(client, seed):
    team = make_workspace("Climate Co")
    user = make_user("analyst3@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(f"{REGISTRY_BASE}/packs").mock(
            return_value=httpx.Response(200, json={"items": [], "next_cursor": None})
        )
        first = await client.get("/api/packs/registry/search")
        second = await client.get("/api/packs/registry/search")
        assert first.status_code == 200 and second.status_code == 200
        assert route.call_count == 1  # second call served from cache

        # Force the one cached entry to look stale without touching the real
        # clock (time.monotonic() also backs the event loop's own scheduling,
        # so patching it globally is not safe here): age its timestamp past
        # the TTL directly.
        [key] = list(packs_api._registry_cache.keys())
        cached_at, cached_body = packs_api._registry_cache[key]
        packs_api._registry_cache[key] = (
            cached_at - packs_api._MARKETPLACE_CACHE_TTL_SECONDS - 1,
            cached_body,
        )

        third = await client.get("/api/packs/registry/search")
        assert third.status_code == 200
        assert route.call_count == 2  # cache expired, refetched


# ── upstream error mapping ───────────────────────────────────────────────────
async def test_an_upstream_404_is_mapped_cleanly(client, seed):
    team = make_workspace("Climate Co")
    user = make_user("analyst4@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/missing").mock(return_value=httpx.Response(404))
        response = await client.get("/api/packs/registry/missing")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


async def test_an_upstream_timeout_is_a_502(client, seed):
    team = make_workspace("Climate Co")
    user = make_user("analyst5@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample").mock(side_effect=httpx.ConnectTimeout("timed out"))
        response = await client.get("/api/packs/registry/sample")
    assert response.status_code == 502


async def test_a_non_json_upstream_body_is_a_502(client, seed):
    """`response.json()` raising (the registry answering 200 with, say, an
    HTML error page instead of JSON) must map to 502, not bubble up as an
    unhandled 500 — `_registry_proxy_error`'s ValueError branch."""
    team = make_workspace("Climate Co")
    user = make_user("analyst4b@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample").mock(
            return_value=httpx.Response(200, content=b"<html>not json</html>", headers={"content-type": "text/html"})
        )
        response = await client.get("/api/packs/registry/sample")
    assert response.status_code == 502
    assert "malformed" in response.json()["detail"].lower()


# ── cache bounds ──────────────────────────────────────────────────────────────
async def test_registry_cache_purges_expired_entries_and_caps_size(client, seed, monkeypatch):
    """`_registry_cache_set` purges anything past the TTL on every write and
    otherwise evicts the oldest entry once over `_MARKETPLACE_CACHE_MAX_ENTRIES`
    — so an unbounded stream of distinct queries (each its own cache key)
    cannot grow the process-lifetime cache without bound."""
    team = make_workspace("Climate Co")
    user = make_user("analyst4c@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    monkeypatch.setattr(packs_api, "_MARKETPLACE_CACHE_MAX_ENTRIES", 3)

    with respx.mock(assert_all_called=False) as mock:
        # respx matches on path only (query string ignored) unless a route
        # also filters on `params=`, so one route covers every `?q=...` variant.
        mock.get(f"{REGISTRY_BASE}/packs").mock(return_value=httpx.Response(200, json={"ok": True}))
        for i in range(5):
            response = await client.get("/api/packs/registry/search", params={"q": f"q{i}"})
            assert response.status_code == 200, response.text

    assert len(packs_api._registry_cache) <= 3

    # Expired-entry purge: age every cached entry past the TTL, then trigger
    # one more write — the write must not leave the stale entries behind.
    for key in list(packs_api._registry_cache):
        ts, body = packs_api._registry_cache[key]
        packs_api._registry_cache[key] = (ts - packs_api._MARKETPLACE_CACHE_TTL_SECONDS - 1, body)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/fresh").mock(return_value=httpx.Response(200, json={"ok": True}))
        response = await client.get("/api/packs/registry/fresh")
        assert response.status_code == 200

    assert list(packs_api._registry_cache.keys()) == [f"{REGISTRY_BASE}/packs/fresh"]


# ── slug/version validation (path traversal into the registry host) ──────────
async def test_registry_install_rejects_a_slug_with_a_slash(client, seed):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin2@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    response = await client.post(
        "/api/packs/registry/install", json={"slug": "../../etc/passwd", "version": "1.0.0"}
    )
    assert response.status_code == 422, response.text
    assert "slug" in response.json()["detail"].lower()


async def test_registry_install_rejects_a_version_with_a_slash(client, seed):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin3@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    response = await client.post(
        "/api/packs/registry/install", json={"slug": "sample", "version": "1.0.0/../../admin"}
    )
    assert response.status_code == 422, response.text
    assert "version" in response.json()["detail"].lower()


async def test_registry_pack_summary_rejects_a_dotdot_slug(client, seed):
    """A literal ".." in the request path is normalized away by httpx's own
    URL handling before it is even sent (RFC 3986 dot-segment removal) — not
    a real bypass, since any well-behaved client does the same. The percent-
    encoded form is not: it survives client-side normalization intact and
    only becomes ".." once Starlette decodes the path segment for routing,
    which is the actual shape this validator has to catch."""
    team = make_workspace("Climate Co")
    user = make_user("analyst4d@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    response = await client.get("/api/packs/registry/%2e%2e")
    assert response.status_code == 422, response.text


async def test_registry_pack_version_accepts_a_draft_suffixed_version(client, seed):
    """`+` must stay legal — `{version}+draft.{n}` (api/pack_builder.py) is a
    real version shape this validator must not reject."""
    team = make_workspace("Climate Co")
    user = make_user("analyst4e@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    detail = _detail_json(slug="sample", version="1.0.0+draft.1", content_hash="ab" * 32, size_bytes=1)
    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample/1.0.0+draft.1").mock(
            return_value=httpx.Response(200, json=detail)
        )
        response = await client.get("/api/packs/registry/sample/1.0.0+draft.1")
    assert response.status_code == 200, response.text


# ── disabled registry / egress off ───────────────────────────────────────────
async def test_an_empty_registry_url_is_a_503(client, seed, monkeypatch):
    team = make_workspace("Climate Co")
    user = make_user("analyst6@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    monkeypatch.setenv("TRET_PACK_REGISTRY_URL", "")
    get_settings.cache_clear()
    try:
        response = await client.get("/api/packs/registry/search")
        assert response.status_code == 503
        assert "not configured" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


async def test_egress_off_is_a_503(client, seed, monkeypatch):
    team = make_workspace("Climate Co")
    user = make_user("analyst7@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    monkeypatch.setenv("TRET_EGRESS", "off")
    get_settings.cache_clear()
    try:
        response = await client.get("/api/packs/registry/search")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


async def test_egress_replay_is_a_503(client, seed, monkeypatch):
    """`replay` has no meaning outside the `research` class — every other
    class, including marketplace, folds it to off (tret/net/policy.py). The
    marketplace policy derives its on/off decision through the same
    normalization the rest of the egress lattice uses (`master_is_off`), so a
    master switch that folds to off for `provider`/`catalog`/`local` must not
    leave the marketplace class on."""
    team = make_workspace("Climate Co")
    user = make_user("analyst7b@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    monkeypatch.setenv("TRET_EGRESS", "replay")
    get_settings.cache_clear()
    try:
        response = await client.get("/api/packs/registry/search")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


async def test_egress_garbage_is_a_503(client, seed, monkeypatch):
    """A spelling the lattice's own validator would reject at Settings
    construction (garbage) must still fold to off here if it ever reaches
    `_marketplace_policy` some other way — e.g. a runtime object with
    `.egress` set directly, bypassing the field validator. Constructed with
    `Settings.model_construct` to land a value the validator itself would
    refuse, exercising `_normalize`'s own "unknown reads as off" fallback."""
    team = make_workspace("Climate Co")
    user = make_user("analyst7c@example.com")
    await seed(team, Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample"), user,
               make_member(user, team, role="analyst"))
    await login(client, user.email)

    from tret.config import Settings
    from tret.net import policy as policy_module

    bad_settings = Settings.model_construct(**{**get_settings().model_dump(), "egress": "garbage"})
    monkeypatch.setattr(packs_api, "get_settings", lambda: bad_settings)
    monkeypatch.setattr(policy_module, "get_settings", lambda: bad_settings)
    try:
        response = await client.get("/api/packs/registry/search")
        assert response.status_code == 503
        assert "unavailable" in response.json()["detail"].lower()
    finally:
        get_settings.cache_clear()


# ── install ───────────────────────────────────────────────────────────────────
async def test_workspace_admin_can_install_from_the_registry(client, seed, storage, tmp_path):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    pack_dir = _build_minimal_pack(tmp_path / "sample-pack")
    content_hash = pack_content_hash(pack_dir)
    archive_bytes = _tar_directory(pack_dir)
    detail = _detail_json(
        slug="sample", version="1.0.0", content_hash=content_hash, size_bytes=len(archive_bytes)
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample/1.0.0").mock(return_value=httpx.Response(200, json=detail))
        mock.get(f"{REGISTRY_BASE}/packs/sample/1.0.0/download").mock(
            return_value=httpx.Response(200, content=archive_bytes)
        )
        response = await client.post(
            "/api/packs/registry/install", json={"slug": "sample", "version": "1.0.0"}
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slug"] == "sample"
    assert body["version"] == "1.0.0"
    assert body["content_hash"] == content_hash


async def test_installed_pack_row_and_directory_exist(client, seed, storage, tmp_path, session_factory):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin2@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    pack_dir = _build_minimal_pack(tmp_path / "sample-pack-2", description="verify storage")
    content_hash = pack_content_hash(pack_dir)
    archive_bytes = _tar_directory(pack_dir)
    detail = _detail_json(
        slug="sample2", version="2.0.0", content_hash=content_hash, size_bytes=len(archive_bytes)
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample2/2.0.0").mock(return_value=httpx.Response(200, json=detail))
        mock.get(f"{REGISTRY_BASE}/packs/sample2/2.0.0/download").mock(
            return_value=httpx.Response(200, content=archive_bytes)
        )
        response = await client.post(
            "/api/packs/registry/install", json={"slug": "sample2", "version": "2.0.0"}
        )
    assert response.status_code == 200, response.text
    pack_id = response.json()["id"]

    async with session_factory() as db:
        row = await db.get(Pack, uuid.UUID(pack_id))
        assert row is not None
        assert row.workspace_id == team.id
        assert row.content_hash == content_hash
        assert Path(row.source_path).is_dir()
        assert (Path(row.source_path) / "pack.yaml").exists()


async def test_a_non_admin_workspace_member_cannot_install(client, seed):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    member = make_user("plainmember@example.com")
    await seed(team, project, member, make_member(member, team, role="analyst"))
    await login(client, member.email)

    response = await client.post(
        "/api/packs/registry/install", json={"slug": "sample", "version": "1.0.0"}
    )
    assert response.status_code == 403


async def test_a_hash_mismatch_is_refused_with_502_and_installs_nothing(
    client, seed, storage, tmp_path, session_factory
):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin3@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    pack_dir = _build_minimal_pack(tmp_path / "sample-pack-3")
    archive_bytes = _tar_directory(pack_dir)
    # A hash the registry *claims* but that does not match the archive's real
    # content — the "registry served wrong bytes" case.
    wrong_hash = "0" * 64
    detail = _detail_json(
        slug="sample3", version="1.0.0", content_hash=wrong_hash, size_bytes=len(archive_bytes)
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample3/1.0.0").mock(return_value=httpx.Response(200, json=detail))
        mock.get(f"{REGISTRY_BASE}/packs/sample3/1.0.0/download").mock(
            return_value=httpx.Response(200, content=archive_bytes)
        )
        response = await client.post(
            "/api/packs/registry/install", json={"slug": "sample3", "version": "1.0.0"}
        )
    assert response.status_code == 502, response.text
    assert "wrong bytes" in response.json()["detail"].lower()

    async with session_factory() as db:
        rows = (await db.execute(select(Pack).where(Pack.workspace_id == team.id))).scalars().all()
        assert rows == []


async def test_an_oversized_download_is_cut_at_the_cap(client, seed, storage, tmp_path, monkeypatch):
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user("wsadmin4@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    # Shrink the cap so the test does not need to move real megabytes.
    monkeypatch.setattr(packs_api, "_MARKETPLACE_MAX_BYTES", 1024)
    oversized = b"x" * (2048)
    detail = _detail_json(
        slug="sample4", version="1.0.0", content_hash="irrelevant" * 6 + "12", size_bytes=len(oversized)
    )

    with respx.mock(assert_all_called=True) as mock:
        mock.get(f"{REGISTRY_BASE}/packs/sample4/1.0.0").mock(return_value=httpx.Response(200, json=detail))
        mock.get(f"{REGISTRY_BASE}/packs/sample4/1.0.0/download").mock(
            return_value=httpx.Response(200, content=oversized)
        )
        response = await client.post(
            "/api/packs/registry/install", json={"slug": "sample4", "version": "1.0.0"}
        )
    assert response.status_code == 502, response.text
    assert "exceeded" in response.json()["detail"].lower()
