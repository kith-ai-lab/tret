"""`services/workspace.py::create_workspace` / `seed_workspace_content`: the
split between "every workspace gets the default pack(s)"
(`Settings.seed_default_packs`, independent of `TRET_MULTI_TENANT`) and
"demo/sample content beyond the pack" (`seed_demo_content`, still following
`TRET_MULTI_TENANT` as before).

Real sqlite database — same harness as `test_packs_api.py` /
`test_workspaces_api.py` (see those files' docstrings for why a real database
rather than a fake session): the query shapes under test here (a `Pack` row
existing/not existing for a workspace, harnesses seeded after a mid-flight
failure) are exactly the kind a hand-rolled fake session gets subtly wrong.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.config import get_settings
from tret.db.models import Base, Harness, Pack, User, Workspace, WorkspaceMember
from tret.packs.links import packs_for_harness, set_harness_packs
from tret.services import workspace as workspace_module
from tret.services.workspace import create_workspace, seed_workspace_content

PACKS_DIR = Path(__file__).resolve().parents[2] / "packs"
CLIMATE_PACK = PACKS_DIR / "climate-risk"


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
async def db(session_factory):
    async with session_factory() as session:
        yield session


def make_user(email: str) -> User:
    return User(id=uuid.uuid4(), email=email, display_name=email.split("@")[0].title())


@pytest.fixture(autouse=True)
def _packs_dir(monkeypatch):
    """Point TRET_PACKS_DIR at the repo's real packs/ directory explicitly,
    rather than relying on the default `../packs` resolving correctly by cwd
    (it does today, since the suite always runs via `cd backend && pytest`,
    but being explicit here is self-documenting and immune to that changing).
    Every test in this file also gets `get_settings.cache_clear()`'d before
    and after, since several tests below flip TRET_MULTI_TENANT /
    TRET_SEED_DEFAULT_PACKS too and `get_settings` is `@lru_cache`d.
    """
    monkeypatch.setenv("TRET_PACKS_DIR", str(PACKS_DIR))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _pack(db, workspace_id, *, slug: str = "climate-risk") -> Pack | None:
    return (
        await db.execute(select(Pack).where(Pack.workspace_id == workspace_id, Pack.slug == slug))
    ).scalars().first()


async def _harness_names(db, workspace_id) -> set[str]:
    rows = (
        await db.execute(select(Harness).where(Harness.workspace_id == workspace_id))
    ).scalars().all()
    return {h.name for h in rows}


async def _chat_harness(db, workspace_id) -> Harness:
    return (
        await db.execute(
            select(Harness).where(
                Harness.workspace_id == workspace_id, Harness.task_profile == "chat"
            )
        )
    ).scalars().first()


def make_pack(workspace_id, *, slug: str) -> Pack:
    """A bare Pack row, not routed through `install_pack` — enough to
    exercise `_seed_chat_harness`'s own pack-linking logic, which only reads
    the `packs` table, without needing a real pack directory on disk. Empty
    `task_types` so linking never trips `task_slug_collision`."""
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={"pack": slug, "version": "1.0.0", "display_name": slug.title(), "task_types": []},
        source_path=f"/tmp/{slug}",
    )


# ── pack installation follows TRET_SEED_DEFAULT_PACKS, not TRET_MULTI_TENANT ──
async def test_multi_tenant_team_workspace_gets_the_default_pack(db, monkeypatch):
    """The finding this file pins: today a multi-tenant team/personal
    workspace ships with no packs at all (`seed_demo_content=False`). After
    the split, TRET_SEED_DEFAULT_PACKS (true by default) installs the pack
    regardless of TRET_MULTI_TENANT — while the non-pack demo content (the
    "Sample Engagement" project framing) stays tied to seed_demo_content
    exactly as before.

    Climate Analyst is no longer part of that non-pack demo-content split:
    it now ships as the climate-risk pack's own `harnesses:` preset
    (`packs/loader.py::install_pack`), so it arrives with the pack itself —
    here, in a team workspace under TRET_MULTI_TENANT, same as everywhere
    else the pack installs. Only the "General" (not "Sample Engagement")
    project naming still marks this as a non-demo-content workspace."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()

    owner = make_user("owner@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Acme", kind="team", owner=owner)

    pack = await _pack(db, workspace.id)
    assert pack is not None
    assert pack.version

    assert await _harness_names(db, workspace.id) == {
        "Chat Assistant",
        "General Assistant",
        "Climate Analyst",
    }


