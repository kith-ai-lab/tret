"""Archive-based pack install: `packs/archive.py`'s tar.gz extraction, the
`install_pack_from_archive` staging/rename sequence in `packs/loader.py`, and
the two API endpoints built on them (`POST /api/packs/install/archive`,
`DELETE /api/packs/{id}`).

Archive bytes are third-party input — same trust level as an uploaded document
(see test_documents_api.py's docstring) — so most of this file is attack
fixtures: a hostile tar.gz gets one shot at each of the ways `extract_pack_archive`
promises to refuse it (path escape, symlink/hardlink/device members, an
oversized compressed archive, and a decompression bomb declared per-member or
across the whole archive), plus the member-count cap. The hash round-trip test
is the positive case: a real pack, tarred and re-extracted, must hash exactly
as it did on disk.
"""
from __future__ import annotations

import errno
import io
import tarfile
import uuid
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth, packs as packs_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Dataset,
    Finding,
    Harness,
    MethodRun,
    Pack,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceMember,
)
from tret.packs.archive import (
    MAX_COMPRESSED_BYTES,
    MAX_MEMBER_COUNT,
    MAX_MEMBER_DECOMPRESSED_BYTES,
    MAX_TOTAL_DECOMPRESSED_BYTES,
    PackArchiveError,
    extract_pack_archive,
)
from tret.packs.integrity import pack_content_hash
from tret.packs.loader import (
    PackInstallConflict,
    _swap_into_final_dir,
    _unwrap_sole_directory,
    install_pack_from_archive,
)
from tret.packs.storage import PackStorage

PACKS_DIR = Path(__file__).parent.parent.parent / "packs"

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


# ── archive-building helpers ─────────────────────────────────────────────────


def _archive_from_entries(entries: list[tuple[tarfile.TarInfo, bytes | None]]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for info, data in entries:
            tar.addfile(info, io.BytesIO(data) if data is not None else None)
    return buf.getvalue()


def _reg(name: str, data: bytes) -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name=name)
    info.size = len(data)
    info.type = tarfile.REGTYPE
    return info, data


def _dir(name: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name=name)
    info.type = tarfile.DIRTYPE
    return info, None


def _special(name: str, kind: str, linkname: str = "") -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name=name)
    info.type = {
        "symlink": tarfile.SYMTYPE,
        "hardlink": tarfile.LNKTYPE,
        "chardev": tarfile.CHRTYPE,
        "blockdev": tarfile.BLKTYPE,
        "fifo": tarfile.FIFOTYPE,
    }[kind]
    if linkname:
        info.linkname = linkname
    return info, None


def _archive_from_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(src).as_posix(), recursive=False)
    return buf.getvalue()


