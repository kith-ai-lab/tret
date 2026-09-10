"""services/bootstrap.py's membership backstop, and why it must never run in
multi-tenant mode.

`_ensure_every_user_has_a_membership` exists so a self-hosted database that
predates workspaces (or a hand-inserted user row) never leaves a user unable
to reach any workspace-scoped endpoint — it drops the orphan into the oldest
workspace. In multi-tenant mode "the oldest workspace" is just some tenant's
paid workspace: running this on every boot would silently re-add a user who
was deliberately removed from every workspace they belonged to. These tests
pin the gate: the backstop runs with `multi_tenant=False` (self-host, the
default) and never runs with `multi_tenant=True`.

`bootstrap()`'s own workspace/project/pack seeding (`create_workspace`,
`seed_workspace_content`) and `bootstrap_admin` are stubbed out — that
machinery is exercised elsewhere (test_provider_catalog.py,
test_context_composition.py); this file is only about the gate.

The "chat-harness backfill reaches every workspace" section near the bottom
is a separate concern living in the same file because it is also about
`bootstrap()`'s cross-workspace behavior: the oldest-workspace reseed above
only ever touches the one workspace `bootstrap()` picks at boot, so a
default added to `seed_chat_harness` after a tenant's workspace was created
needs its own path to reach that tenant. Those tests use a real sqlite
database (see `test_workspace_service.py`'s docstring for why — the query
shapes under test, pack links and a chat harness's own `tool_names`, are
exactly what a hand-rolled `FakeSession` like the one below gets subtly
wrong), so they get their own `engine`/`session_factory`/`db` fixtures
rather than reusing `FakeSession`.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.config import get_settings
from tret.db.models import Base, Harness, Pack, Project, User, Workspace
from tret.packs.links import packs_for_harness
from tret.services import bootstrap as bootstrap_module


class FakeSession:
    """Just enough of AsyncSession for `bootstrap()`'s steady-state branch
    (an existing Workspace + Project, so `create_workspace` is never called)
    plus `_ensure_every_user_has_a_membership`'s own query. Each query
    `bootstrap()` actually issues is handled by entity name directly, rather
    than a general where-clause interpreter — this file owns exactly which
    three queries the function under test can issue.
    """

    def __init__(self, *, workspace: Workspace, project, users=()):
        self.store: dict[str, list] = {
            "Workspace": [workspace],
            "Project": [project],
            "User": list(users),
            "WorkspaceMember": [],
        }

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        name = entity.__name__

        class _Result:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return list(self._rows)

            def first(self):
                return self._rows[0] if self._rows else None

        if name == "Workspace":
            return _Result(sorted(self.store["Workspace"], key=lambda w: w.created_at))
        if name == "Project":
            return _Result(list(self.store["Project"]))
        if name == "User":
            # `_ensure_every_user_has_a_membership`'s only query: users with
            # no row in WorkspaceMember at all.
            membered = {m.user_id for m in self.store["WorkspaceMember"]}
            return _Result([u for u in self.store["User"] if u.id not in membered])
        raise AssertionError(f"unexpected query against {name}")

    def add(self, obj):
        self.store.setdefault(type(obj).__name__, []).append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass


def _workspace(name: str, *, age_seconds: float = 0.0) -> Workspace:
    from datetime import timedelta

    return Workspace(
        id=uuid.uuid4(),
        name=name,
        kind="team",
        created_at=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


def _user(email: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email,
        role="analyst",
        disabled=False,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture(autouse=True)
def _stub_seeding(monkeypatch):
    """bootstrap()'s own workspace/pack seeding is out of scope for this
    file — stub it to no-ops so only the membership backstop is under test."""

    async def _noop_admin(db):
        return None

    async def _noop_seed(db, workspace_id, project_id):
        return None

    monkeypatch.setattr(bootstrap_module, "bootstrap_admin", _noop_admin)
    monkeypatch.setattr(bootstrap_module, "seed_workspace_content", _noop_seed)


async def test_multi_tenant_false_backstops_a_membership_less_user(monkeypatch):
    monkeypatch.setenv("TRET_MULTI_TENANT", "false")
    from tret.config import get_settings

    get_settings.cache_clear()
    try:
        oldest = _workspace("Oldest", age_seconds=100)
        from tret.db.models import Project

        proj = Project(id=uuid.uuid4(), workspace_id=oldest.id, name="Sample")
        orphan = _user("orphan@example.com")
        db = FakeSession(workspace=oldest, project=proj, users=[orphan])

        await bootstrap_module.bootstrap(db)

        memberships = [m for m in db.store["WorkspaceMember"] if m.user_id == orphan.id]
        assert len(memberships) == 1
        assert memberships[0].workspace_id == oldest.id
    finally:
        monkeypatch.delenv("TRET_MULTI_TENANT", raising=False)
        get_settings.cache_clear()


async def test_multi_tenant_true_leaves_a_membership_less_user_alone(monkeypatch):
    """The tenancy-leak case this finding fixes: a user deliberately removed
    from every workspace must not be silently re-added on the next boot."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "true")
    from tret.config import get_settings

    get_settings.cache_clear()
    try:
        oldest = _workspace("SomeTenant", age_seconds=100)
        from tret.db.models import Project

        proj = Project(id=uuid.uuid4(), workspace_id=oldest.id, name="Sample")
        removed = _user("removed@example.com")
        db = FakeSession(workspace=oldest, project=proj, users=[removed])

        await bootstrap_module.bootstrap(db)

        memberships = [m for m in db.store["WorkspaceMember"] if m.user_id == removed.id]
        assert memberships == []
    finally:
        monkeypatch.delenv("TRET_MULTI_TENANT", raising=False)
        get_settings.cache_clear()


