"""Write-back to SharePoint: the human-facing half.

`propose_connected_write` (engine/tools.py, tests/test_connected_write.py)
never touches Microsoft Graph — it only records a `connected_write` Finding.
This file covers everything that actually uploads:

* `POST /api/findings/{id}/approval` — approving a `connected_write` finding
  triggers the upload as a side effect, recording `payload["upload"]` either
  way and never failing the approval itself;
* `POST /api/findings/{id}/upload-retry` — re-runs that upload, when it
  previously failed *or* was never observed at all (payload["upload"] still
  None/missing — the process died between the approval commit and the
  upload's own commit);
* `POST /api/deliverables/{slug}/publish` — the direct human route, which
  creates no Finding at all and is blessed by the approver-role requirement
  on the endpoint itself.

Extends the shape of `test_approval_gate.py` (a real signed session cookie,
the real `current_user`/`require_workspace_approver` dependencies, a fake
`AsyncSession`) rather than duplicating its whole suite — this file is about
what approving a `connected_write` finding *does*, not about the blessing
gate's own authorization rules, which are already covered there.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

import pytest
import tret.services.export as export_module
from argon2 import PasswordHasher
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tret.api import auth, findings as findings_api
from tret.api.auth import login_limiter
from tret.db.engine import get_db
from tret.db.models import Finding, Project, User, Workspace, WorkspaceMember
from tret.services import connections as connections_service

PASSWORD = "correct-horse-battery"
_HASH = PasswordHasher().hash(PASSWORD)  # once: argon2 is deliberately slow


# ── fake database (same shape as test_approval_gate.py's) ───────────────────
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
    def __init__(self, *, users=(), findings_=(), projects=(), workspaces=(), members=()):
        self.store: dict[str, dict] = {
            "User": {u.id: u for u in users},
            "Finding": {f.id: f for f in findings_},
            "Approval": {},
            "Project": {p.id: p for p in projects},
            "Workspace": {w.id: w for w in workspaces},
            "WorkspaceMember": dict(enumerate(members)),
        }
        self.added: list = []
        self.commits = 0

    async def get(self, model, ident, *, with_for_update=False, **_kw):
        return self.store.get(model.__name__, {}).get(ident)

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        rows = list(self.store.get(entity.__name__, {}).values())
        for key, value in _criteria(stmt):
            rows = [r for r in rows if getattr(r, key, None) == value]
        return FakeResult(rows)

    def add(self, obj):
        self.added.append(obj)
        key = getattr(obj, "id", None) or uuid.uuid4()
        self.store.setdefault(type(obj).__name__, {})[key] = obj

    async def flush(self):
        pass

    async def commit(self):
        self.commits += 1


# ── fixtures ─────────────────────────────────────────────────────────────────
def _user(role: str, email: str, name: str) -> User:
    return User(id=uuid.uuid4(), email=email, display_name=name, password_hash=_HASH, role=role)


PROJECT_ID = uuid.uuid4()
WORKSPACE = Workspace(id=uuid.uuid4(), name="W", kind="team")
_WORKSPACE_ROLE = {"admin": "owner", "approver": "approver", "analyst": "analyst"}


@pytest.fixture
def people() -> dict[str, User]:
    return {
        "admin": _user("admin", "admin@example.com", "Ada Admin"),
        "approver": _user("approver", "approver@example.com", "Rex Reviewer"),
        "analyst": _user("analyst", "analyst@example.com", "Ann Analyst"),
    }


@pytest.fixture
def members(people) -> list[WorkspaceMember]:
    return [
        WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
        for u in people.values()
    ]


def _connected_write_finding(status: str = "draft", *, upload=None, source=None, **over) -> Finding:
    source = source or {"kind": "inline", "content": "hello world"}
    # A real sha256 of the actual inline content, not a placeholder — finding
    # 5 verifies this against the bytes about to be uploaded, so a fixture
    # whose hash doesn't match its own content would make every ordinary
    # approve-and-upload test in this file look like tampering. Only a
    # deliverable/other source carries no fixed content yet (rendered at
    # approval time), so it gets no size/hash either, same as the real tool.
    if source.get("kind") == "inline":
        content_bytes = (source.get("content") or "").encode("utf-8")
        size, content_sha256 = len(content_bytes), hashlib.sha256(content_bytes).hexdigest()
    else:
        size = content_sha256 = None
    payload = {
        "target_slug": "site-finance",
        "target_label": "Finance",
        "target_path": "/Documents/tret",
        "filename": "report.md",
        "content_type": "text/markdown",
        "source": source,
        "size": size,
        "content_sha256": content_sha256,
        "upload": upload,
    }
    base = dict(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        schema_slug="connected_write",
        subject={"target": "site-finance", "filename": "report.md"},
        payload=payload,
        provenance={"model": "anthropic/test", "doctrine_sha": "abc123"},
        status=status,
    )
    base.update(over)
    return Finding(**base)


@pytest.fixture
def connected_write() -> Finding:
    return _connected_write_finding()


@pytest.fixture
def db(people, connected_write, members) -> FakeSession:
    return FakeSession(
        users=people.values(),
        findings_=[connected_write],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=members,
    )


@pytest.fixture
def client(db) -> TestClient:
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        yield c
    login_limiter.reset()


def login_as(client: TestClient, user: User) -> None:
    client.cookies.clear()
    response = client.post("/api/auth/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text


def decide(client: TestClient, finding_id, **body):
    return client.post(f"/api/findings/{finding_id}/approval", json=body)


# ── the services/connections.py write contract, faked ────────────────────────
class FakeConnectionWriteError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class FakeConnectionUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FakeUploadResult:
    item_id: str
    web_url: str
    name: str
    size: int
    target_slug: str
    path: str


@pytest.fixture()
def upload_ok(monkeypatch):
    """`upload_connected_file` that always succeeds, remembering every call."""
    calls: list[dict] = []

    async def _upload(
        db, *, workspace_id, target_slug, filename, data, content_type, actor_user_id=None,
        actor_run_id=None,
    ):
        calls.append(
            {
                "workspace_id": workspace_id,
                "target_slug": target_slug,
                "filename": filename,
                "data": data,
                "content_type": content_type,
                "actor_user_id": actor_user_id,
            }
        )
        return FakeUploadResult(
            item_id="item-1",
            web_url="https://contoso.sharepoint.com/Documents/tret/" + filename,
            name=filename,
            size=len(data),
            target_slug=target_slug,
            path="/Documents/tret/" + filename,
        )

    monkeypatch.setattr(findings_api.connections_service, "upload_connected_file", _upload, raising=False)
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionWriteError", FakeConnectionWriteError, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "safe_upload_filename", lambda n: n, raising=False
    )
    return calls


@pytest.fixture()
def upload_fails(monkeypatch):
    """`upload_connected_file` that always raises `ConnectionWriteError`."""

    async def _upload(db, **kwargs):
        raise FakeConnectionWriteError("The Finance connection has expired; reconnect to retry.")

    monkeypatch.setattr(findings_api.connections_service, "upload_connected_file", _upload, raising=False)
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionWriteError", FakeConnectionWriteError, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "safe_upload_filename", lambda n: n, raising=False
    )


@pytest.fixture()
def upload_ok_with_real_activity(monkeypatch):
    """`upload_connected_file` that succeeds and — unlike `upload_ok` above —
    behaves like the real function for record-keeping: it actually calls
    the real `record_connection_activity` (`db.add` + `db.flush`, never a
    fake of it) to write its `action="upload"` row. `upload_ok`'s wholesale
    replacement skips that entirely, which would make a test using it
    unable to tell whether `publish_deliverable` actually commits that row
    (finding 1) or not — this fixture exists so `test_publish_happy_path`
    can."""
    calls: list[dict] = []

    async def _upload(
        db, *, workspace_id, target_slug, filename, data, content_type, actor_user_id=None,
        actor_run_id=None,
    ):
        calls.append({"target_slug": target_slug, "filename": filename, "data": data})
        await connections_service.record_connection_activity(
            db,
            workspace_id=workspace_id,
            provider="m365",
            action="upload",
            target=f"{target_slug}/tret/{filename}",
            bytes_count=len(data),
            actor_user_id=actor_user_id,
            actor_run_id=actor_run_id,
        )
        return FakeUploadResult(
            item_id="item-1",
            web_url="https://contoso.sharepoint.com/Documents/tret/" + filename,
            name=filename,
            size=len(data),
            target_slug=target_slug,
            path="/Documents/tret/" + filename,
        )

    monkeypatch.setattr(findings_api.connections_service, "upload_connected_file", _upload, raising=False)
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionWriteError", FakeConnectionWriteError, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "safe_upload_filename", lambda n: n, raising=False
    )
    return calls


@pytest.fixture()
def upload_fails_with_real_activity(monkeypatch):
    """`upload_connected_file` that raises `ConnectionWriteError`, but —
    like the real function's own except-clause around the Graph call —
    records the `action="upload_failed"` row (the real `record_connection_
    activity`, `db.add` + `db.flush`) before raising. Exists for the same
    reason `upload_ok_with_real_activity` does: `upload_fails`'s wholesale
    replacement never touches the database at all, so a test built on it
    cannot see whether the refusal's activity row survives past the
    request (finding 1's other half — commit before raising the 409)."""

    async def _upload(db, *, workspace_id, target_slug, filename, data, **kwargs):
        await connections_service.record_connection_activity(
            db,
            workspace_id=workspace_id,
            provider="m365",
            action="upload_failed",
            target=f"{target_slug}/tret/{filename}",
            bytes_count=len(data),
            detail="The Finance connection has expired; reconnect to retry.",
        )
        raise FakeConnectionWriteError("The Finance connection has expired; reconnect to retry.")

    monkeypatch.setattr(findings_api.connections_service, "upload_connected_file", _upload, raising=False)
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionWriteError", FakeConnectionWriteError, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "safe_upload_filename", lambda n: n, raising=False
    )


