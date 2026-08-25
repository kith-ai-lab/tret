"""`api/pack_builder.py`: the in-app pack builder's draft CRUD and actions
(Plan Phase D — `.claude/plans/optimized-jumping-quail.md`).

Same real-sqlite-database harness as test_packs_api.py/test_pack_archive.py —
a real FastAPI app wired to an in-memory sqlite database with foreign keys
turned on, not a fake session. `storage` monkeypatches `TRET_STORAGE_DIR` to a
tmp_path so test-install's swap into `PackStorage`'s permanent directory
lands somewhere disposable.
"""
from __future__ import annotations

import io
import tarfile
import uuid

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
import yaml
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, pack_builder, packs as packs_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Base, Project, User, Workspace, WorkspaceMember
from tret.packs.loader import validate_pack

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


# ── fixtures (mirrors test_pack_archive.py's harness) ────────────────────────
@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    @sa.event.listens_for(eng.sync_engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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


@pytest_asyncio.fixture
async def client(session_factory):
    app = FastAPI()
    app.include_router(auth.router)
    # pack_builder before packs_api: see the same-order comment in tret/main.py
    # — packs_api's `GET|DELETE /{pack_id}` would otherwise swallow
    # `/api/packs/drafts` first and fail pack_id's UUID conversion with a 422.
    app.include_router(pack_builder.router)
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


async def _admin_workspace(seed, *, name: str = "Climate Co", email: str = "admin@example.com"):
    """A team workspace with a project and a workspace-admin member, seeded
    and ready to log in as."""
    team = make_workspace(name)
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    admin = make_user(email)
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    return team, project, admin


# ── CRUD ──────────────────────────────────────────────────────────────────────
async def test_create_draft_scaffolds_a_manifest_and_lists_it(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)

    created = await client.post("/api/packs/drafts", json={"slug": "my-pack"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["slug"] == "my-pack"
    assert body["manifest_json"]["pack"] == "my-pack"
    assert body["manifest_json"]["version"] == "0.1.0"
    assert body["manifest_json"]["methods"] == []
    assert body["files"] == {}

    listed = await client.get("/api/packs/drafts")
    assert listed.status_code == 200
    assert [d["slug"] for d in listed.json()] == ["my-pack"]


async def test_get_draft_round_trips_manifest_and_files(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    created = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()
    draft_id = created["id"]

    patched = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"doctrine/01.md": "# Hello\nBody"}},
    )
    assert patched.status_code == 200, patched.text

    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.status_code == 200
    assert fetched.json()["files"] == {"doctrine/01.md": "# Hello\nBody"}


async def test_patch_files_merges_per_key_and_null_deletes(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"a.md": "A", "b.md": "B"}},
    )
    second = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"b.md": None, "c.md": "C"}},
    )
    assert second.status_code == 200, second.text
    assert second.json()["files"] == {"a.md": "A", "c.md": "C"}


async def test_patch_manifest_replaces_wholesale(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    new_manifest = {
        "pack": "my-pack",
        "version": "0.2.0",
        "display_name": "My Pack",
        "description": "updated",
        "doctrine": [],
        "task_types": [],
        "datasets": [],
        "methods": [],
    }
    patched = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"manifest_json": new_manifest}
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["manifest_json"] == new_manifest


async def test_delete_draft(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    deleted = await client.delete(f"/api/packs/drafts/{draft_id}")
    assert deleted.status_code == 200, deleted.text

    missing = await client.get(f"/api/packs/drafts/{draft_id}")
    assert missing.status_code == 404


async def test_an_analyst_is_refused_403(client, seed):
    """`require_workspace_admin` gates every route here — an analyst (below
    the admin rank) must not create drafts even in their own workspace."""
    team = make_workspace("Climate Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="Sample")
    analyst = make_user("analyst@example.com")
    await seed(team, project, analyst, make_member(analyst, team, role="analyst"))
    await login(client, analyst.email)

    response = await client.post("/api/packs/drafts", json={"slug": "my-pack"})
    assert response.status_code == 403


# ── cross-workspace isolation ────────────────────────────────────────────────
async def test_a_draft_in_another_workspace_404s_not_403s(client, seed):
    _, _, admin_a = await _admin_workspace(seed, name="Alpha Co", email="a@example.com")
    team_b = make_workspace("Bravo Co")
    project_b = Project(id=uuid.uuid4(), workspace_id=team_b.id, name="Sample B")
    admin_b = make_user("b@example.com")
    await seed(team_b, project_b, admin_b, make_member(admin_b, team_b, role="admin"))

    await login(client, admin_a.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "alpha-pack"})).json()["id"]

    await login(client, admin_b.email)

    get_resp = await client.get(f"/api/packs/drafts/{draft_id}")
    assert get_resp.status_code == 404
    patch_resp = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"manifest_json": {"pack": "x", "version": "1", "methods": []}},
    )
    assert patch_resp.status_code == 404
    delete_resp = await client.delete(f"/api/packs/drafts/{draft_id}")
    assert delete_resp.status_code == 404
    validate_resp = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    assert validate_resp.status_code == 404
    test_install_resp = await client.post(f"/api/packs/drafts/{draft_id}/test-install")
    assert test_install_resp.status_code == 404
    export_resp = await client.get(f"/api/packs/drafts/{draft_id}/export")
    assert export_resp.status_code == 404

    # And it is not present in workspace B's own listing either.
    listed = await client.get("/api/packs/drafts")
    assert listed.json() == []


