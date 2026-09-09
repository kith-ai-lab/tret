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
from tret.packs.links import link_pack_to_harness, packs_for_harness, set_harness_packs
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


def make_chat_harness(workspace_id: uuid.UUID) -> Harness:
    return Harness(
        workspace_id=workspace_id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
    )


def make_bare_pack(workspace_id: uuid.UUID, *, slug: str, version: str = "1.0.0") -> Pack:
    """A Pack row not routed through `install_pack` — for tests that only
    need a pre-existing linked pack to check ordering/replacement against,
    not a real pack directory on disk."""
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        version=version,
        doctrine_sha="deadbeef",
        manifest={"pack": slug, "version": version, "display_name": slug.title(), "task_types": []},
        source_path=f"/tmp/{slug}",
    )


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


# ── a newly installed pack also links into the workspace's Chat Assistant ───


async def test_installing_a_new_pack_appends_to_the_chat_harness(session_factory, seed, tmp_path):
    """Installing a pack into a workspace that already has a chat harness
    (`Harness.task_profile == 'chat'`) links the new pack onto it too — the
    default this whole feature is about — appended after whatever the chat
    harness was already linked to."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    async with session_factory() as db:
        chat_harness = make_chat_harness(workspace.id)
        existing_pack = make_bare_pack(workspace.id, slug="already-there")
        db.add_all([chat_harness, existing_pack])
        await db.flush()
        await set_harness_packs(db, chat_harness, [existing_pack.id])
        await db.commit()
        chat_harness_id = chat_harness.id

    pack_dir = _build_pack_with_preset(tmp_path / "pack")
    async with session_factory() as db:
        pack = await install_pack(db, pack_dir, workspace.id, project.id)

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [existing_pack.id, pack.id]


async def test_installing_a_new_version_replaces_the_old_link_in_place(
    session_factory, seed, tmp_path
):
    """A version-bump install of an already-linked pack's slug replaces that
    link in place (same position) rather than appending a second entry for
    the same pack — checked here with an unrelated pack ahead of it in the
    list, so "in place" actually means something."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    async with session_factory() as db:
        chat_harness = make_chat_harness(workspace.id)
        other_pack = make_bare_pack(workspace.id, slug="other-pack")
        db.add_all([chat_harness, other_pack])
        await db.flush()
        await set_harness_packs(db, chat_harness, [other_pack.id])
        await db.commit()
        chat_harness_id = chat_harness.id

    v1_dir = _build_pack_with_preset(tmp_path / "v1", version="1.0.0")
    async with session_factory() as db:
        v1_pack = await install_pack(db, v1_dir, workspace.id, project.id)

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [other_pack.id, v1_pack.id]

    v2_dir = _build_pack_with_preset(tmp_path / "v2", version="2.0.0")
    async with session_factory() as db:
        v2_pack = await install_pack(db, v2_dir, workspace.id, project.id)

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    # Same position (index 1) as v1 held, not appended as a third link.
    assert [p.id for p in linked] == [other_pack.id, v2_pack.id]


