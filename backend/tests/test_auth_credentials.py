"""Credential lifecycle: password change, admin rotation, deactivation — and the
session revocation all three depend on.

docs/hardening.md tells an operator to change the bootstrap admin's password and
to rotate credentials after an incident. Both instructions are only true if a
password change actually invalidates the sessions minted against the old one, so
that is what most of this file is about: tret has no session table, so
revocation rides on a credential fingerprint inside the signed cookie
(`auth.credential_version`), and the tests below check the fingerprint cannot be
bypassed, replayed, or outlived.

Real dependency chain, real argon2, real cookies; only the database is faked.
"""
from __future__ import annotations

import uuid

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from tret.api import auth
from tret.api.auth import SESSION_COOKIE, credential_version, login_limiter
from tret.db.engine import get_db
from tret.db.models import User, Workspace, WorkspaceMember

HASHER = PasswordHasher()
ADMIN_PASSWORD = "admin-password-1"
USER_PASSWORD = "user-password-1"


def _user(role: str, email: str, password: str | None, *, disabled: bool | None = None) -> User:
    # A fixture built with no password represents an already-deactivated
    # legacy account (this is exactly what the tenancy backfill migration
    # does for real rows: `disabled=true where password_hash IS NULL`)
    # unless a test overrides it explicitly.
    if disabled is None:
        disabled = password is None
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(password) if password else None,
        role=role,
        session_epoch=0,
        disabled=disabled,
    )


# ── fake database ────────────────────────────────────────────────────────────
def _matches(row, stmt) -> bool:
    """Evaluate a simple `where` (== / IS NOT NULL / IS NULL / IS TRUE/FALSE)
    against a row."""
    from sqlalchemy.sql.elements import False_, Null, True_

    where = stmt.whereclause
    if where is None:
        return True
    for clause in getattr(where, "clauses", [where]):
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        if left is None:
            continue
        actual = getattr(row, left.key, None)
        operator = getattr(clause.operator, "__name__", "")
        if isinstance(right, True_):
            if actual is not True:
                return False
        elif isinstance(right, False_):
            if actual is not False:
                return False
        elif isinstance(right, Null) or operator in ("is_", "is_not"):
            is_null = actual is None
            if operator == "is_not" and is_null:
                return False
            if operator == "is_" and not is_null:
                return False
        elif getattr(right, "value", None) is not None:
            if actual != right.value:
                return False
    return True


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]


class FakeSession:
    """Users, plus (mostly empty by default) workspaces and memberships —
    every test in this file that does not care about workspaces gets exactly
    one implicit membership per user (see `people`/`db` below), so the
    sole-membership fallback resolves the same way self-host always has.
    """

    def __init__(self, users, *, workspaces=(), members=()):
        self.store: dict[str, list] = {
            "User": list(users),
            "Workspace": list(workspaces),
            "WorkspaceMember": list(members),
        }
        self.commits = 0

    async def get(self, model, ident, **_kw):
        rows = self.store.get(model.__name__, [])
        if model is WorkspaceMember:
            user_id, workspace_id = ident
            return next(
                (m for m in rows if m.user_id == user_id and m.workspace_id == workspace_id), None
            )
        return next((r for r in rows if r.id == ident), None)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        # `select(func.count()).select_from(User)` (the admin-count query) has
        # no mapped column, so column_descriptions carries no entity — the
        # only shape this fake ever sees that way.
        entity_name = entity.__name__ if entity is not None else "User"
        rows = self.store.get(entity_name, [])
        matching = [r for r in rows if _matches(r, stmt)]
        if "count(" in str(stmt).lower():
            return FakeResult([len(matching)])
        return FakeResult(matching)

    def add(self, obj):
        self.store.setdefault(type(obj).__name__, []).append(obj)

    async def commit(self):
        self.commits += 1


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def people() -> dict[str, User]:
    return {
        "admin": _user("admin", "admin@example.com", ADMIN_PASSWORD),
        "admin2": _user("admin", "second-admin@example.com", ADMIN_PASSWORD),
        "analyst": _user("analyst", "analyst@example.com", USER_PASSWORD),
        "dormant": _user("analyst", "dormant@example.com", None),
    }