# ── methods rejection ─────────────────────────────────────────────────────────
async def test_patch_rejects_non_empty_methods(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "0.1.0",
                "display_name": "My Pack",
                "methods": [
                    {
                        "slug": "m1",
                        "display_name": "M1",
                        "entrypoint": "methods/m1.py",
                    }
                ],
            }
        },
    )
    assert response.status_code == 422
    assert "POST /api/packs/install/archive" in response.json()["detail"]

    # Nothing was saved: the draft's manifest is unchanged from creation.
    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.json()["manifest_json"]["methods"] == []


# ── path sanitization ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_path",
    [
        "/etc/passwd",
        "../escape.md",
        "a/../../escape.md",
        "a\\b.md",
        "",
        "a//b.md",
        "a/./b.md",
        "pack.yaml",
        "PACK.YAML",
        " pack.yaml ",
        "a\x00b.md",
        "\x00",
    ],
)
async def test_patch_rejects_unsafe_file_paths(client, seed, bad_path):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {bad_path: "content"}}
    )
    assert response.status_code == 422, (bad_path, response.text)


async def test_patch_accepts_a_normal_nested_path(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"doctrine/01-intro.md": "# Intro"}},
    )
    assert response.status_code == 200, response.text


# ── file/directory shadowing ────────────────────────────────────────────────
async def test_patch_rejects_a_file_shadowing_a_directory_in_one_call(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"a.md": "file", "a.md/b.md": "nested under a file"}},
    )
    assert response.status_code == 422, response.text
    assert "a.md" in response.json()["detail"]

    # Nothing was saved — neither key.
    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.json()["files"] == {}


async def test_patch_rejects_a_file_shadowing_a_directory_across_two_calls(client, seed):
    """The conflict is checked against the *merged* dict — an existing file
    from a prior PATCH, then a second PATCH that tries to nest under it,
    must be caught too, not just a same-call pair."""
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    first = await client.patch(f"/api/packs/drafts/{draft_id}", json={"files": {"a.md": "file"}})
    assert first.status_code == 200, first.text

    second = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {"a.md/b.md": "nested under a file"}}
    )
    assert second.status_code == 422, second.text

    # The first write is unaffected by the second, rejected one.
    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.json()["files"] == {"a.md": "file"}


async def test_patch_accepts_unrelated_files_that_merely_share_a_prefix(client, seed):
    """`ab.md` is not nested under `a.md` — sharing a string prefix is not
    the same thing as one being a directory of the other."""
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {"a.md": "one", "ab.md": "two"}}
    )
    assert response.status_code == 200, response.text


# ── malformed file content ───────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad_b64",
    [
        "not valid base64!!",
        123,
        None,
        {},
    ],
)
async def test_patch_rejects_malformed_b64_content(client, seed, bad_b64):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {"image.png": {"b64": bad_b64}}}
    )
    assert response.status_code == 422, (bad_b64, response.text)

    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.json()["files"] == {}


async def test_patch_accepts_valid_b64_content(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {"image.png": {"b64": "aGVsbG8="}}}
    )
    assert response.status_code == 200, response.text


