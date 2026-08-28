"""`packs/schema.py::HarnessPreset` + `packs/loader.py::install_pack`'s new
harness-instantiation step: a pack's `harnesses:` entries become ready-to-run
workspace `Harness` rows at install time (Plan: "packs ship HARNESS
PRESETS").

Idempotency is by `(workspace, harness name)`, not `(workspace, pack,
version)` — that is what makes a plain re-install a no-op *and* what makes an
upgrade install (same pack slug, new version) leave an already-created,
possibly operator-edited harness alone rather than duplicating or mutating
it. See `install_pack`'s own docstring.

Real sqlite database, FK enforcement on — same harness as
`test_pack_archive.py`'s loader-integration section (a `Harness` row here has
a real FK on `workspace_id`, and its link to a pack is a real FK'd
`harness_packs` row rather than a column on `Harness` itself).
"""
from __future__ import annotations

import io
import logging
import tarfile
import uuid
from pathlib import Path

import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.db.models import Base, Harness, Pack, Project, Workspace
from tret.packs.links import packs_for_harness
from tret.packs.loader import install_pack, install_pack_from_archive

PACKS_DIR = Path(__file__).parent.parent.parent / "packs"


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


def make_workspace(name: str = "Acme") -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def _build_pack_with_preset(
    root: Path,
    *,
    version: str = "0.1.0",
    description: str = "v1 description",
    tools: str = "lookup_dataset",
    tier_line: str = "    suggested_cost_tier: standard\n",
    task_types_line: str = "    task_types: [t1]\n",
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "pack.yaml").write_text(
        f"pack: harness-preset-test\nversion: {version}\ndisplay_name: Harness Preset Test\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: Preset Harness\n"
        f"    description: {description}\n"
        f"{task_types_line}"
        f"    tools: [{tools}]\n"
        f"{tier_line}"
    )
    return root


async def _harness(session_factory, workspace_id, *, name: str = "Preset Harness") -> Harness | None:
    async with session_factory() as db:
        return (
            await db.execute(
                select(Harness).where(Harness.workspace_id == workspace_id, Harness.name == name)
            )
        ).scalars().first()


async def _linked_pack_ids(session_factory, harness: Harness) -> list[uuid.UUID]:
    """A preset harness's linked packs, in position order — the replacement
    for the old single `harness.pack_id` column."""
    async with session_factory() as db:
        return [p.id for p in await packs_for_harness(db, harness)]


# ── install_pack creates the harness with the right shape ───────────────────


async def test_install_creates_a_harness_from_the_preset(session_factory, seed, tmp_path):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "pack")
    async with session_factory() as db:
        pack = await install_pack(db, pack_dir, workspace.id, project.id)

    harness = await _harness(session_factory, workspace.id)
    assert harness is not None
    assert await _linked_pack_ids(session_factory, harness) == [pack.id]
    assert harness.description == "v1 description"
    assert harness.task_profile == "t1"
    assert harness.tool_names == ["lookup_dataset"]
    assert harness.model_policy == {"mode": "auto", "max_cost_tier": "standard"}
    assert harness.is_archived is False


async def test_install_preset_without_a_tier_uses_the_ordinary_auto_default(
    session_factory, seed, tmp_path
):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "pack", tier_line="")
    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)

    harness = await _harness(session_factory, workspace.id)
    assert harness.model_policy == {"mode": "auto"}


async def test_install_preset_without_task_types_defaults_to_freeform(
    session_factory, seed, tmp_path
):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "pack", task_types_line="")
    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)

    harness = await _harness(session_factory, workspace.id)
    assert harness.task_profile == "freeform"


# ── idempotent re-install ────────────────────────────────────────────────────


async def test_reinstalling_the_same_version_does_not_duplicate_the_harness(
    session_factory, seed, tmp_path
):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "pack")
    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)
    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace.id, Harness.name == "Preset Harness"
                )
            )
        ).scalars().all()
    assert len(rows) == 1


# ── upgrade install leaves the existing harness untouched ───────────────────