def _archive_from_dir_wrapped(src: Path, wrapper: str) -> bytes:
    """Same as `_archive_from_dir`, but every member is nested one level
    under `wrapper` — the shape `tar czf pack.tar.gz mypack/` produces, where
    the archive's own top level holds one directory rather than `pack.yaml`
    directly."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                arcname = f"{wrapper}/{path.relative_to(src).as_posix()}"
                tar.add(path, arcname=arcname, recursive=False)
    return buf.getvalue()


def _build_minimal_pack(root: Path, *, description: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(
        f"pack: sample\nversion: 0.1.0\ndisplay_name: Sample\ndescription: {description}\n"
    )
    return root


# ── extract_pack_archive: the hash round-trip property ──────────────────────


def test_hash_round_trip_over_the_real_climate_risk_fixture(tmp_path):
    """Tarring a real pack and extracting it back must reproduce the exact
    directory `pack_content_hash` pins — the property `install_pack_from_archive`
    depends on to treat an archive install exactly like a path install."""
    source = PACKS_DIR / "climate-risk"
    extracted = tmp_path / "extracted"
    extract_pack_archive(_archive_from_dir(source), extracted)
    assert pack_content_hash(extracted) == pack_content_hash(source)
    # Sanity: the round trip actually produced files, not an empty directory.
    assert (extracted / "pack.yaml").is_file()
    assert (extracted / "methods" / "ghg_inventory.py").is_file()


# ── attack fixture 1: oversized compressed archive (checked before decompression) ──


def test_rejects_an_oversized_compressed_archive(tmp_path):
    oversized = b"x" * (MAX_COMPRESSED_BYTES + 1)
    with pytest.raises(PackArchiveError, match="too large"):
        extract_pack_archive(oversized, tmp_path / "dest")
    # Never even attempted: the destination is not created.
    assert not (tmp_path / "dest").exists()


# ── attack fixture 2: path traversal (zip-slip) ──────────────────────────────


@pytest.mark.parametrize(
    "name",
    ["../evil.txt", "../../etc/cron.d/evil", "methods/../../escape.py", "/etc/passwd"],
)
def test_rejects_path_traversal_members(tmp_path, name):
    archive = _archive_from_entries([_reg(name, b"hostile")])
    with pytest.raises(PackArchiveError, match="escapes|absolute"):
        extract_pack_archive(archive, tmp_path / "dest")
    # Nothing was written outside the destination.
    assert not (tmp_path / "evil.txt").exists()
    assert not (tmp_path.parent / "escape.py").exists()


# ── attack fixture 3: symlink / hardlink / device members ───────────────────


@pytest.mark.parametrize(
    "kind,linkname",
    [
        ("symlink", "/etc/passwd"),
        ("hardlink", "pack.yaml"),
        ("chardev", ""),
        ("blockdev", ""),
        ("fifo", ""),
    ],
)
def test_rejects_symlink_hardlink_and_device_members(tmp_path, kind, linkname):
    archive = _archive_from_entries([_special("methods/evil", kind, linkname)])
    with pytest.raises(PackArchiveError, match="pack archives may not contain"):
        extract_pack_archive(archive, tmp_path / "dest")


# ── attack fixture 4: decompression bomb (per-member and cumulative) ────────


def test_rejects_a_per_member_decompression_bomb(tmp_path):
    """Highly compressible content whose *declared* (and actual) decompressed
    size alone exceeds the per-member cap — a small compressed archive, a huge
    payload once unpacked."""
    bomb = bytes(MAX_MEMBER_DECOMPRESSED_BYTES + 1)  # all zero bytes: compresses tiny
    archive = _archive_from_entries([_reg("data/huge.csv", bomb)])
    assert len(archive) < MAX_COMPRESSED_BYTES  # confirms this really is a bomb, not just big
    with pytest.raises(PackArchiveError, match="decompressed"):
        extract_pack_archive(archive, tmp_path / "dest")


def test_rejects_a_cumulative_decompression_bomb(tmp_path):
    """No single member exceeds the per-member cap, but three of them together
    exceed the cumulative cap."""
    chunk_size = MAX_TOTAL_DECOMPRESSED_BYTES // 3 + 1024
    assert chunk_size < MAX_MEMBER_DECOMPRESSED_BYTES
    chunk = bytes(chunk_size)
    archive = _archive_from_entries(
        [_reg(f"data/part-{i}.csv", chunk) for i in range(3)]
    )
    assert len(archive) < MAX_COMPRESSED_BYTES
    with pytest.raises(PackArchiveError, match="total decompressed size"):
        extract_pack_archive(archive, tmp_path / "dest")


# ── attack fixture 5: member count ───────────────────────────────────────────


def test_rejects_too_many_members(tmp_path):
    entries = [_reg(f"f{i}.txt", b"") for i in range(MAX_MEMBER_COUNT + 1)]
    archive = _archive_from_entries(entries)
    with pytest.raises(PackArchiveError, match="too many members"):
        extract_pack_archive(archive, tmp_path / "dest")


# ── a directory member is allowed and creates the directory ─────────────────


def test_directory_members_are_created(tmp_path):
    archive = _archive_from_entries([_dir("doctrine"), _reg("doctrine/a.md", b"# A\n")])
    dest = tmp_path / "dest"
    extract_pack_archive(archive, dest)
    assert (dest / "doctrine").is_dir()
    assert (dest / "doctrine" / "a.md").read_text() == "# A\n"


# ── _unwrap_sole_directory: `tar czf pack.tar.gz mypack/`'s natural shape ────


def test_unwrap_sole_directory_finds_the_nested_pack_root(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    nested = staging / "mypack"
    nested.mkdir()
    (nested / "pack.yaml").write_text("x")
    assert _unwrap_sole_directory(staging) == nested


def test_unwrap_sole_directory_leaves_a_top_level_manifest_alone(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "pack.yaml").write_text("x")
    assert _unwrap_sole_directory(staging) == staging


def test_unwrap_sole_directory_leaves_multiple_top_level_entries_alone(tmp_path):
    staging = tmp_path / "staging"
    staging.mkdir()
    (staging / "a").mkdir()
    (staging / "b.txt").write_text("x")
    assert _unwrap_sole_directory(staging) == staging


# ── _swap_into_final_dir: the trash-swap that replaces final_dir atomically ──


def test_swap_into_final_dir_moves_source_when_final_is_absent(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text("a")
    final_dir = tmp_path / "final"

    _swap_into_final_dir(source, final_dir, content_hash="unused")

    assert final_dir.is_dir()
    assert (final_dir / "pack.yaml").read_text() == "a"
    assert not source.exists()


def test_swap_into_final_dir_replaces_existing_content_via_trash(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text("new")
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    (final_dir / "pack.yaml").write_text("old")

    _swap_into_final_dir(source, final_dir, content_hash="unused")

    assert (final_dir / "pack.yaml").read_text() == "new"
    assert not source.exists()
    assert not list(tmp_path.glob(".trash-*"))  # trash cleaned up, not leaked


def test_swap_into_final_dir_restores_the_original_on_an_unexpected_rename_failure(
    tmp_path, monkeypatch
):
    """Not the concurrent-loser (ENOTEMPTY/EEXIST) case — a genuine I/O
    failure. `final_dir` must never be left missing: the trashed original is
    restored before the failure propagates."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text("new")
    final_dir = tmp_path / "final"
    final_dir.mkdir()
    (final_dir / "pack.yaml").write_text("old")

    real_rename = Path.rename

    def flaky_rename(self, target):
        if self == source:
            raise OSError(errno.EIO, "simulated I/O error")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(OSError):
        _swap_into_final_dir(source, final_dir, content_hash="unused")

    assert (final_dir / "pack.yaml").read_text() == "old"  # restored, not left missing
    assert not list(tmp_path.glob(".trash-*"))