# ── size caps ─────────────────────────────────────────────────────────────────
async def test_patch_rejects_a_file_over_the_per_file_cap(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    huge = "x" * (5 * 1024 * 1024 + 1)
    response = await client.patch(
        f"/api/packs/drafts/{draft_id}", json={"files": {"big.md": huge}}
    )
    assert response.status_code == 422
    assert "per-file cap" in response.json()["detail"]


async def test_patch_rejects_total_draft_size_over_the_cap(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    # Two files, each under the per-file cap, that together exceed the total.
    chunk = "x" * (4 * 1024 * 1024)
    await client.patch(f"/api/packs/drafts/{draft_id}", json={"files": {"a.md": chunk}})
    await client.patch(f"/api/packs/drafts/{draft_id}", json={"files": {"b.md": chunk}})
    response = await client.patch(f"/api/packs/drafts/{draft_id}", json={"files": {"c.md": chunk}})
    assert response.status_code == 422
    assert "cap" in response.json()["detail"]


# ── manifest smuggling: files{} can never win over the generated pack.yaml ────
# PATCH already refuses a `files["pack.yaml"]` key outright (see the
# parametrized `test_patch_rejects_unsafe_file_paths` above) — these exercise
# the belt-and-braces guard in `tret.packs.draft` itself for a `DraftPack`
# row whose `files` already contains that key some other way than PATCH
# (a pre-fix row, a direct DB write, a future code path that forgets to call
# `validate_draft_relpath`), so materialize/export still cannot be tricked
# into shipping a smuggled manifest.
async def test_patch_rejects_files_pack_yaml_with_a_named_reason(client, seed):
    """A named test for the specific vector, beyond the generic bad_path
    parametrization above: the 422 detail should tell an author *why*, not
    just that the path is unsafe."""
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]

    response = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={"files": {"pack.yaml": "pack: my-pack\nversion: 99.0.0\n"}},
    )
    assert response.status_code == 422, response.text
    assert "reserved" in response.json()["detail"].lower()

    # Nothing was saved.
    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.json()["files"] == {}


def _smuggling_draft() -> "DraftPack":  # noqa: F821 - imported lazily below
    from tret.db.models import DraftPack

    return DraftPack(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        slug="my-pack",
        manifest_json={
            "pack": "my-pack",
            "version": "1.0.0",
            "display_name": "My Pack",
            "methods": [],
        },
        files={
            # A whole replacement manifest, methods included — what PATCH's
            # `_reject_methods` (which only inspects `manifest_json`) would
            # never see if this were allowed to win.
            "pack.yaml": (
                "pack: my-pack\nversion: 99.0.0\ndisplay_name: Smuggled\n"
                "methods:\n  - slug: evil\n    display_name: Evil\n"
                "    entrypoint: methods/evil.py\n"
            ),
        },
    )


def test_materialize_draft_never_lets_files_pack_yaml_win(tmp_path):
    from tret.packs.draft import DraftPathError, materialize_draft, validate_draft_relpath

    # The key is rejected outright wherever it is validated...
    with pytest.raises(DraftPathError):
        validate_draft_relpath("pack.yaml")

    # ...and even bypassing that (this draft's files dict already has the key,
    # as if it reached the row some other way), materialize_draft's own
    # write-files-then-manifest order means the generated manifest is what
    # ends up on disk, not the smuggled one.
    draft = _smuggling_draft()
    pack_dir = materialize_draft(draft, root=tmp_path / "materialized")
    manifest = yaml.safe_load((pack_dir / "pack.yaml").read_text())
    assert manifest["version"] == "1.0.0"
    assert manifest["display_name"] == "My Pack"
    assert manifest.get("methods") == []


def test_build_draft_archive_never_contains_a_smuggled_manifest():
    from tret.packs.draft import build_draft_archive

    draft = _smuggling_draft()
    archive_bytes = build_draft_archive(draft)
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as tar:
        names = [m.name for m in tar.getmembers()]
        # Exactly one pack.yaml member — never two entries racing an
        # extractor's own last-one-wins behaviour.
        assert names.count("pack.yaml") == 1
        manifest = yaml.safe_load(tar.extractfile("pack.yaml").read())
        assert manifest["version"] == "1.0.0"
        assert manifest["display_name"] == "My Pack"
        assert manifest.get("methods") == []


# ── validate: parity with a filesystem pack ──────────────────────────────────
def _manifest_with_bad_doctrine_selector() -> dict:
    return {
        "pack": "my-pack",
        "version": "0.1.0",
        "display_name": "My Pack",
        "doctrine": ["doctrine.md"],
        "task_types": [
            {
                "slug": "t1",
                "display_name": "T1",
                "shape": "freeform",
                "doctrine": ["doctrine.md#Missing Heading"],
            }
        ],
        "datasets": [],
        "methods": [],
    }


_DOCTRINE_TEXT = "# Title\nIntro text.\n\n## Other Heading\nBody.\n"