async def test_upgrade_install_leaves_the_v1_harness_untouched(session_factory, seed, tmp_path):
    """A v2 install of the same pack slug (a new version, not a re-install of
    the same one) must not duplicate or mutate the harness a v1 install
    already created — deliberate-upgrade: an operator who edited the harness
    after v1 installed keeps their edits, and a v2 preset with different
    fields does not silently overwrite them."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    v1_dir = _build_pack_with_preset(tmp_path / "v1", version="1.0.0", description="v1 description")
    async with session_factory() as db:
        v1_pack = await install_pack(db, v1_dir, workspace.id, project.id)

    v2_dir = _build_pack_with_preset(
        tmp_path / "v2", version="2.0.0", description="v2 description — should not appear"
    )
    async with session_factory() as db:
        v2_pack = await install_pack(db, v2_dir, workspace.id, project.id)

    assert v2_pack.id != v1_pack.id  # a genuinely new Pack row, not a re-install
    async with session_factory() as db:
        packs = (
            await db.execute(select(Pack).where(Pack.workspace_id == workspace.id))
        ).scalars().all()
    assert len(packs) == 2

    rows = None
    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace.id, Harness.name == "Preset Harness"
                )
            )
        ).scalars().all()
    assert len(rows) == 1  # still just the one from v1
    assert await _linked_pack_ids(session_factory, rows[0]) == [v1_pack.id]
    assert rows[0].description == "v1 description"


# ── a deliberately archived preset harness survives reboots ────────────────


async def test_reinstall_after_archiving_the_harness_stays_archived(
    session_factory, seed, tmp_path
):
    """`bootstrap.py::bootstrap` re-runs the equivalent of `install_pack` on
    every self-host boot, not just at first install. An operator who
    deliberately archived a preset's auto-created harness must not find it
    silently un-archived — recreated from scratch — the next time the server
    restarts."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "pack")
    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)

    async with session_factory() as db:
        harness = (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace.id, Harness.name == "Preset Harness"
                )
            )
        ).scalar_one()
        harness.is_archived = True
        await db.commit()

    async with session_factory() as db:
        await install_pack(db, pack_dir, workspace.id, project.id)

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace.id, Harness.name == "Preset Harness"
                )
            )
        ).scalars().all()
    assert len(rows) == 1  # not recreated
    assert rows[0].is_archived is True  # still archived


# ── cross-pack name collision is logged, not silent ─────────────────────────


async def test_cross_pack_name_collision_logs_a_warning_naming_both_packs(
    session_factory, seed, tmp_path, caplog
):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_a_dir = _build_pack_with_preset(tmp_path / "a")
    async with session_factory() as db:
        pack_a = await install_pack(db, pack_a_dir, workspace.id, project.id)

    pack_b_dir = tmp_path / "b"
    pack_b_dir.mkdir()
    (pack_b_dir / "pack.yaml").write_text(
        "pack: harness-preset-test-b\nversion: 0.1.0\ndisplay_name: Harness Preset Test B\n"
        "task_types:\n"
        "  - slug: t1\n    display_name: T1\n    shape: freeform\n"
        "harnesses:\n"
        "  - name: Preset Harness\n"
        "    task_types: [t1]\n"
        "    tools: [lookup_dataset]\n"
    )

    with caplog.at_level(logging.WARNING, logger="tret.packs.loader"):
        async with session_factory() as db:
            pack_b = await install_pack(db, pack_b_dir, workspace.id, project.id)

    assert f"{pack_a.slug}@{pack_a.version}" in caplog.text
    assert f"{pack_b.slug}@{pack_b.version}" in caplog.text

    async with session_factory() as db:
        rows = (
            await db.execute(
                select(Harness).where(
                    Harness.workspace_id == workspace.id, Harness.name == "Preset Harness"
                )
            )
        ).scalars().all()
    assert len(rows) == 1  # pack_b's preset was skipped, not duplicated
    assert await _linked_pack_ids(session_factory, rows[0]) == [pack_a.id]


# ── archive install round-trip ───────────────────────────────────────────────


def _archive_from_dir(src: Path) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                tar.add(path, arcname=path.relative_to(src).as_posix(), recursive=False)
    return buf.getvalue()


async def test_archive_install_also_creates_the_preset_harness(
    session_factory, seed, tmp_path, monkeypatch
):
    from tret.config import get_settings

    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))

    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    pack_dir = _build_pack_with_preset(tmp_path / "src")
    archive = _archive_from_dir(pack_dir)
    async with session_factory() as db:
        pack = await install_pack_from_archive(db, workspace.id, project.id, archive)

    harness = await _harness(session_factory, workspace.id)
    assert harness is not None
    assert await _linked_pack_ids(session_factory, harness) == [pack.id]
    assert harness.tool_names == ["lookup_dataset"]


# ── the shipped climate-risk pack, end to end ────────────────────────────────


async def test_installing_the_real_climate_risk_pack_creates_climate_analyst(
    session_factory, seed
):
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    async with session_factory() as db:
        pack = await install_pack(db, PACKS_DIR / "climate-risk", workspace.id, project.id)

    harness = await _harness(session_factory, workspace.id, name="Climate Analyst")
    assert harness is not None
    assert await _linked_pack_ids(session_factory, harness) == [pack.id]
    assert harness.task_profile == "divergence_assessment"
    assert harness.model_policy == {"mode": "auto", "max_cost_tier": "premium"}
    assert harness.tool_names == []