@pytest.fixture()
def fake_render_bytes(monkeypatch):
    """`services.export.render_deliverable_bytes`, faked so these tests never
    need a real assembled deliverable — remembers every call."""
    calls: list[dict] = []

    async def _render(db, project_id, deliverable_slug, format, include_draft=False):
        calls.append(
            {
                "project_id": project_id,
                "deliverable_slug": deliverable_slug,
                "format": format,
                "include_draft": include_draft,
            }
        )
        content_type = {"markdown": "text/markdown", "html": "text/html", "pdf": "application/pdf"}[format]
        return f"# {deliverable_slug}".encode(), content_type

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _render, raising=False)
    return calls


# ══════════════════════════════════════════════════════════════════════════
# approval side effect
# ══════════════════════════════════════════════════════════════════════════
def test_approving_uploads_with_the_right_bytes_and_content_type(client, db, people, connected_write, upload_ok):
    login_as(client, people["approver"])
    response = decide(client, connected_write.id, action="approve")
    assert response.status_code == 200
    assert response.json()["status"] == "approved"

    assert len(upload_ok) == 1
    call = upload_ok[0]
    assert call["data"] == b"hello world"
    assert call["content_type"] == "text/markdown"
    assert call["filename"] == "report.md"
    assert call["target_slug"] == "site-finance"
    assert call["workspace_id"] == WORKSPACE.id
    assert call["actor_user_id"] == people["approver"].id