def test_swap_into_final_dir_treats_a_content_matching_concurrent_loser_as_success(
    tmp_path, monkeypatch
):
    """The ordinary shape of two concurrent re-installs of the same pack id:
    another install recreates `final_dir` in the gap between us trashing the
    old one and renaming our own content in. If the two installs agreed on
    the same content, losing the race is not a failure."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text("same")
    same_hash = pack_content_hash(source)
    final_dir = tmp_path / "final"

    real_rename = Path.rename

    def flaky_rename(self, target):
        if self == source:
            final_dir.mkdir()
            (final_dir / "pack.yaml").write_text("same")  # the "winner"'s content
            raise OSError(errno.ENOTEMPTY, "simulated concurrent winner")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    _swap_into_final_dir(source, final_dir, content_hash=same_hash)  # does not raise

    assert not source.exists()  # our own copy was cleaned up
    assert (final_dir / "pack.yaml").read_text() == "same"


def test_swap_into_final_dir_raises_a_conflict_when_the_concurrent_loser_content_differs(
    tmp_path, monkeypatch
):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text("mine")
    my_hash = pack_content_hash(source)
    final_dir = tmp_path / "final"

    real_rename = Path.rename

    def flaky_rename(self, target):
        if self == source:
            final_dir.mkdir()
            (final_dir / "pack.yaml").write_text("theirs")  # different content
            raise OSError(errno.EEXIST, "simulated concurrent winner")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    with pytest.raises(PackInstallConflict):
        _swap_into_final_dir(source, final_dir, content_hash=my_hash)

    assert not source.exists()  # still cleaned up even though it's a real conflict


# ── loader integration: staging, idempotent re-install, dir replacement ─────


@pytest_asyncio.fixture
async def engine():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )

    # sqlite ignores FOREIGN KEY constraints unless a connection turns them on
    # for itself — off by default would let DELETE /api/packs/{id} silently
    # leave a Run/Finding/MethodRun/Dataset row pointing at a deleted pack
    # instead of ever exercising the precondition checks (or the Dataset
    # NULL-out) this file pins. Postgres enforces this by default; this
    # brings sqlite in line so those paths are actually exercised here (same
    # pattern tret-cloud's tests/conftest.py uses for the same reason).
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
        # One `add()` + `flush()` per row, in the exact order given, rather
        # than a single `add_all` + commit. With FK enforcement on (this
        # file's `engine` fixture above), a batched flush does not honor
        # `rows`' own order — SQLAlchemy only topologically sorts flush
        # order across mapped classes that share a `relationship()`, which
        # this schema mostly does not use, so `add_all([workspace, project])`
        # can and did insert `projects` before `workspaces` even though the
        # workspace was listed (and added) first, violating the FK before a
        # single row here was ever meant to test that. Flushing one row at a
        # time sidesteps that: every call site already lists rows
        # parent-before-child (a workspace before its project, a user before
        # its membership, ...), and a flush with only one new row pending has
        # no cross-table order left to get wrong.
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


async def test_archive_install_is_idempotent_and_replaces_the_directory(
    session_factory, seed, storage, tmp_path
):
    """Installing the same (workspace, slug, version) twice reuses the Pack
    row (loader.py's own idempotency, unchanged) — so the permanent directory
    is the same path both times, and its *contents* come from whichever
    archive was installed most recently rather than merging the two."""
    workspace = make_workspace("Acme")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"

    archive_v1 = _archive_from_dir(_build_minimal_pack(tmp_path / "v1", description="first"))
    async with session_factory() as db:
        pack1 = await install_pack_from_archive(db, workspace.id, project.id, archive_v1)
    on_disk = packs_root / str(pack1.id)
    assert on_disk.is_dir()
    assert "first" in (on_disk / "pack.yaml").read_text()
    assert not list(packs_root.glob(".staging-*"))  # no leftover staging dirs

    archive_v2 = _archive_from_dir(_build_minimal_pack(tmp_path / "v2", description="second"))
    async with session_factory() as db:
        pack2 = await install_pack_from_archive(db, workspace.id, project.id, archive_v2)

    assert pack2.id == pack1.id  # same version -> same row
    assert on_disk.is_dir()
    assert "second" in (on_disk / "pack.yaml").read_text()
    assert not list(packs_root.glob(".staging-*"))

    async with session_factory() as db:
        rows = (await db.execute(select(Pack).where(Pack.workspace_id == workspace.id))).scalars().all()
    assert len(rows) == 1  # not a second row


async def test_archive_install_rolls_back_the_staging_dir_on_a_hostile_archive(
    session_factory, seed, storage, tmp_path
):
    workspace = make_workspace("Acme")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    hostile = _archive_from_entries([_reg("../evil.txt", b"x")])
    async with session_factory() as db:
        with pytest.raises(PackArchiveError):
            await install_pack_from_archive(db, workspace.id, project.id, hostile)

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    remaining = list(packs_root.glob("*")) if packs_root.exists() else []
    assert remaining == []  # staging dir was removed, nothing installed


async def test_archive_install_unwraps_a_sole_top_level_directory(
    session_factory, seed, storage, tmp_path
):
    """`tar czf pack.tar.gz mypack/` — the archive's own top level holds one
    directory, not `pack.yaml` directly. install must still find it."""
    workspace = make_workspace("Acme")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_minimal_pack(tmp_path / "mypack", description="wrapped")
    archive = _archive_from_dir_wrapped(pack_dir, "mypack")

    async with session_factory() as db:
        pack = await install_pack_from_archive(db, workspace.id, project.id, archive)

    assert pack.slug == "sample"
    final = Path(pack.source_path)
    assert (final / "pack.yaml").is_file()  # unwrapped: not final/mypack/pack.yaml
    assert final.name != "mypack"  # landed at the permanent {pack.id} dir, not the wrapper's name

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert not list(packs_root.glob(".staging-*"))  # no leftover wrapper/staging dirs


async def test_archive_reinstall_restores_the_original_directory_on_a_swap_failure(
    session_factory, seed, storage, tmp_path, monkeypatch
):
    """A genuine I/O failure during a re-install's directory swap (not the
    concurrent-loser ENOTEMPTY/EEXIST case) must leave the pack's permanent
    directory holding its original content, not missing — and the Pack row
    must still be there afterward."""
    workspace = make_workspace("Acme")
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"

    archive_v1 = _archive_from_dir(_build_minimal_pack(tmp_path / "v1", description="first"))
    async with session_factory() as db:
        pack1 = await install_pack_from_archive(db, workspace.id, project.id, archive_v1)
    final_dir = packs_root / str(pack1.id)
    assert "first" in (final_dir / "pack.yaml").read_text()

    real_rename = Path.rename

    def flaky_rename(self, target):
        # Targets exactly the swap's "move new content into final_dir" step:
        # the staging directory (no wrapper, so it *is* the pack root passed
        # to the swap) is the only renamed path named this way.
        if self.name.startswith(".staging-"):
            raise OSError(errno.EIO, "simulated I/O error")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", flaky_rename)

    archive_v2 = _archive_from_dir(_build_minimal_pack(tmp_path / "v2", description="second"))
    async with session_factory() as db:
        with pytest.raises(OSError):
            await install_pack_from_archive(db, workspace.id, project.id, archive_v2)

    # The original directory is intact — never left missing or half-replaced.
    assert final_dir.is_dir()
    assert "first" in (final_dir / "pack.yaml").read_text()
    assert not list(packs_root.glob(".trash-*"))
    assert not list(packs_root.glob(".staging-*"))

    # The row is still there and still valid.
    async with session_factory() as db:
        row = await db.get(Pack, pack1.id)
    assert row is not None
    assert row.slug == "sample"
    assert row.version == "0.1.0"


async def test_archive_install_endpoint_maps_a_unique_violation_to_409_not_500(
    client, seed, storage, monkeypatch, tmp_path
):
    """The concurrent-brand-new-install race finding #7 targets: two uploads
    of the same never-before-seen (workspace, slug, version) both pass
    install_pack's own idempotency check (neither sees the other's row yet)
    and race the unique constraint at insert. Exercised here by making
    install_pack_from_archive itself raise the IntegrityError such a race
    produces — the thing under test is the endpoint's mapping of that
    exception to 409, not reproducing genuine thread/process concurrency."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    async def _boom(*args, **kwargs):
        raise IntegrityError("INSERT INTO packs ...", {}, Exception("UNIQUE constraint failed"))

    monkeypatch.setattr(packs_api, "install_pack_from_archive", _boom)

    archive = _archive_from_dir(_build_minimal_pack(tmp_path / "p", description="x"))
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 409