@pytest.fixture
def db(people) -> FakeSession:
    return FakeSession(people.values())


@pytest.fixture
def client(db) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        yield c
    login_limiter.reset()


def login(client: TestClient, user: User, password: str):
    client.cookies.clear()
    return client.post("/api/auth/login", json={"email": user.email, "password": password})


def cookie_of(client: TestClient) -> str:
    return client.cookies[SESSION_COOKIE]


def me_with(client: TestClient, token: str):
    client.cookies.clear()
    client.cookies.set(SESSION_COOKIE, token)
    return client.get("/api/auth/me")


# ── the session is bound to the credential ───────────────────────────────────
def test_a_session_cookie_is_bound_to_the_password_it_was_minted_against(client, people):
    analyst = people["analyst"]
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    stale = cookie_of(client)
    assert me_with(client, stale).status_code == 200

    analyst.password_hash = HASHER.hash("a-completely-new-password")  # rotated elsewhere
    response = me_with(client, stale)
    assert response.status_code == 401
    assert "password was changed" in response.json()["detail"]


def test_a_legacy_cookie_carrying_only_a_user_id_is_refused(client, people):
    """The old cookie shape cannot be honoured: it predates revocation.

    Accepting it would be a bypass — a cookie with no credential fingerprint can
    never be invalidated by a password change.
    """
    legacy = URLSafeTimedSerializer(auth.get_settings().secret_key, salt="tret-session").dumps(
        str(people["analyst"].id)
    )
    assert me_with(client, legacy).status_code == 401


def test_a_cookie_with_a_guessed_fingerprint_is_refused(client, people):
    forged = URLSafeTimedSerializer(auth.get_settings().secret_key, salt="tret-session").dumps(
        {"uid": str(people["analyst"].id), "cv": "0" * 16}
    )
    assert me_with(client, forged).status_code == 401


def test_the_fingerprint_is_not_the_password_hash(client, people):
    """Only a truncated digest travels in the cookie."""
    analyst = people["analyst"]
    version = credential_version(analyst)
    assert len(version) == 16
    assert version not in analyst.password_hash
    assert analyst.password_hash not in version


def test_the_fingerprint_changes_even_when_the_password_does_not(people):
    """argon2 salts every hash, so re-setting the same password still revokes."""
    analyst = people["analyst"]
    before = credential_version(analyst)
    analyst.password_hash = HASHER.hash(USER_PASSWORD)
    assert credential_version(analyst) != before


# ── self-service password change ─────────────────────────────────────────────
def test_changing_your_password_invalidates_your_other_sessions(client, db, people):
    analyst = people["analyst"]
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    other_device = cookie_of(client)

    assert login(client, analyst, USER_PASSWORD).status_code == 200
    response = client.post(
        "/api/auth/password",
        json={"current_password": USER_PASSWORD, "new_password": "brand-new-password"},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "sessions_invalidated": True}
    assert db.commits == 1

    # The session that made the change survives (it was re-issued)...
    assert client.get("/api/auth/me").status_code == 200
    # ...and the one on the other device does not.
    assert me_with(client, other_device).status_code == 401


def test_the_new_password_is_the_one_that_works_afterwards(client, people):
    analyst = people["analyst"]
    login(client, analyst, USER_PASSWORD)
    client.post(
        "/api/auth/password",
        json={"current_password": USER_PASSWORD, "new_password": "brand-new-password"},
    )
    assert login(client, analyst, USER_PASSWORD).status_code == 401
    assert login(client, analyst, "brand-new-password").status_code == 200