def test_a_successful_upload_is_recorded_on_the_finding(client, db, people, connected_write, upload_ok):
    login_as(client, people["approver"])
    decide(client, connected_write.id, action="approve")

    upload = connected_write.payload["upload"]
    assert upload["status"] == "uploaded"
    assert upload["item_id"] == "item-1"
    assert upload["web_url"] == "https://contoso.sharepoint.com/Documents/tret/report.md"
    assert upload["uploaded_at"] is not None
    assert upload["error"] is None
    assert upload["approver_id"] == str(people["approver"].id)


def test_a_failed_upload_is_recorded_but_the_endpoint_still_returns_200(
    client, db, people, connected_write, upload_fails
):
    login_as(client, people["approver"])
    response = decide(client, connected_write.id, action="approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert connected_write.status == "approved"  # the approval itself still went through

    upload = connected_write.payload["upload"]
    assert upload["status"] == "failed"
    assert upload["item_id"] is None
    assert upload["web_url"] is None
    assert "expired" in upload["error"]
    assert upload["approver_id"] == str(people["approver"].id)


def test_rejecting_does_not_upload(client, db, people, connected_write, upload_ok):
    login_as(client, people["approver"])
    response = decide(client, connected_write.id, action="reject")

    assert response.status_code == 200
    assert connected_write.status == "rejected"
    assert upload_ok == []  # never called
    assert connected_write.payload["upload"] is None  # untouched


def test_approving_a_non_connected_write_finding_never_calls_upload(client, upload_ok, people):
    other = Finding(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        schema_slug="draft_section",
        subject={"deliverable": "d", "section": "s"},
        payload={"markdown": "body"},
        provenance={},
        status="draft",
    )
    db = FakeSession(
        users=people.values(),
        findings_=[other],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{other.id}/approval", json={"action": "approve"})
    login_limiter.reset()

    assert response.status_code == 200
    assert upload_ok == []


def test_approving_a_deliverable_sourced_finding_renders_then_uploads(
    client, db, people, upload_ok, fake_render_bytes
):
    finding = _connected_write_finding(
        source={"kind": "deliverable", "slug": "tcfd-report", "format": "html"},
    )
    finding.payload["filename"] = "report.html"
    finding.payload["content_type"] = "text/html"
    finding.payload["size"] = None
    finding.payload["content_sha256"] = None
    db.store["Finding"][finding.id] = finding

    login_as(client, people["approver"])
    response = decide(client, finding.id, action="approve")
    assert response.status_code == 200

    assert fake_render_bytes == [
        {
            "project_id": PROJECT_ID,
            "deliverable_slug": "tcfd-report",
            "format": "html",
            "include_draft": False,
        }
    ]
    assert upload_ok[0]["data"] == b"# tcfd-report"
    assert upload_ok[0]["content_type"] == "text/html"
    assert finding.payload["upload"]["status"] == "uploaded"


def test_an_empty_deliverable_records_a_clear_failure(client, db, people, upload_ok, monkeypatch):
    finding = _connected_write_finding(source={"kind": "deliverable", "slug": "empty-report", "format": "markdown"})
    db.store["Finding"][finding.id] = finding

    async def _raise_empty(db_, project_id, deliverable_slug, format, include_draft=False):
        raise export_module.DeliverableEmpty(deliverable_slug)

    monkeypatch.setattr(export_module, "render_deliverable_bytes", _raise_empty, raising=False)

    login_as(client, people["approver"])
    response = decide(client, finding.id, action="approve")

    assert response.status_code == 200
    assert finding.payload["upload"]["status"] == "failed"
    assert "empty-report" in finding.payload["upload"]["error"]
    assert upload_ok == []  # never reached


def test_an_unknown_source_kind_records_a_clear_failure(client, db, people, upload_ok):
    finding = _connected_write_finding(source={"kind": "bogus"})
    db.store["Finding"][finding.id] = finding

    login_as(client, people["approver"])
    decide(client, finding.id, action="approve")

    assert finding.payload["upload"]["status"] == "failed"
    assert "bogus" in finding.payload["upload"]["error"]
    assert upload_ok == []


# ══════════════════════════════════════════════════════════════════════════
# content hash verification (finding 5)
# ══════════════════════════════════════════════════════════════════════════
def test_a_tampered_inline_payload_is_refused_without_uploading(client, db, people, upload_ok):
    """The payload's own `content_sha256` no longer matches its `content` —
    altered after `propose_connected_write` recorded it, whether by a bug or
    a hand-edited row. Never uploaded; recorded as a clear failure instead."""
    finding = _connected_write_finding(
        source={"kind": "inline", "content": "hello world"},
    )
    finding.payload["content_sha256"] = "0" * 64  # no longer matches "hello world"
    db.store["Finding"][finding.id] = finding

    login_as(client, people["approver"])
    response = decide(client, finding.id, action="approve")

    assert response.status_code == 200  # approval itself still succeeds
    assert finding.status == "approved"
    assert finding.payload["upload"]["status"] == "failed"
    assert finding.payload["upload"]["error"] == (
        "Content hash mismatch; the proposal was altered after it was made."
    )
    assert upload_ok == []  # never reached


def test_a_matching_inline_payload_uploads_normally(client, db, people, upload_ok):
    """The control on the test above: a correct `content_sha256` (the
    fixture's default, computed from the real content) uploads exactly as
    before — hash verification must not false-positive on the ordinary
    case."""
    finding = _connected_write_finding(source={"kind": "inline", "content": "hello world"})
    db.store["Finding"][finding.id] = finding

    login_as(client, people["approver"])
    response = decide(client, finding.id, action="approve")

    assert response.status_code == 200
    assert finding.payload["upload"]["status"] == "uploaded"
    assert len(upload_ok) == 1


def test_a_deliverable_sourced_payload_skips_hash_verification(
    client, db, people, upload_ok, fake_render_bytes
):
    """A deliverable-sourced write has no fixed content to have drifted from
    — it is rendered fresh at approval — so it carries no `content_sha256`
    to check against, and must upload normally rather than being refused
    for a hash that was never meant to exist yet."""
    finding = _connected_write_finding(
        source={"kind": "deliverable", "slug": "tcfd-report", "format": "markdown"}
    )
    assert finding.payload["content_sha256"] is None  # nothing to verify
    db.store["Finding"][finding.id] = finding

    login_as(client, people["approver"])
    response = decide(client, finding.id, action="approve")

    assert response.status_code == 200
    assert finding.payload["upload"]["status"] == "uploaded"
    assert len(upload_ok) == 1


# ══════════════════════════════════════════════════════════════════════════
# upload-retry
# ══════════════════════════════════════════════════════════════════════════
def _failed_upload(approver_id) -> dict:
    return {
        "status": "failed",
        "item_id": None,
        "web_url": None,
        "uploaded_at": None,
        "error": "The Finance connection has expired; reconnect to retry.",
        "approver_id": str(approver_id),
    }


def test_retry_reuploads_a_failed_upload_and_succeeds(client, people, upload_ok):
    approver = people["approver"]
    finding = _connected_write_finding(status="approved", upload=_failed_upload(approver.id))
    db = FakeSession(
        users=people.values(),
        findings_=[finding],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        login_as(c, approver)
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()

    assert response.status_code == 200
    assert response.json()["payload"]["upload"]["status"] == "uploaded"
    assert len(upload_ok) == 1
    assert finding.status == "approved"  # unaffected


@pytest.mark.parametrize(
    "upload_value",
    [None, {}],
    ids=["upload_is_none", "upload_key_missing_entirely"],
)
def test_retry_accepts_a_finding_whose_upload_was_never_observed(
    people, upload_ok, upload_value, monkeypatch
):
    """A process that died between the approval's status commit and
    `_upload_connected_write`'s own commit leaves `payload["upload"]` at
    `None` (or, for a payload built without ever setting the key at all,
    missing) forever — there is no other retry path. `upload-retry` must
    accept that the same way it accepts a recorded 'failed' (finding 2)."""
    approver = people["approver"]
    finding = _connected_write_finding(status="approved", upload=upload_value)
    if upload_value == {}:
        # Exercise "the key is missing entirely", not just "the key is an
        # empty dict" — payload.get("upload") returns None either way, but
        # this pins the actual shape described in the finding.
        del finding.payload["upload"]
    db = FakeSession(
        users=people.values(),
        findings_=[finding],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        login_as(c, approver)
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()

    assert response.status_code == 200
    assert response.json()["payload"]["upload"]["status"] == "uploaded"
    assert len(upload_ok) == 1


def test_retry_locks_the_finding_row_for_the_length_of_the_retry(people, upload_ok, monkeypatch):
    """Concurrent retries of the same finding must not both read
    'failed'/None and both upload (finding 4) — `retry_finding_upload` now
    reads the finding `with_for_update=True`, same as `decide_finding`. The
    fake harness has no real row locking to observe, so this pins the call
    itself: `db.get(Finding, finding_id, with_for_update=True)`."""
    finding = _connected_write_finding(
        status="approved", upload=_failed_upload(people["approver"].id)
    )
    db = FakeSession(
        users=people.values(),
        findings_=[finding],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    calls: list[dict] = []
    original_get = FakeSession.get

    async def _tracking_get(self, model, ident, *, with_for_update=False, **kw):
        if model is Finding:
            calls.append({"ident": ident, "with_for_update": with_for_update})
        return await original_get(self, model, ident, with_for_update=with_for_update, **kw)

    monkeypatch.setattr(FakeSession, "get", _tracking_get)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    login_limiter.reset()
    with TestClient(app) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()

    assert response.status_code == 200
    assert calls == [{"ident": finding.id, "with_for_update": True}]


def _retry_client(finding: Finding, people) -> TestClient:
    db = FakeSession(
        users=people.values(),
        findings_=[finding],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app)


def test_retry_is_refused_for_a_draft_finding(people, upload_ok):
    finding = _connected_write_finding(status="draft")
    login_limiter.reset()
    with _retry_client(finding, people) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()
    assert response.status_code == 409
    assert upload_ok == []


def test_retry_is_refused_when_the_upload_did_not_fail(people, upload_ok):
    finding = _connected_write_finding(
        status="approved",
        upload={
            "status": "uploaded",
            "item_id": "item-1",
            "web_url": "https://x",
            "uploaded_at": "2026-09-08T00:00:00+00:00",
            "error": None,
            "approver_id": str(people["approver"].id),
        },
    )
    login_limiter.reset()
    with _retry_client(finding, people) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()
    assert response.status_code == 409
    assert "nothing to retry" in response.json()["detail"].lower()
    assert upload_ok == []


def test_retry_is_refused_for_a_non_connected_write_schema(people, upload_ok):
    other = Finding(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        project_id=PROJECT_ID,
        schema_slug="draft_section",
        subject={"deliverable": "d", "section": "s"},
        payload={"markdown": "body"},
        provenance={},
        status="approved",
    )
    login_limiter.reset()
    with _retry_client(other, people) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{other.id}/upload-retry")
    login_limiter.reset()
    assert response.status_code == 409


def test_retry_requires_the_approver_role(people, upload_ok):
    finding = _connected_write_finding(status="approved", upload=_failed_upload(people["approver"].id))
    login_limiter.reset()
    with _retry_client(finding, people) as c:
        login_as(c, people["analyst"])
        response = c.post(f"/api/findings/{finding.id}/upload-retry")
    login_limiter.reset()
    assert response.status_code == 403
    assert upload_ok == []


def test_retry_on_an_unknown_finding_is_404(people, upload_ok):
    login_limiter.reset()
    with _retry_client(_connected_write_finding(), people) as c:
        login_as(c, people["approver"])
        response = c.post(f"/api/findings/{uuid.uuid4()}/upload-retry")
    login_limiter.reset()
    assert response.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# POST /api/deliverables/{slug}/publish — the human route
# ══════════════════════════════════════════════════════════════════════════
def _publish_client(people) -> TestClient:
    db = FakeSession(
        users=people.values(),
        findings_=[],
        projects=[Project(id=PROJECT_ID, workspace_id=WORKSPACE.id, name="P")],
        workspaces=[WORKSPACE],
        members=[
            WorkspaceMember(user_id=u.id, workspace_id=WORKSPACE.id, role=_WORKSPACE_ROLE[u.role])
            for u in people.values()
        ],
    )
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(findings_api.router)
    app.dependency_overrides[get_db] = lambda: db
    return TestClient(app), db


def _publish(client: TestClient, slug="tcfd-report", **over):
    body = {"target_slug": "site-finance", "filename": "report.md", "format": "markdown"}
    body.update(over)
    return client.post(f"/api/deliverables/{slug}/publish", json=body)


def test_publish_happy_path(people, upload_ok_with_real_activity, fake_render_bytes):
    client, db = _publish_client(people)
    commits_before = db.commits
    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c)
    login_limiter.reset()

    assert response.status_code == 200
    assert response.json() == {
        "web_url": "https://contoso.sharepoint.com/Documents/tret/report.md",
        "item_id": "item-1",
        "name": "report.md",
        "size": len(b"# tcfd-report"),
        "target_slug": "site-finance",
        "path": "/Documents/tret/report.md",
    }
    assert fake_render_bytes[0]["deliverable_slug"] == "tcfd-report"
    assert fake_render_bytes[0]["format"] == "markdown"
    # No Finding is created for this route.
    assert not [obj for obj in db.added if isinstance(obj, Finding)]

    # The ConnectionActivity row upload_connected_file flushed must actually
    # be committed by publish_deliverable, not left to roll back with the
    # request session (finding 1).
    upload_rows = [
        row for row in db.store.get("ConnectionActivity", {}).values() if row.action == "upload"
    ]
    assert len(upload_rows) == 1
    assert db.commits > commits_before


def test_publish_commits_the_upload_failed_activity_row_before_raising_409(
    people, fake_render_bytes, upload_fails_with_real_activity
):
    client, db = _publish_client(people)
    commits_before = db.commits
    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c)
    login_limiter.reset()

    assert response.status_code == 409
    failed_rows = [
        row for row in db.store.get("ConnectionActivity", {}).values() if row.action == "upload_failed"
    ]
    assert len(failed_rows) == 1
    # Committed, not just flushed — a refusal's own activity row must
    # survive past this request's session close exactly like a success's
    # does (finding 1: commit, then raise).
    assert db.commits > commits_before


def test_publish_rejects_include_draft(people, upload_ok, fake_render_bytes):
    """`include_draft` was removed from this endpoint's body — publish is
    always approved-only content, unlike the export preview's own opt-in
    draft toggle (finding 6). `extra="forbid"` on `PublishDeliverableBody`
    turns a client still sending it into an audible 422, not a silently
    ignored field."""
    client, _db = _publish_client(people)
    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c, include_draft=True)
    login_limiter.reset()
    assert response.status_code == 422
    assert upload_ok == []


def test_publish_requires_the_approver_role(people, upload_ok, fake_render_bytes):
    client, _db = _publish_client(people)
    login_limiter.reset()
    with client as c:
        login_as(c, people["analyst"])
        response = _publish(c)
    login_limiter.reset()
    assert response.status_code == 403
    assert upload_ok == []
    assert fake_render_bytes == []


def test_publish_409s_on_connection_write_error(people, fake_render_bytes, monkeypatch):
    client, _db = _publish_client(people)

    async def _upload(db, **kwargs):
        raise FakeConnectionWriteError("The Finance connection has expired; reconnect to retry.")

    monkeypatch.setattr(findings_api.connections_service, "upload_connected_file", _upload, raising=False)
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionWriteError", FakeConnectionWriteError, raising=False
    )
    monkeypatch.setattr(
        findings_api.connections_service, "ConnectionUnavailable", FakeConnectionUnavailable, raising=False
    )
    monkeypatch.setattr(findings_api.connections_service, "safe_upload_filename", lambda n: n, raising=False)

    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c)
    login_limiter.reset()

    assert response.status_code == 409
    assert response.json()["detail"] == "The Finance connection has expired; reconnect to retry."


def test_publish_400s_on_a_bad_filename(people, fake_render_bytes, monkeypatch):
    client, _db = _publish_client(people)

    def _bad(name: str) -> str:
        raise ValueError(f"'{name}' is not a safe filename")

    monkeypatch.setattr(findings_api.connections_service, "safe_upload_filename", _bad, raising=False)

    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c)
    login_limiter.reset()

    assert response.status_code == 400
    assert "not a safe filename" in response.json()["detail"]


def test_publish_refuses_an_unknown_format(people, upload_ok, fake_render_bytes):
    client, _db = _publish_client(people)
    login_limiter.reset()
    with client as c:
        login_as(c, people["approver"])
        response = _publish(c, format="docx")
    login_limiter.reset()
    assert response.status_code == 422
    assert upload_ok == []