async def test_validate_matches_a_filesystem_packs_error_text(client, seed, tmp_path):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": _manifest_with_bad_doctrine_selector(),
            "files": {"doctrine.md": _DOCTRINE_TEXT},
        },
    )

    response = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["valid"] is False

    # The same manifest + doctrine file, laid out on a real filesystem and
    # validated by the unchanged validate_pack, must produce identical errors.
    fs_root = tmp_path / "fs-pack"
    fs_root.mkdir()
    (fs_root / "pack.yaml").write_text(
        yaml.safe_dump(_manifest_with_bad_doctrine_selector(), sort_keys=False)
    )
    (fs_root / "doctrine.md").write_text(_DOCTRINE_TEXT)
    _, _, fs_errors = validate_pack(fs_root)

    assert body["errors"] == fs_errors
    assert any("names a heading that does not exist" in e for e in body["errors"])


async def test_validate_reports_missing_doctrine_file(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "0.1.0",
                "display_name": "My Pack",
                "doctrine": ["missing.md"],
                "task_types": [],
                "datasets": [],
                "methods": [],
            }
        },
    )

    response = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    body = response.json()
    assert body["valid"] is False
    assert "doctrine file missing: missing.md" in body["errors"]


async def test_validate_a_clean_draft_is_valid(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "0.1.0",
                "display_name": "My Pack",
                "doctrine": ["doctrine.md"],
                "task_types": [],
                "datasets": [],
                "methods": [],
            },
            "files": {"doctrine.md": _DOCTRINE_TEXT},
        },
    )

    response = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    body = response.json()
    assert body["valid"] is True, body["errors"]
    assert body["summary"]["pack"] == "my-pack"
    assert body["summary"]["doctrine_files"] == ["doctrine.md"]


# ── a draft with harnesses: presets validates like any other manifest field ──
async def test_a_draft_with_a_valid_harness_preset_validates(client, seed):
    """`manifest_json.harnesses` is not `methods` — `_reject_methods` must not
    over-reject it — and a preset referencing a real task_type/tool in the
    same draft round-trips through the unchanged `validate_pack` cleanly."""
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    patched = await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "0.1.0",
                "display_name": "My Pack",
                "doctrine": [],
                "task_types": [
                    {"slug": "t1", "display_name": "T1", "shape": "freeform"},
                ],
                "datasets": [],
                "methods": [],
                "harnesses": [
                    {
                        "name": "My Harness",
                        "description": "A draft harness preset.",
                        "task_types": ["t1"],
                        "tools": ["lookup_dataset"],
                        "suggested_cost_tier": "standard",
                    }
                ],
            }
        },
    )
    assert patched.status_code == 200, patched.text

    response = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    body = response.json()
    assert body["valid"] is True, body["errors"]


async def test_a_draft_with_an_invalid_harness_preset_fails_validation(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "0.1.0",
                "display_name": "My Pack",
                "doctrine": [],
                "task_types": [],
                "datasets": [],
                "methods": [],
                "harnesses": [
                    {
                        "name": "My Harness",
                        "task_types": [],
                        "tools": ["not_a_real_tool"],
                    }
                ],
            }
        },
    )

    response = await client.post(f"/api/packs/drafts/{draft_id}/validate")
    body = response.json()
    assert body["valid"] is False
    assert any("unknown tool 'not_a_real_tool'" in e for e in body["errors"])


# ── test-install: draft-suffixed version, no collision on repeat ────────────
async def _make_installable_draft(client) -> str:
    draft_id = (await client.post("/api/packs/drafts", json={"slug": "my-pack"})).json()["id"]
    await client.patch(
        f"/api/packs/drafts/{draft_id}",
        json={
            "manifest_json": {
                "pack": "my-pack",
                "version": "1.0.0",
                "display_name": "My Pack",
                "doctrine": ["doctrine.md"],
                "task_types": [],
                "datasets": [],
                "methods": [],
            },
            "files": {"doctrine.md": _DOCTRINE_TEXT},
        },
    )
    return draft_id


async def test_test_install_uses_a_draft_suffixed_version(client, seed, storage):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = await _make_installable_draft(client)

    response = await client.post(f"/api/packs/drafts/{draft_id}/test-install")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["slug"] == "my-pack"
    assert body["version"] == "1.0.0+draft.1"

    listed = await client.get("/api/packs")
    assert body["id"] in {p["id"] for p in listed.json()}