def test_the_current_password_is_required(client, db, people):
    analyst = people["analyst"]
    original = analyst.password_hash
    login(client, analyst, USER_PASSWORD)
    response = client.post(
        "/api/auth/password",
        json={"current_password": "not-the-password", "new_password": "brand-new-password"},
    )
    assert response.status_code == 403
    assert analyst.password_hash == original
    assert db.commits == 0


@pytest.mark.parametrize("weak", ["", "short", "1234567"])
def test_a_new_password_must_clear_the_length_floor(client, db, people, weak):
    analyst = people["analyst"]
    original = analyst.password_hash
    login(client, analyst, USER_PASSWORD)
    response = client.post(
        "/api/auth/password", json={"current_password": USER_PASSWORD, "new_password": weak}
    )
    assert response.status_code == 422
    assert analyst.password_hash == original
    assert db.commits == 0


def test_a_password_cannot_be_changed_to_itself(client, db, people):
    """Otherwise "rotate the credential" could be satisfied without rotating it."""
    login(client, people["analyst"], USER_PASSWORD)
    response = client.post(
        "/api/auth/password",
        json={"current_password": USER_PASSWORD, "new_password": USER_PASSWORD},
    )
    assert response.status_code == 422
    assert db.commits == 0


def test_an_anonymous_request_cannot_change_a_password(client, db):
    response = client.post(
        "/api/auth/password",
        json={"current_password": USER_PASSWORD, "new_password": "brand-new-password"},
    )
    assert response.status_code == 401
    assert db.commits == 0


def test_the_current_password_check_is_rate_limited(client, people, monkeypatch):
    """A stolen cookie must not become an oracle for the account's password."""
    monkeypatch.setattr(auth.get_settings(), "login_max_attempts", 3)
    login(client, people["analyst"], USER_PASSWORD)
    for _ in range(3):
        response = client.post(
            "/api/auth/password",
            json={"current_password": "wrong", "new_password": "brand-new-password"},
        )
        assert response.status_code == 403
    blocked = client.post(
        "/api/auth/password",
        json={"current_password": "wrong", "new_password": "brand-new-password"},
    )
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


# ── admin rotation ───────────────────────────────────────────────────────────
def test_an_admin_can_rotate_another_users_password(client, db, people):
    analyst = people["analyst"]
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    analyst_session = cookie_of(client)

    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(
        f"/api/auth/users/{analyst.id}/password", json={"new_password": "rotated-by-admin"}
    )
    assert response.status_code == 200
    assert response.json()["active"] is True

    assert me_with(client, analyst_session).status_code == 401  # revoked
    assert login(client, analyst, "rotated-by-admin").status_code == 200


def test_a_non_admin_cannot_rotate_anyone(client, db, people):
    analyst = people["analyst"]
    original = analyst.password_hash
    login(client, analyst, USER_PASSWORD)
    for target in (analyst.id, people["admin"].id):
        response = client.post(
            f"/api/auth/users/{target}/password", json={"new_password": "rotated-by-analyst"}
        )
        assert response.status_code == 403
    assert analyst.password_hash == original
    assert db.commits == 0