async def test_a_colliding_pack_is_not_linked_to_chat_and_logs_a_warning(
    session_factory, seed, tmp_path, caplog
):
    """A newly installed pack whose task_type slug collides with a pack
    already linked to the chat harness must not be linked — that would
    create exactly the list `api/harnesses.py::_resolve_pack_ids` 422s on —
    and a warning naming the harness, the pack, and the colliding slug is
    logged instead."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    async with session_factory() as db:
        chat_harness = make_chat_harness(workspace.id)
        db.add(chat_harness)
        await db.commit()
        chat_harness_id = chat_harness.id

    pack_a_dir = tmp_path / "a"
    pack_a_dir.mkdir()
    (pack_a_dir / "pack.yaml").write_text(
        "pack: chat-collision-a\nversion: 0.1.0\ndisplay_name: Collision A\n"
        "task_types:\n"
        "  - slug: collide\n    display_name: Collide\n    shape: freeform\n"
    )
    async with session_factory() as db:
        pack_a = await install_pack(db, pack_a_dir, workspace.id, project.id)

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [pack_a.id]  # auto-linked, no collision yet

    pack_b_dir = tmp_path / "b"
    pack_b_dir.mkdir()
    (pack_b_dir / "pack.yaml").write_text(
        "pack: chat-collision-b\nversion: 0.1.0\ndisplay_name: Collision B\n"
        "task_types:\n"
        "  - slug: collide\n    display_name: Collide\n    shape: freeform\n"
    )
    with caplog.at_level(logging.WARNING, logger="tret.packs.links"):
        async with session_factory() as db:
            pack_b = await install_pack(db, pack_b_dir, workspace.id, project.id)

    assert "collide" in caplog.text
    assert "chat-collision-b" in caplog.text
    assert "Chat Assistant" in caplog.text

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    # pack_b was NOT linked — the chat harness is exactly as it was.
    assert [p.id for p in linked] == [pack_a.id]
    assert pack_b.id not in {p.id for p in linked}


# ── link_pack_to_harness: append avoids a full rewrite; multi-duplicate collapse ──


async def test_appending_a_new_pack_does_not_rewrite_the_whole_link_list(
    session_factory, seed, monkeypatch
):
    """F4: the plain-append case must write a single new `HarnessPack` row
    rather than going through `set_harness_packs` (DELETE-all-then-re-INSERT)
    — the rewrite is what creates the lost-update race between two
    concurrent installs auto-linking to the same chat harness. Verified here
    by monkeypatching `set_harness_packs` itself and asserting it is never
    called for an append."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    import tret.packs.links as links_module

    calls = []
    real_set_harness_packs = links_module.set_harness_packs

    async def _tracking_set_harness_packs(db, harness, pack_ids):
        calls.append(list(pack_ids))
        return await real_set_harness_packs(db, harness, pack_ids)

    monkeypatch.setattr(links_module, "set_harness_packs", _tracking_set_harness_packs)

    async with session_factory() as db:
        chat_harness = make_chat_harness(workspace.id)
        pack_a = make_bare_pack(workspace.id, slug="pack-a")
        pack_b = make_bare_pack(workspace.id, slug="pack-b")
        db.add_all([chat_harness, pack_a, pack_b])
        await db.flush()
        await links_module.set_harness_packs(db, chat_harness, [pack_a.id])
        await db.commit()
        chat_harness_id, pack_a_id, pack_b_id = chat_harness.id, pack_a.id, pack_b.id

    calls.clear()  # only care about calls made by link_pack_to_harness below

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        pack_b = await db.get(Pack, pack_b_id)
        changed = await links_module.link_pack_to_harness(db, chat_harness, pack_b)
        await db.commit()
    assert changed is True
    assert calls == []  # the append never went through set_harness_packs

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [pack_a_id, pack_b_id]


async def test_a_pre_existing_duplicate_same_slug_link_collapses_to_one(session_factory, seed):
    """F6: same-slug replacement now handles every matching link, not just
    the first. Simulates a pre-existing duplicate (two different-version
    packs of the same slug both already linked — the kind of state this fix
    exists to clean up rather than perpetuate): linking a third, newer
    version of that slug must replace *both* stale entries with one, at the
    position of the first."""
    workspace = make_workspace()
    project = Project(id=uuid.uuid4(), workspace_id=workspace.id, name="P")
    await seed(workspace, project)

    async with session_factory() as db:
        chat_harness = make_chat_harness(workspace.id)
        other = make_bare_pack(workspace.id, slug="other")
        v1 = make_bare_pack(workspace.id, slug="dup", version="1.0.0")
        v2 = make_bare_pack(workspace.id, slug="dup", version="2.0.0")
        v3 = make_bare_pack(workspace.id, slug="dup", version="3.0.0")
        db.add_all([chat_harness, other, v1, v2, v3])
        await db.flush()
        # Pre-existing duplicate: v1 and v2 both linked (position 1 and 2),
        # with an unrelated pack ahead of them at position 0.
        await set_harness_packs(db, chat_harness, [other.id, v1.id, v2.id])
        await db.commit()
        chat_harness_id, other_id, v3_id = chat_harness.id, other.id, v3.id

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        v3 = await db.get(Pack, v3_id)
        changed = await link_pack_to_harness(db, chat_harness, v3)
        await db.commit()
    assert changed is True

    async with session_factory() as db:
        chat_harness = await db.get(Harness, chat_harness_id)
        linked = await packs_for_harness(db, chat_harness)
    # v1 and v2 collapsed into v3, at v1's original position (1) — not
    # appended, and not left as a second stale entry.
    assert [p.id for p in linked] == [other_id, v3_id]