async def test_archive_install_endpoint_missing_manifest_error_has_no_server_path(
    client, seed, storage
):
    """Finding #3: the missing-manifest error must read as an archive-authoring
    mistake, in plain relative terms — never the server's own staging path."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    archive = _archive_from_entries([_reg("README.md", b"no manifest in this archive")])
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 422
    errors = response.json()["detail"]["errors"]  # HTTPException(422, {"errors": [...]})
    assert errors == ["the archive must contain pack.yaml at its top level"]
    assert str(get_settings().storage_dir) not in response.text
    assert ".staging-" not in response.text


# ── API: POST /api/packs/install/archive ─────────────────────────────────────


async def test_archive_install_endpoint_requires_workspace_admin(client, seed, storage, tmp_path):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    analyst = make_user("analyst@example.com")
    await seed(team, project, analyst, make_member(analyst, team, role="analyst"))
    await login(client, analyst.email)

    archive = _archive_from_dir(_build_minimal_pack(tmp_path / "p", description="x"))
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 403


async def test_workspace_admin_can_install_via_archive(client, seed, storage, tmp_path):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    archive = _archive_from_dir(_build_minimal_pack(tmp_path / "p", description="x"))
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["slug"] == "sample"

    listed = await client.get("/api/packs")
    assert response.json()["id"] in {p["id"] for p in listed.json()}


async def test_archive_install_endpoint_rejects_a_hostile_archive(client, seed, storage):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    hostile = _archive_from_entries([_reg("../evil.txt", b"x")])
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", hostile, "application/gzip")},
    )
    assert response.status_code == 422


async def test_archive_install_endpoint_413s_an_oversized_upload(client, seed, storage):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    oversized = b"x" * (MAX_COMPRESSED_BYTES + 1)
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", oversized, "application/gzip")},
    )
    assert response.status_code == 413


# ── API: DELETE /api/packs/{id} ──────────────────────────────────────────────


async def _install_via_archive(client, description="x", name="p"):
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        archive = _archive_from_dir(_build_minimal_pack(Path(td) / name, description=description))
    response = await client.post(
        "/api/packs/install/archive",
        files={"file": ("pack.tar.gz", archive, "application/gzip")},
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_delete_requires_workspace_admin(client, seed, storage):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    analyst = make_user("analyst@example.com")
    await seed(
        team, project, admin, analyst,
        make_member(admin, team, role="admin"),
        make_member(analyst, team, role="analyst"),
    )
    await login(client, admin.email)
    pack = await _install_via_archive(client)

    await login(client, analyst.email)
    response = await client.delete(f"/api/packs/{pack['id']}")
    assert response.status_code == 403


async def test_delete_404s_for_a_pack_in_another_workspace(client, seed, storage):
    team_a = make_workspace("Alpha")
    team_b = make_workspace("Bravo")
    project_a = Project(id=uuid.uuid4(), workspace_id=team_a.id, name="A")
    project_b = Project(id=uuid.uuid4(), workspace_id=team_b.id, name="B")
    admin = make_user("admin@example.com")
    await seed(
        team_a, team_b, project_a, project_b, admin,
        make_member(admin, team_a, role="admin"),
        make_member(admin, team_b, role="admin"),
    )
    await login(client, admin.email)
    await client.post("/api/auth/workspace", json={"workspace_id": str(team_a.id)})
    pack = await _install_via_archive(client)

    await client.post("/api/auth/workspace", json={"workspace_id": str(team_b.id)})
    response = await client.delete(f"/api/packs/{pack['id']}")
    assert response.status_code == 404


async def test_delete_409s_while_a_harness_references_the_pack(
    client, seed, storage, session_factory
):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    async with session_factory() as db:
        db.add(
            Harness(
                workspace_id=team.id,
                pack_id=pack_id,
                name="Uses the pack",
                task_profile="freeform",
                model_policy={"mode": "auto"},
                tool_names=[],
            )
        )
        await db.commit()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 409
    assert "Uses the pack" in response.text

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert (packs_root / str(pack_id)).is_dir()  # untouched


async def _seed_harness(session_factory, team, *, pack_id=None) -> uuid.UUID:
    """A Harness unrelated to the pack under test, just to satisfy Run's
    NOT NULL `harness_id` FK — with FK enforcement on (this file's `engine`
    fixture), Run/Finding rows below need a real one to point at."""
    harness_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(
            Harness(
                id=harness_id,
                workspace_id=team.id,
                pack_id=pack_id,
                name="Generic",
                task_profile="freeform",
                model_policy={"mode": "auto"},
                tool_names=[],
            )
        )
        await db.commit()
    return harness_id


async def test_delete_409s_while_a_run_references_the_pack(client, seed, storage, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    harness_id = await _seed_harness(session_factory, team)
    async with session_factory() as db:
        db.add(
            Run(
                project_id=project.id,
                harness_id=harness_id,
                pack_id=pack_id,
                task_type="chat",
            )
        )
        await db.commit()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 409
    assert "run" in response.text.lower()

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert (packs_root / str(pack_id)).is_dir()  # untouched


async def test_delete_409s_while_a_finding_references_the_pack(
    client, seed, storage, session_factory
):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    harness_id = await _seed_harness(session_factory, team)
    run_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(
            Run(id=run_id, project_id=project.id, harness_id=harness_id, task_type="chat")
        )
        await db.flush()
        db.add(
            Finding(
                run_id=run_id,
                project_id=project.id,
                pack_id=pack_id,
                schema_slug="verdict",
                subject={},
                payload={},
                provenance={},
            )
        )
        await db.commit()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 409
    assert "finding" in response.text.lower()

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert (packs_root / str(pack_id)).is_dir()  # untouched


async def test_delete_409s_while_a_method_run_references_the_pack(
    client, seed, storage, session_factory
):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    async with session_factory() as db:
        db.add(
            MethodRun(
                project_id=project.id,
                pack_id=pack_id,
                method_slug="ghg_inventory",
                code_sha="deadbeef",
            )
        )
        await db.commit()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 409
    assert "method run" in response.text.lower()

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert (packs_root / str(pack_id)).is_dir()  # untouched


async def test_delete_severs_pack_seeded_datasets_instead_of_deleting_them(
    client, seed, storage, session_factory
):
    """Dataset.pack_id is nullable — the plan's "datasets survive uninstall"
    is implemented by severing the link (NULL), not by blocking the delete
    or cascading it onto the Dataset row."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    dataset_id = uuid.uuid4()
    async with session_factory() as db:
        db.add(
            Dataset(
                id=dataset_id,
                project_id=project.id,
                pack_id=pack_id,
                name="seeded",
                schema_json={"columns": []},
                row_count=0,
            )
        )
        await db.commit()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        dataset = await db.get(Dataset, dataset_id)
    assert dataset is not None  # survives
    assert dataset.pack_id is None  # severed, not FK'd to a row that no longer exists