def test_rotating_an_unknown_user_is_a_404(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(
        f"/api/auth/users/{uuid.uuid4()}/password", json={"new_password": "rotated-by-admin"}
    )
    assert response.status_code == 404
    assert db.commits == 0


def test_a_rotated_password_must_also_clear_the_length_floor(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(
        f"/api/auth/users/{people['analyst'].id}/password", json={"new_password": "short"}
    )
    assert response.status_code == 422
    assert db.commits == 0


# ── deactivation ─────────────────────────────────────────────────────────────
def test_deactivating_a_user_ends_their_sessions_and_their_logins(client, db, people):
    analyst = people["analyst"]
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    analyst_session = cookie_of(client)

    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(f"/api/auth/users/{analyst.id}/deactivate")
    assert response.status_code == 200
    assert response.json()["active"] is False

    assert me_with(client, analyst_session).status_code == 401
    assert login(client, analyst, USER_PASSWORD).status_code == 401
    # The credential itself is left alone — `disabled` is the signal now, not
    # a cleared hash. `POST /users/{id}/password` is what restores access.
    assert analyst.password_hash is not None
    assert analyst.disabled is True


def test_a_deactivated_user_is_kept_not_deleted(client, db, people):
    """Approvals name a human; the row that human is has to stay."""
    analyst = people["analyst"]
    login(client, people["admin"], ADMIN_PASSWORD)
    client.post(f"/api/auth/users/{analyst.id}/deactivate")
    listed = client.get("/api/auth/users").json()
    entry = next(u for u in listed if u["id"] == str(analyst.id))
    assert entry["active"] is False
    assert entry["display_name"] == analyst.display_name


def test_deactivation_is_idempotent(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    first = client.post(f"/api/auth/users/{people['dormant'].id}/deactivate")
    assert first.status_code == 200
    assert first.json()["active"] is False
    assert db.commits == 0  # nothing to write


def test_a_deactivated_user_can_be_restored_by_setting_a_password(client, people):
    dormant = people["dormant"]
    assert login(client, dormant, USER_PASSWORD).status_code == 401
    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(
        f"/api/auth/users/{dormant.id}/password", json={"new_password": "welcome-back-1"}
    )
    assert response.json()["active"] is True
    assert login(client, dormant, "welcome-back-1").status_code == 200


def test_a_non_admin_cannot_deactivate_anyone(client, db, people):
    login(client, people["analyst"], USER_PASSWORD)
    response = client.post(f"/api/auth/users/{people['admin'].id}/deactivate")
    assert response.status_code == 403
    assert people["admin"].password_hash is not None
    assert db.commits == 0


def test_an_admin_cannot_deactivate_themselves(client, db, people):
    admin = people["admin"]
    login(client, admin, ADMIN_PASSWORD)
    response = client.post(f"/api/auth/users/{admin.id}/deactivate")
    assert response.status_code == 422
    assert admin.password_hash is not None
    assert db.commits == 0


def test_the_last_active_admin_cannot_be_deactivated(client, db, people):
    """Otherwise an operator can lock the deployment out of its own admin surface."""
    admin, second = people["admin"], people["admin2"]
    login(client, admin, ADMIN_PASSWORD)

    # Two admins: deactivating one is allowed.
    assert client.post(f"/api/auth/users/{second.id}/deactivate").status_code == 200
    assert second.disabled is True

    # One left, and an admin cannot deactivate themselves either, so there is no
    # sequence of calls that leaves zero admins.
    login(client, admin, ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{admin.id}/deactivate").status_code == 422
    assert admin.disabled is False


def test_deactivating_an_unknown_user_is_a_404(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{uuid.uuid4()}/deactivate").status_code == 404
    assert db.commits == 0


# ── reactivation ─────────────────────────────────────────────────────────────
def test_reactivating_a_user_clears_disabled_and_restores_login(client, db, people):
    analyst = people["analyst"]
    login(client, people["admin"], ADMIN_PASSWORD)
    client.post(f"/api/auth/users/{analyst.id}/deactivate")
    assert analyst.disabled is True
    assert login(client, analyst, USER_PASSWORD).status_code == 401

    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(f"/api/auth/users/{analyst.id}/reactivate")
    assert response.status_code == 200
    assert response.json()["active"] is True
    assert analyst.disabled is False
    # The password itself was never touched by either call.
    assert login(client, analyst, USER_PASSWORD).status_code == 200


def test_a_non_admin_cannot_reactivate_anyone(client, db, people):
    admin2 = people["admin2"]
    login(client, people["admin"], ADMIN_PASSWORD)
    client.post(f"/api/auth/users/{admin2.id}/deactivate")

    login(client, people["analyst"], USER_PASSWORD)
    response = client.post(f"/api/auth/users/{admin2.id}/reactivate")
    assert response.status_code == 403
    assert admin2.disabled is True


def test_reactivating_an_unknown_user_is_a_404(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{uuid.uuid4()}/reactivate").status_code == 404


def test_reactivation_is_not_gated_by_oidc_auth_mode(client, db, people, monkeypatch):
    """The finding this fixes: `admin_set_password` (the only other lever that
    clears `disabled`) is password-gated and 403s under `auth_mode=oidc`,
    which made deactivation irreversible on an OIDC-only deployment.
    Reactivation touches neither `password_hash` nor `session_epoch`, so it
    must stay available regardless of `auth_mode`."""
    analyst = people["analyst"]
    login(client, people["admin"], ADMIN_PASSWORD)
    client.post(f"/api/auth/users/{analyst.id}/deactivate")

    monkeypatch.setenv("TRET_AUTH_MODE", "oidc")
    auth.get_settings.cache_clear()
    try:
        # Confirm the gate this endpoint is deliberately NOT behind actually
        # would have blocked a password-mode endpoint here.
        blocked = client.post(
            f"/api/auth/users/{analyst.id}/password", json={"new_password": "irrelevant-1"}
        )
        assert blocked.status_code == 403

        response = client.post(f"/api/auth/users/{analyst.id}/reactivate")
        assert response.status_code == 200
        assert response.json()["active"] is True
        assert analyst.disabled is False
    finally:
        monkeypatch.delenv("TRET_AUTH_MODE", raising=False)
        auth.get_settings.cache_clear()


# ── what /me and the user list report ────────────────────────────────────────
def test_me_reports_the_account_state(client, people):
    login(client, people["analyst"], USER_PASSWORD)
    body = client.get("/api/auth/me").json()
    assert body["email"] == people["analyst"].email
    assert body["role"] == "analyst"
    assert body["active"] is True


def test_login_never_reveals_whether_an_account_is_deactivated(client, people):
    """Deactivated, wrong password and no such user are one answer."""
    deactivated = login(client, people["dormant"], USER_PASSWORD)
    wrong = login(client, people["analyst"], "not-the-password")
    absent = client.post(
        "/api/auth/login", json={"email": "nobody@example.com", "password": USER_PASSWORD}
    )
    assert {r.status_code for r in (deactivated, wrong, absent)} == {401}
    assert len({r.json()["detail"] for r in (deactivated, wrong, absent)}) == 1


# ── session-epoch revocation: sign out everywhere, without touching the
# password or deactivating the account ───────────────────────────────────────
def test_revoking_sessions_ends_every_outstanding_cookie(client, db, people):
    analyst = people["analyst"]
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    stale = cookie_of(client)
    assert me_with(client, stale).status_code == 200

    login(client, people["admin"], ADMIN_PASSWORD)
    response = client.post(f"/api/auth/users/{analyst.id}/revoke-sessions")
    assert response.status_code == 200
    assert analyst.session_epoch == 1

    # The session minted before the epoch bump is gone...
    assert me_with(client, stale).status_code == 401
    # ...the password itself was never touched, so a fresh login still works...
    assert login(client, analyst, USER_PASSWORD).status_code == 200
    # ...and the account was never deactivated.
    assert analyst.disabled is False


def test_revoking_sessions_does_not_touch_the_password_hash(client, db, people):
    analyst = people["analyst"]
    original_hash = analyst.password_hash
    login(client, people["admin"], ADMIN_PASSWORD)
    client.post(f"/api/auth/users/{analyst.id}/revoke-sessions")
    assert analyst.password_hash == original_hash


def test_revoking_sessions_twice_keeps_advancing_the_epoch(client, db, people):
    """Each call is its own fresh boundary — a cookie minted between two
    revocations is still cut off by the second."""
    analyst = people["analyst"]
    login(client, people["admin"], ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{analyst.id}/revoke-sessions").status_code == 200
    assert analyst.session_epoch == 1

    login(client, analyst, USER_PASSWORD)
    mid_session = cookie_of(client)
    assert me_with(client, mid_session).status_code == 200

    login(client, people["admin"], ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{analyst.id}/revoke-sessions").status_code == 200
    assert analyst.session_epoch == 2
    assert me_with(client, mid_session).status_code == 401


def test_a_non_admin_cannot_revoke_anyones_sessions(client, db, people):
    login(client, people["analyst"], USER_PASSWORD)
    response = client.post(f"/api/auth/users/{people['admin'].id}/revoke-sessions")
    assert response.status_code == 403
    assert people["admin"].session_epoch in (0, None)


def test_revoking_sessions_for_an_unknown_user_is_a_404(client, db, people):
    login(client, people["admin"], ADMIN_PASSWORD)
    assert client.post(f"/api/auth/users/{uuid.uuid4()}/revoke-sessions").status_code == 404


# ── workspace switching ──────────────────────────────────────────────────────
def _two_workspace_user() -> tuple[User, Workspace, Workspace]:
    user = _user("admin", "multi@example.com", ADMIN_PASSWORD)
    ws_a = Workspace(id=uuid.uuid4(), name="A", kind="team")
    ws_b = Workspace(id=uuid.uuid4(), name="B", kind="team")
    return user, ws_a, ws_b


def test_switching_workspace_re_mints_the_cookies_wid(client, db):
    user, ws_a, ws_b = _two_workspace_user()
    db.store["User"].append(user)
    db.store["Workspace"].extend([ws_a, ws_b])
    db.store["WorkspaceMember"].extend(
        [
            WorkspaceMember(user_id=user.id, workspace_id=ws_a.id, role="owner"),
            WorkspaceMember(user_id=user.id, workspace_id=ws_b.id, role="admin"),
        ]
    )
    login(client, user, ADMIN_PASSWORD)
    # Two memberships: /me cannot pick one on its own.
    before = client.get("/api/auth/me").json()
    assert before["current_workspace_id"] is None
    assert {w["id"] for w in before["workspaces"]} == {str(ws_a.id), str(ws_b.id)}

    response = client.post("/api/auth/workspace", json={"workspace_id": str(ws_b.id)})
    assert response.status_code == 200
    assert response.json()["current_workspace_id"] == str(ws_b.id)
    assert response.json()["role"] == "admin"  # ws_b's membership role, not the global one

    # The re-minted cookie carries the choice forward on the next request too.
    after = client.get("/api/auth/me").json()
    assert after["current_workspace_id"] == str(ws_b.id)


def test_switching_to_a_workspace_you_do_not_belong_to_is_refused(client, db):
    user, ws_a, _ws_b = _two_workspace_user()
    db.store["User"].append(user)
    db.store["Workspace"].append(ws_a)
    db.store["WorkspaceMember"].append(
        WorkspaceMember(user_id=user.id, workspace_id=ws_a.id, role="owner")
    )
    login(client, user, ADMIN_PASSWORD)
    foreign = uuid.uuid4()
    response = client.post("/api/auth/workspace", json={"workspace_id": str(foreign)})
    assert response.status_code == 404


def test_login_mints_the_wid_of_a_sole_membership(client, db):
    """The self-host path: one membership, so login never needs a switch."""
    user = _user("admin", "solo@example.com", ADMIN_PASSWORD)
    workspace = Workspace(id=uuid.uuid4(), name="Solo", kind="team")
    db.store["User"].append(user)
    db.store["Workspace"].append(workspace)
    db.store["WorkspaceMember"].append(
        WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role="owner")
    )
    body = login(client, user, ADMIN_PASSWORD).json()
    assert body["current_workspace_id"] == str(workspace.id)
    assert body["role"] == "owner"
