"""The blessing gate: `POST /api/findings/{id}/approval`.

This is tret's headline trust guarantee, so it is tested through the real
dependency chain rather than around it: a real signed session cookie, the real
`current_user`/`require_approver` dependencies, and the real Pydantic body. Only
the database is a fake (`FakeSession`), because the endpoint's promises are about
authority and identity, not about SQL.

What each group of tests pins down:

* **the approver is the session, not the payload** — the API has no approver
  field and cannot be talked into one, so the name stamped on an approval is
  whoever's cookie made the request;
* **role gating** — an analyst cannot bless anything, and a failed attempt
  leaves no trace;
* **a decision happens once** — an already-decided finding cannot be re-decided,
  and its recorded approver can never be replaced;
* **nothing is written on any rejected path** — 401/403/404/409/422 all leave the
  finding and the approvals table exactly as they were.

The last point is why almost every test asserts on `db.added` and `db.commits`:
"the request failed" and "the request failed without recording anything" are
different guarantees, and only the second one is worth having here.
"""
from __future__ import annotations

import uuid

import pytest
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from tret.api import auth, findings
from tret.api.auth import SESSION_COOKIE, login_limiter
from tret.db.engine import get_db
from tret.db.models import Approval, Finding, Project, User

PASSWORD = "correct-horse-battery"
_HASH = PasswordHasher().hash(PASSWORD)  # once: argon2 is deliberately slow


# ── fake database ────────────────────────────────────────────────────────────
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
        assert len(self._rows) <= 1, f"expected at most one row, got {len(self._rows)}"
        return self._rows[0] if self._rows else None


def _criteria(stmt) -> list[tuple[str, object]]:
    """(column, value) pairs from a simple `where a == :x [and b == :y]`."""
    where = stmt.whereclause
    if where is None:
        return []
    clauses = getattr(where, "clauses", [where])
    out = []
    for clause in clauses:
        left, right = getattr(clause, "left", None), getattr(clause, "right", None)
        if left is not None and hasattr(right, "value"):
            out.append((left.key, right.value))
    return out


class FakeSession:
    """Enough AsyncSession for these endpoints, and a record of every write."""

    def __init__(self, *, users=(), findings_=(), approvals=(), projects=()):
        self.store: dict[str, dict] = {
            "User": {u.id: u for u in users},
            "Finding": {f.id: f for f in findings_},
            "Approval": {a.id: a for a in approvals},
            "Project": {p.id: p for p in projects},
        }
        self.added: list = []
        self.commits = 0
        self.locked_for_update: list[tuple[str, uuid.UUID]] = []

    async def get(self, model, ident, *, with_for_update=False, **_kw):
        if with_for_update:
            self.locked_for_update.append((model.__name__, ident))
        return self.store.get(model.__name__, {}).get(ident)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        rows = list(self.store.get(entity.__name__, {}).values())
        for key, value in _criteria(stmt):
            rows = [r for r in rows if getattr(r, key, None) == value]
        return FakeResult(rows)

    def add(self, obj):
        self.added.append(obj)
        self.store.setdefault(type(obj).__name__, {})[getattr(obj, "id", uuid.uuid4())] = obj

    async def commit(self):
        self.commits += 1

    # Assertion helper: no approval row, no commit, nothing at all.
    def wrote_nothing(self) -> bool:
        return not self.added and self.commits == 0


# ── fixtures ─────────────────────────────────────────────────────────────────
def _user(role: str, email: str, name: str) -> User:
    return User(id=uuid.uuid4(), email=email, display_name=name, password_hash=_HASH, role=role)


def _finding(status: str = "draft", **over) -> Finding:
    base = dict(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        schema_slug="draft_section",
        subject={"deliverable": "tcfd-report", "section": "governance"},
        payload={"markdown": "# Governance\n\nSome drafted prose."},
        provenance={"model": "anthropic/test", "doctrine_sha": "abc123"},
        status=status,
    )
    base.update(over)
    return Finding(**base)


@pytest.fixture
def people() -> dict[str, User]:
    return {
        "admin": _user("admin", "admin@example.com", "Ada Admin"),
        "approver": _user("approver", "approver@example.com", "Rex Reviewer"),
        "analyst": _user("analyst", "analyst@example.com", "Ann Analyst"),
    }


@pytest.fixture
def draft() -> Finding:
    return _finding()


@pytest.fixture
def db(people, draft) -> FakeSession:
    return FakeSession(
        users=people.values(),
        findings_=[draft],
        projects=[Project(id=draft.project_id, workspace_id=uuid.uuid4(), name="P")],
    )


@pytest.fixture
def client(db) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        yield c
    login_limiter.reset()