async def test_delete_commits_the_row_before_touching_the_directory(
    client, seed, storage, session_factory, monkeypatch
):
    """Ordering fix: the row delete must be committed before the directory
    removal is even attempted. Proven here by making the removal itself
    blow up — under the old ordering that would be moot (the directory was
    destroyed first, and `db.commit()` was the thing that could still fail
    with an FK violation afterward); under the fixed ordering, the row is
    already durably gone no matter what happens to the directory next."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = uuid.UUID(pack["id"])

    def _boom(self, path):
        raise OSError("simulated directory removal failure")

    monkeypatch.setattr(PackStorage, "remove", _boom)

    try:
        await client.delete(f"/api/packs/{pack_id}")
    except Exception:  # noqa: BLE001 - only the DB state below is under test
        pass

    async with session_factory() as db:
        row = await db.get(Pack, pack_id)
    assert row is None  # committed gone regardless of the directory removal's fate


async def test_delete_removes_the_row_and_the_directory_when_unreferenced(client, seed, storage):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)
    pack = await _install_via_archive(client)
    pack_id = pack["id"]

    packs_root = Path(get_settings().storage_dir).resolve() / "packs"
    assert (packs_root / pack_id).is_dir()

    response = await client.delete(f"/api/packs/{pack_id}")
    assert response.status_code == 200, response.text

    assert not (packs_root / pack_id).exists()
    follow_up = await client.get(f"/api/packs/{pack_id}")
    assert follow_up.status_code == 404


async def test_delete_of_a_path_installed_pack_leaves_its_original_directory_untouched(
    client, seed, storage, tmp_path
):
    """The critical safety property of `PackStorage.owns`: a pack installed
    from an operator-supplied filesystem path (outside `storage_dir/packs/`)
    must have its row deletable without ever rmtree-ing the operator's own
    directory."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    # Global admin (for the path-install gate) who is also this workspace's
    # workspace-admin (for the delete gate) — see api/packs.py for why the
    # two endpoints have different gates.
    admin = make_user("admin@example.com", global_role="admin")
    await seed(team, project, admin, make_member(admin, team, role="admin"))
    await login(client, admin.email)

    operator_dir = tmp_path / "operator-owned-pack-dir"  # deliberately outside `storage`
    _build_minimal_pack(operator_dir, description="operator's own copy")

    response = await client.post("/api/packs/install", json={"path": str(operator_dir)})
    assert response.status_code == 200, response.text
    pack_id = response.json()["id"]

    delete_response = await client.delete(f"/api/packs/{pack_id}")
    assert delete_response.status_code == 200, delete_response.text

    assert operator_dir.is_dir()
    assert (operator_dir / "pack.yaml").read_text() == (
        "pack: sample\nversion: 0.1.0\ndisplay_name: Sample\n"
        "description: operator's own copy\n"
    )