async def test_multi_tenant_personal_workspace_gets_the_default_pack(db, monkeypatch):
    """Same finding, through the JIT personal-workspace shape
    (kind='personal'), the code path services/identity.py actually uses."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()

    owner = make_user("grace@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(
        db, "Grace's workspace", kind="personal", owner=owner, seed_demo_content=False
    )

    assert await _pack(db, workspace.id) is not None


async def test_self_host_workspace_still_gets_the_default_pack(db):
    """Not just a multi-tenant-mode change: self-host (TRET_MULTI_TENANT
    unset) also gets the pack via the settings-driven path, same as it always
    has via seed_demo_content — this pins that the two paths now agree."""
    owner = make_user("selfhost@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Default", kind="team", owner=owner, seed_demo_content=True)

    assert await _pack(db, workspace.id) is not None
    # seed_demo_content=True still seeds the Climate Analyst harness.
    assert "Climate Analyst" in await _harness_names(db, workspace.id)


# ── TRET_SEED_DEFAULT_PACKS=false opts every mode out ─────────────────────────
async def test_seed_default_packs_false_skips_it_in_multi_tenant_mode(db, monkeypatch):
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    owner = make_user("noPack@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Acme", kind="team", owner=owner)

    assert await _pack(db, workspace.id) is None
    # The rest of seeding still happens — packs are the only thing gated off.
    assert await _harness_names(db, workspace.id) == {"Chat Assistant", "General Assistant"}


async def test_seed_default_packs_false_skips_it_in_self_host_mode(db, monkeypatch):
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    owner = make_user("noPack2@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Default", kind="team", owner=owner, seed_demo_content=True)

    assert await _pack(db, workspace.id) is None


# ── a pack install failure must not fail workspace creation ───────────────────
async def test_pack_install_failure_does_not_fail_workspace_creation(db, monkeypatch):
    """Simulates install_pack blowing up partway (not a PackValidationError —
    that path never touched the DB and was already safe; this exercises the
    new broad except + rollback path). The workspace, its owner membership,
    and its harnesses must all survive, and — the part that would actually
    catch a wrong commit/rollback ordering — must still be there when read
    back from a *fresh* session, not just the in-memory objects."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()

    async def _boom(db, pack_dir, workspace_id, project_id):
        # A partial write before the failure, to actually exercise the
        # rollback path rather than a no-op failure.
        db.add(
            Pack(
                id=uuid.uuid4(),
                workspace_id=workspace_id,
                slug="doomed",
                version="0.0.0",
                doctrine_sha="deadbeef",
                manifest={},
                source_path=str(pack_dir),
            )
        )
        await db.flush()
        raise RuntimeError("simulated pack install failure")

    monkeypatch.setattr(workspace_module, "install_pack", _boom)

    owner = make_user("resilient@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Acme", kind="team", owner=owner)
    workspace_id = workspace.id

    # The failed pack's own partial write must have been rolled back...
    assert await _pack(db, workspace_id, slug="doomed") is None
    # ...but everything else — the workspace, owner membership, and the
    # harnesses seeded right after the pack loop — must have survived in
    # *this* session. `test_pack_install_failure_workspace_persists_in_a_fresh_session`
    # below re-checks the same shape against a fresh session/connection,
    # which is what actually catches a wrong commit/rollback ordering (a
    # dangling uncommitted transaction would pass here but fail there).
    membership = (
        await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == owner.id,
            )
        )
    ).scalars().first()
    assert membership is not None
    assert membership.role == "owner"
    assert await _harness_names(db, workspace_id) == {"Chat Assistant", "General Assistant"}


async def test_pack_install_failure_workspace_persists_in_a_fresh_session(
    session_factory, monkeypatch
):
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    get_settings.cache_clear()

    async def _boom(db, pack_dir, workspace_id, project_id):
        raise RuntimeError("simulated pack install failure")

    monkeypatch.setattr(workspace_module, "install_pack", _boom)

    async with session_factory() as db:
        owner = make_user("resilient2@example.com")
        db.add(owner)
        await db.flush()
        workspace = await create_workspace(db, "Acme", kind="team", owner=owner)
        workspace_id = workspace.id
        owner_id = owner.id

    async with session_factory() as fresh:
        found = (
            await fresh.execute(select(Workspace).where(Workspace.id == workspace_id))
        ).scalars().first()
        assert found is not None

        membership = (
            await fresh.execute(
                select(WorkspaceMember).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.user_id == owner_id,
                )
            )
        ).scalars().first()
        assert membership is not None
        assert membership.role == "owner"

        harness_names = {
            h.name
            for h in (
                await fresh.execute(select(Harness).where(Harness.workspace_id == workspace_id))
            )
            .scalars()
            .all()
        }
        assert harness_names == {"Chat Assistant", "General Assistant"}