async def test_test_install_maps_a_unique_violation_to_409_not_500(client, seed, storage, monkeypatch):
    """The same concurrent-brand-new-install race `api/packs.py::install_archive`
    guards against (test_pack_archive.py's own equivalent test): two
    test-installs of the same never-before-seen `{version}+draft.{n}` both
    pass `install_pack`'s own idempotency check before either commits, and
    race the unique constraint at insert. Exercised by making `install_pack`
    itself raise the IntegrityError such a race produces — the thing under
    test is `test_install_draft`'s mapping of that exception to 409 (and
    rolling the session back so it stays usable), not reproducing genuine
    thread/process concurrency."""
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = await _make_installable_draft(client)

    async def _boom(*args, **kwargs):
        raise IntegrityError("INSERT INTO packs ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(pack_builder, "install_pack", _boom)

    response = await client.post(f"/api/packs/drafts/{draft_id}/test-install")
    assert response.status_code == 409, response.text

    # The session survived the rollback: a follow-up request against it works.
    fetched = await client.get(f"/api/packs/drafts/{draft_id}")
    assert fetched.status_code == 200


async def test_repeated_test_install_increments_n_without_collision(client, seed, storage):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = await _make_installable_draft(client)

    first = (await client.post(f"/api/packs/drafts/{draft_id}/test-install")).json()
    second = (await client.post(f"/api/packs/drafts/{draft_id}/test-install")).json()

    assert first["version"] == "1.0.0+draft.1"
    assert second["version"] == "1.0.0+draft.2"
    assert first["id"] != second["id"]

    listed = (await client.get("/api/packs")).json()
    versions = {p["version"] for p in listed if p["slug"] == "my-pack"}
    assert versions == {"1.0.0+draft.1", "1.0.0+draft.2"}


async def test_test_install_lands_in_the_drafts_own_workspace(client, seed, storage):
    """Test-installs land wherever the draft lives — an admin who belongs to
    two workspaces must see the newly test-installed pack from the draft's
    own workspace, and not from the other one."""
    team_a = make_workspace("Alpha Co")
    team_b = make_workspace("Bravo Co")
    project_a = Project(id=uuid.uuid4(), workspace_id=team_a.id, name="Sample A")
    project_b = Project(id=uuid.uuid4(), workspace_id=team_b.id, name="Sample B")
    admin = make_user("dual@example.com")
    await seed(
        team_a, team_b, project_a, project_b, admin,
        make_member(admin, team_a, role="admin"),
        make_member(admin, team_b, role="admin"),
    )
    await login(client, admin.email)
    await client.post("/api/auth/workspace", json={"workspace_id": str(team_a.id)})

    draft_id = await _make_installable_draft(client)
    installed = (await client.post(f"/api/packs/drafts/{draft_id}/test-install")).json()

    listed_a = await client.get("/api/packs")
    assert installed["id"] in {p["id"] for p in listed_a.json()}

    await client.post("/api/auth/workspace", json={"workspace_id": str(team_b.id)})
    listed_b = await client.get("/api/packs")
    assert installed["id"] not in {p["id"] for p in listed_b.json()}


# ── export → archive install round trip ──────────────────────────────────────
async def test_export_produces_a_flat_tar_gz_matching_the_draft(client, seed):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = await _make_installable_draft(client)

    response = await client.get(f"/api/packs/drafts/{draft_id}/export")
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]

    with tarfile.open(fileobj=io.BytesIO(response.content), mode="r:gz") as tar:
        names = sorted(m.name for m in tar.getmembers())
        assert names == ["doctrine.md", "pack.yaml"]
        manifest = yaml.safe_load(tar.extractfile("pack.yaml").read())
        assert manifest["pack"] == "my-pack"
        assert manifest["version"] == "1.0.0"  # no draft suffix on export
        doctrine = tar.extractfile("doctrine.md").read().decode()
        assert doctrine == _DOCTRINE_TEXT


async def test_exported_archive_installs_via_the_archive_endpoint(client, seed, storage):
    _, _, admin = await _admin_workspace(seed)
    await login(client, admin.email)
    draft_id = await _make_installable_draft(client)

    exported = await client.get(f"/api/packs/drafts/{draft_id}/export")
    assert exported.status_code == 200

    installed = await client.post(
        "/api/packs/install/archive",
        files={"file": ("my-pack.tar.gz", exported.content, "application/gzip")},
    )
    assert installed.status_code == 200, installed.text
    body = installed.json()
    assert body["slug"] == "my-pack"
    assert body["version"] == "1.0.0"
    assert body["doctrine_files"] == ["doctrine.md"]

    detail = await client.get(f"/api/packs/{body['id']}")
    assert detail.json()["doctrine_contents"]["doctrine.md"] == _DOCTRINE_TEXT
