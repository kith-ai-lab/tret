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
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from tret.db.models import User, Workspace
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

    async def _noop_seed(db, workspace_id, project_id, *, seed_demo_content):
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