# ── the chat-harness default backfill reaches every workspace, not just the
#    oldest one `bootstrap()`'s steady-state branch re-seeds ─────────────────
#
# Real sqlite database here, not `FakeSession` above — same reasoning as
# `test_workspace_service.py`'s docstring: linking a pack to a harness and
# reading its `tool_names` back are exactly the query shapes a hand-rolled
# fake session gets subtly wrong. `bootstrap_admin` stays stubbed by the
# autouse fixture above; `seed_workspace_content` is stubbed too, but that
# only no-ops the *oldest* workspace's own reseed — the loop under test here
# calls `seed_chat_harness` directly (a real, unstubbed function), which is
# exactly the path being pinned.


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


def _make_pack(workspace_id, *, slug: str = "pack-a") -> Pack:
    """A bare Pack row, not routed through `install_pack` — enough to
    exercise `seed_chat_harness`'s own pack-linking logic (it only reads the
    `packs` table), without needing a real pack directory on disk. Mirrors
    `test_workspace_service.py::make_pack`."""
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        slug=slug,
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={
            "pack": slug,
            "version": "1.0.0",
            "display_name": slug.title(),
            "task_types": [],
        },
        source_path=f"/tmp/{slug}",
    )


async def test_chat_harness_backfill_reaches_every_workspace_not_just_the_oldest(
    db, monkeypatch
):
    """The finding this section pins: `bootstrap()`'s steady-state branch
    only re-seeds the OLDEST workspace (the one it looks up at the top of
    the function). In a multi-tenant deployment, every later workspace's
    Chat Assistant — linked to its packs once, at `create_workspace` time,
    and never revisited by that branch again — would never receive a
    default added to `seed_chat_harness` after that workspace was created
    (a new builtin tool, or the default-pack-links behavior itself). Sets up
    a SECOND, newer workspace with a chat harness that has never had its
    packs defaulted (`packs_linked_at IS NULL`), an installed `Pack` with no
    `harness_packs` row yet, and `tool_names` missing "run_method" — after
    `bootstrap()`, both the pack link and the tool backfill must have
    reached it, exactly as if it were the workspace bootstrap() re-seeds
    directly."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "false")
    get_settings.cache_clear()

    now = datetime.now(timezone.utc)
    oldest = Workspace(id=uuid.uuid4(), name="Oldest", kind="team", created_at=now - timedelta(seconds=100))
    second = Workspace(id=uuid.uuid4(), name="Second", kind="team", created_at=now)
    db.add_all([oldest, second])
    await db.flush()
    db.add(Project(id=uuid.uuid4(), workspace_id=oldest.id, name="Sample"))

    pack = _make_pack(second.id)
    db.add(pack)
    chat_harness = Harness(
        workspace_id=second.id,
        name="Chat Assistant",
        task_profile="chat",
        model_policy={"mode": "auto"},
        tool_names=["run_harness_task"],  # "run_method" deliberately missing
        packs_linked_at=None,
    )
    db.add(chat_harness)
    await db.flush()
    assert await packs_for_harness(db, chat_harness) == []

    await bootstrap_module.bootstrap(db)

    linked = await packs_for_harness(db, chat_harness)
    assert [p.id for p in linked] == [pack.id]
    assert chat_harness.packs_linked_at is not None
    assert "run_method" in chat_harness.tool_names


async def test_chat_harness_backfill_failure_in_one_workspace_does_not_abort_boot(
    db, monkeypatch
):
    """Per-workspace failures in the backfill loop must not take the rest of
    boot down with them (mirrors `_install_configured_packs`'s own SAVEPOINT
    protection in `services/workspace.py`). Breaks `seed_chat_harness` for
    every call, then asserts `bootstrap()` still completes without raising."""
    monkeypatch.setenv("TRET_MULTI_TENANT", "false")
    get_settings.cache_clear()

    now = datetime.now(timezone.utc)
    oldest = Workspace(id=uuid.uuid4(), name="Oldest", kind="team", created_at=now - timedelta(seconds=100))
    second = Workspace(id=uuid.uuid4(), name="Second", kind="team", created_at=now)
    db.add_all([oldest, second])
    await db.flush()
    db.add(Project(id=uuid.uuid4(), workspace_id=oldest.id, name="Sample"))
    db.add(
        Harness(
            workspace_id=second.id,
            name="Chat Assistant",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
    )
    await db.flush()

    async def _boom(db, workspace_id):
        raise RuntimeError("simulated chat-harness backfill failure")

    monkeypatch.setattr(bootstrap_module, "seed_chat_harness", _boom)

    await bootstrap_module.bootstrap(db)  # must not raise