# ── the moved commit: harness-seeding failures roll back the whole creation ──
async def test_a_harness_seeding_failure_rolls_back_the_whole_creation_when_packs_are_off(
    session_factory, monkeypatch
):
    """The finding this test pins: `create_workspace`'s protective commit
    used to run unconditionally right after project creation — before packs
    were even attempted, whether or not `TRET_SEED_DEFAULT_PACKS` was even on.
    With packs off there was nothing that commit protected against, only a
    downside: a failure in the unprotected harness-seeding steps right after
    it (`_seed_chat_harness` / `_seed_default_harnesses`, neither wrapped in a
    savepoint) left a committed, half-seeded workspace sitting around — no
    harnesses, still counting against a workspace cap. Now the commit only
    happens immediately before `_install_configured_packs` runs, so with
    packs off nothing commits until the very end: a harness-seeding failure
    rolls back the workspace, the project, and the owner membership right
    along with it. Checked against a *fresh* session/connection, which is
    what actually catches a wrong commit/rollback ordering."""
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    async def _boom(db, workspace_id):
        raise RuntimeError("simulated harness seeding failure")

    monkeypatch.setattr(workspace_module, "_seed_chat_harness", _boom)

    async with session_factory() as db:
        owner = make_user("rollback@example.com")
        db.add(owner)
        await db.flush()
        with pytest.raises(RuntimeError):
            await create_workspace(db, "Acme", kind="team", owner=owner, seed_demo_content=True)
        owner_id = owner.id

    async with session_factory() as fresh:
        workspaces = (await fresh.execute(select(Workspace))).scalars().all()
        assert workspaces == []  # nothing survived the rollback

        user = await fresh.get(User, owner_id)
        assert user is None  # even the caller's own pending write rolled back with it

        memberships = (await fresh.execute(select(WorkspaceMember))).scalars().all()
        assert memberships == []


# ── the seeded Chat Assistant is linked to every installed pack by default ──
async def test_fresh_workspace_chat_assistant_is_linked_to_every_installed_pack(db):
    """The finding this section pins: the seeded Chat Assistant used to ship
    pack-less. On a brand-new workspace, `_install_configured_packs` runs
    before `_seed_chat_harness` (see `seed_workspace_content`), so by the
    time the Chat Assistant is created, the workspace's pack(s) already
    exist — it must come out linked to all of them, not empty."""
    owner = make_user("packed@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Acme", kind="team", owner=owner)

    pack = await _pack(db, workspace.id)
    assert pack is not None
    chat_harness = await _chat_harness(db, workspace.id)
    linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [pack.id]


async def test_workspace_with_seed_default_packs_off_has_no_chat_links(db, monkeypatch):
    """The flip side: with `TRET_SEED_DEFAULT_PACKS` off, no pack ever exists
    in the workspace, so the Chat Assistant has nothing to link — it stays
    exactly as pack-less as it always was."""
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    owner = make_user("unpacked@example.com")
    db.add(owner)
    await db.flush()

    workspace = await create_workspace(db, "Acme", kind="team", owner=owner)

    chat_harness = await _chat_harness(db, workspace.id)
    assert await packs_for_harness(db, chat_harness) == []


async def test_rerunning_seeding_links_packs_into_an_existing_zero_link_chat_harness(
    db, monkeypatch
):
    """A chat harness that already exists but has never been linked to
    anything (e.g. seeded by an older boot, before this default existed) gets
    backfilled the next time `seed_workspace_content` runs — same as the
    tool-name backfill right above it in `_seed_chat_harness`."""
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    workspace = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(workspace)
    await db.flush()
    pack_a = make_pack(workspace.id, slug="pack-a")
    pack_b = make_pack(workspace.id, slug="pack-b")
    db.add_all([pack_a, pack_b])
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    db.add(chat_harness)
    await db.flush()
    assert await packs_for_harness(db, chat_harness) == []

    await seed_workspace_content(db, workspace.id, uuid.uuid4())

    linked_ids = {p.id for p in await packs_for_harness(db, chat_harness)}
    assert linked_ids == {pack_a.id, pack_b.id}


async def test_rerunning_seeding_preserves_a_curated_partial_link_list(db, monkeypatch):
    """The other side of the backfill rule: a chat harness whose default has
    already been applied (`packs_linked_at` set — "already defaulted", per
    the marker's own semantics) is left exactly as an operator set it — even
    when a second pack exists in the workspace that isn't linked. Re-running
    seeding must never silently add to a list someone has curated."""
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    workspace = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(workspace)
    await db.flush()
    pack_a = make_pack(workspace.id, slug="pack-a")
    pack_b = make_pack(workspace.id, slug="pack-b")
    db.add_all([pack_a, pack_b])
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
        packs_linked_at=datetime.now(timezone.utc),
    )
    db.add(chat_harness)
    await db.flush()
    await set_harness_packs(db, chat_harness, [pack_a.id])
    await db.flush()

    await seed_workspace_content(db, workspace.id, uuid.uuid4())

    linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [pack_a.id]


async def test_rerunning_seeding_after_a_deliberate_unlink_all_stays_empty(db, monkeypatch):
    """The finding F3 pins: unlinking everything from the chat harness must
    survive a reboot. A harness whose default was already applied
    (`packs_linked_at` set) and now has zero links — an operator's own
    "unlink all" — must come back from `seed_workspace_content` still
    linked to nothing, not re-defaulted."""
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()

    workspace = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(workspace)
    await db.flush()
    pack = make_pack(workspace.id, slug="pack-a")
    db.add(pack)
    chat_harness = Harness(
        workspace_id=workspace.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=[],
        packs_linked_at=datetime.now(timezone.utc),
    )
    db.add(chat_harness)
    await db.flush()
    assert await packs_for_harness(db, chat_harness) == []  # already unlinked, marker already set

    await seed_workspace_content(db, workspace.id, uuid.uuid4())

    assert await packs_for_harness(db, chat_harness) == []