def login_as(client: TestClient, user: User) -> None:
    """A real login: the cookie under test is one the login endpoint minted."""
    client.cookies.clear()
    response = client.post(
        "/api/auth/login", json={"email": user.email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text


def decide(client: TestClient, _finding_id, **body):
    return client.post(f"/api/findings/{_finding_id}/approval", json=body)


# ── authentication: no session, no blessing ──────────────────────────────────
def test_an_anonymous_request_cannot_approve(client, db, draft):
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 401
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_a_tampered_cookie_cannot_approve(client, db, draft, people):
    login_as(client, people["approver"])
    good = client.cookies[SESSION_COOKIE]
    client.cookies.set(SESSION_COOKIE, good[:-4] + ("aaaa" if not good.endswith("aaaa") else "bbbb"))
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 401
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_a_cookie_signed_with_another_secret_cannot_approve(client, db, draft, people):
    """The forgery an attacker who knows the user id but not the key would try."""
    forged = URLSafeTimedSerializer("not-the-app-secret", salt="tret-session").dumps(
        str(people["approver"].id)
    )
    client.cookies.set(SESSION_COOKIE, forged)
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 401
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_a_validly_signed_session_for_an_unknown_user_cannot_approve(client, db, draft):
    """Deleting the user must retire the cookie: the id is resolved every request."""
    ghost = URLSafeTimedSerializer(auth.get_settings().secret_key, salt="tret-session").dumps(
        str(uuid.uuid4())
    )
    client.cookies.set(SESSION_COOKIE, ghost)
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 401
    assert draft.status == "draft"
    assert db.wrote_nothing()


# ── role gating ──────────────────────────────────────────────────────────────
def test_an_analyst_cannot_approve(client, db, draft, people):
    login_as(client, people["analyst"])
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 403
    assert "Approver role required" in response.json()["detail"]
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_an_analyst_cannot_reject_either(client, db, draft, people):
    """Rejection is also a recorded human judgment, not a lesser action."""
    login_as(client, people["analyst"])
    assert decide(client, draft.id, action="reject").status_code == 403
    assert draft.status == "draft"
    assert db.wrote_nothing()


@pytest.mark.parametrize("role", ["approver", "admin"])
def test_approvers_and_admins_can_approve(client, db, draft, people, role):
    login_as(client, people[role])
    response = decide(client, draft.id, action="approve")
    assert response.status_code == 200
    assert draft.status == "approved"
    assert response.json() == {
        "ok": True,
        "status": "approved",
        "approver": people[role].display_name,
    }


def test_role_is_read_from_the_database_not_the_cookie(client, db, draft, people):
    """A demotion takes effect on the next request, not when the cookie expires."""
    approver = people["approver"]
    login_as(client, approver)
    approver.role = "analyst"  # demoted mid-session
    assert decide(client, draft.id, action="approve").status_code == 403
    assert draft.status == "draft"
    assert db.wrote_nothing()


# ── the approver is the session, never the request body ──────────────────────
def test_the_stamped_approver_is_the_logged_in_user(client, db, draft, people):
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve", note="checked the numbers").status_code == 200

    approvals = [a for a in db.added if isinstance(a, Approval)]
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval.approver_id == people["approver"].id
    assert approval.finding_id == draft.id
    assert approval.action == "approve"
    assert approval.note == "checked the numbers"
    assert db.commits == 1


@pytest.mark.parametrize(
    "field", ["approver_id", "approver", "user_id", "approved_by", "status", "finding_id"]
)
def test_the_body_cannot_claim_an_approver_or_a_status(client, db, draft, people, field):
    """Every field an attacker might use to sign someone else's name is refused.

    Not ignored — refused: the response names the offending field, so a client
    that thinks it is choosing the approver is told it is not, rather than
    watching its claim vanish into a 200 that looks like agreement.
    """
    login_as(client, people["approver"])
    victim = people["admin"]
    response = decide(client, draft.id, action="approve", **{field: str(victim.id)})
    assert response.status_code == 422
    assert field in response.text
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_two_reviewers_each_stamp_their_own_name(client, db, people):
    """The identity travels with the cookie, not with anything the client sends."""
    first, second = _finding(), _finding()
    db.store["Finding"].update({first.id: first, second.id: second})

    login_as(client, people["approver"])
    assert decide(client, first.id, action="approve").json()["approver"] == "Rex Reviewer"
    login_as(client, people["admin"])
    assert decide(client, second.id, action="approve").json()["approver"] == "Ada Admin"

    stamped = {a.finding_id: a.approver_id for a in db.added if isinstance(a, Approval)}
    assert stamped == {first.id: people["approver"].id, second.id: people["admin"].id}


# ── a decision happens once ──────────────────────────────────────────────────
def test_an_approved_finding_cannot_be_approved_again(client, db, draft, people):
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve").status_code == 200
    writes_after_first = len(db.added)

    response = decide(client, draft.id, action="approve")
    assert response.status_code == 409
    assert "already 'approved'" in response.json()["detail"]
    assert len(db.added) == writes_after_first  # no second approvals row


def test_a_second_reviewer_cannot_replace_the_recorded_approver(client, db, draft, people):
    """The audit trail names one human. Nobody can overwrite that name."""
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve").status_code == 200
    first = [a for a in db.added if isinstance(a, Approval)][0]

    login_as(client, people["admin"])
    assert decide(client, draft.id, action="approve").status_code == 409
    assert decide(client, draft.id, action="reject").status_code == 409

    assert [a for a in db.added if isinstance(a, Approval)] == [first]
    assert first.approver_id == people["approver"].id
    assert draft.status == "approved"


def test_a_rejected_finding_cannot_be_flipped_to_approved(client, db, draft, people):
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="reject").status_code == 200
    assert draft.status == "rejected"

    response = decide(client, draft.id, action="approve")
    assert response.status_code == 409
    assert draft.status == "rejected"


@pytest.mark.parametrize("existing", ["approved", "rejected", "superseded"])
def test_only_a_draft_can_be_decided(client, db, people, existing):
    """Anything that is not a draft is already decided, whatever it is called."""
    settled = _finding(status=existing)
    db.store["Finding"][settled.id] = settled
    login_as(client, people["approver"])
    assert decide(client, settled.id, action="approve").status_code == 409
    assert settled.status == existing
    assert not [a for a in db.added if isinstance(a, Approval)]


def test_the_finding_row_is_locked_while_it_is_decided(client, db, draft, people):
    """Pins the fix for the concurrent-decision race.

    Without `SELECT ... FOR UPDATE`, two approvers deciding the same draft in the
    same instant both read 'draft', both write an approvals row, and the later
    commit silently decides the outcome — a reject can be overwritten by an
    approve with no 409 raised anywhere. This asserts the lock is taken, which is
    the only part of that reachable without two live Postgres transactions.
    """
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve").status_code == 200
    assert ("Finding", draft.id) in db.locked_for_update


# ── the action itself ────────────────────────────────────────────────────────
def test_reject_records_a_rejection(client, db, draft, people):
    login_as(client, people["approver"])
    response = decide(client, draft.id, action="reject", note="numbers do not tie out")
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert draft.status == "rejected"
    approval = [a for a in db.added if isinstance(a, Approval)][0]
    assert approval.action == "reject"
    assert approval.approver_id == people["approver"].id


@pytest.mark.parametrize("action", ["", "Approve", "APPROVE", "unapprove", "approve ", "publish"])
def test_an_unknown_action_decides_nothing(client, db, draft, people, action):
    login_as(client, people["approver"])
    response = decide(client, draft.id, action=action)
    assert response.status_code == 422
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_a_missing_action_decides_nothing(client, db, draft, people):
    login_as(client, people["approver"])
    assert client.post(f"/api/findings/{draft.id}/approval", json={}).status_code == 422
    assert draft.status == "draft"
    assert db.wrote_nothing()


def test_the_note_is_optional(client, db, draft, people):
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve", note=None).status_code == 200
    assert [a for a in db.added if isinstance(a, Approval)][0].note is None


def test_approval_does_not_touch_the_finding_content(client, db, draft, people):
    """Blessing an output records a decision about it; it never edits it."""
    payload, provenance, subject = dict(draft.payload), dict(draft.provenance), dict(draft.subject)
    login_as(client, people["approver"])
    assert decide(client, draft.id, action="approve").status_code == 200
    assert draft.payload == payload
    assert draft.provenance == provenance
    assert draft.subject == subject


# ── the finding has to exist ─────────────────────────────────────────────────
def test_an_unknown_finding_cannot_be_approved(client, db, people):
    login_as(client, people["approver"])
    response = decide(client, uuid.uuid4(), action="approve")
    assert response.status_code == 404
    assert db.wrote_nothing()


def test_a_malformed_finding_id_is_refused(client, db, people):
    login_as(client, people["approver"])
    assert decide(client, "not-a-uuid", action="approve").status_code == 422
    assert db.wrote_nothing()


def test_authorization_is_checked_before_existence(client, db, people):
    """An analyst probing for finding ids learns nothing from the status code."""
    login_as(client, people["analyst"])
    assert decide(client, uuid.uuid4(), action="approve").status_code == 403


# ── reading a decided finding back ───────────────────────────────────────────
def test_the_detail_view_reports_the_recorded_approver(client, db, draft, people):
    approval = Approval(
        id=uuid.uuid4(),
        finding_id=draft.id,
        action="approve",
        approver_id=people["approver"].id,
        note="ok",
    )
    db.store["Approval"][approval.id] = approval
    draft.status = "approved"

    login_as(client, people["analyst"])  # reading is not gated on the approver role
    body = client.get(f"/api/findings/{draft.id}").json()
    assert body["status"] == "approved"
    assert body["approvals"] == [
        {
            "action": "approve",
            "approver_id": str(people["approver"].id),
            "note": "ok",
            "created_at": None,
        }
    ]


def test_an_anonymous_request_cannot_read_findings(client, draft):
    assert client.get(f"/api/findings/{draft.id}").status_code == 401
    assert client.get("/api/findings").status_code == 401
