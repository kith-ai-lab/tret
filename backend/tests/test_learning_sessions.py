"""Learning round (A): session groups, rename/move, archive, and the per-message
facts on GET /api/chat/{id} (R01 stop_reason, R08 analysis_runs, R20-R22, R24).

Same end-to-end harness as test_chat_api.py (sqlite + ASGITransport)."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tests.test_chat_api import (
    PASSWORD,
    _NoopEngine,
    make_member,
    make_user,
    make_workspace,
)
from tret.api import auth
from tret.api import chat as chat_api
from tret.db.engine import get_db
from tret.db.models import (
    AnalysisRun,
    Base,
    Conversation,
    Finding,
    Harness,
    Project,
    Run,
)

pytestmark = pytest.mark.asyncio

FOLDER_A = [{"drive_id": "d1", "item_id": "i1", "name": "Week 3", "path": "/Week 3"}]
FOLDER_B = [{"drive_id": "d1", "item_id": "i2", "name": "Week 4", "path": "/Week 4"}]


@pytest_asyncio.fixture
async def session_factory():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(eng, expire_on_commit=False)
    await eng.dispose()


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    monkeypatch.setattr(chat_api, "get_harness_engine", lambda: _NoopEngine())
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(chat_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _setup(client, session_factory, email="a@example.com", name="Co"):
    """Workspace + project + chat harness + logged-in user. Returns project id."""
    team = make_workspace(name)
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user(email)
    async with session_factory() as db:
        db.add_all([team, project, user, make_member(user, team, role="analyst")])
        db.add(
            Harness(
                workspace_id=team.id,
                name="Chat Assistant",
                task_profile="chat",
                model_policy={"mode": "auto"},
                tool_names=[],
            )
        )
        await db.commit()
    r = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return project.id


async def _new_session(client, **body) -> dict:
    r = await client.post("/api/chat", json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def _set_folders(session_factory, conv_id: str, folders: list[dict], *, age_s: int = 0):
    async with session_factory() as db:
        c = await db.get(Conversation, uuid.UUID(conv_id))
        c.connected_folders = folders
        c.updated_at = datetime.now(timezone.utc) - timedelta(seconds=age_s)
        await db.commit()


# ── groups ──


async def test_create_rename_reposition_and_delete_group(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={})).json()
    assert g["title"] == "New group"
    assert g["session_count"] == 0 and g["folders"] == []

    g2 = (await client.post("/api/chat/groups", json={"title": "  Week 4  "})).json()
    assert g2["title"] == "Week 4"
    assert g2["position"] > g["position"]

    patched = await client.patch(f"/api/chat/groups/{g['id']}", json={"title": "Cohort 1", "position": 5})
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Cohort 1" and patched.json()["position"] == 5
    assert (await client.patch(f"/api/chat/groups/{g['id']}", json={"title": "  "})).status_code == 422

    listed = (await client.get("/api/chat/groups")).json()
    # ordered by position then created_at
    assert [x["title"] for x in listed] == ["Week 4", "Cohort 1"]

    assert (await client.delete(f"/api/chat/groups/{g2['id']}")).status_code == 204
    assert [x["id"] for x in (await client.get("/api/chat/groups")).json()] == [g["id"]]
    assert (await client.delete(f"/api/chat/groups/{g2['id']}")).status_code == 404


async def test_deleting_a_group_ungroups_its_sessions(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={"title": "G"})).json()
    s = await _new_session(client, group_id=g["id"])
    assert s["group_id"] == g["id"]

    assert (await client.delete(f"/api/chat/groups/{g['id']}")).status_code == 204
    got = (await client.get(f"/api/chat/{s['id']}")).json()
    assert got["group_id"] is None
    assert any(x["id"] == s["id"] for x in (await client.get("/api/chat")).json())


async def test_groups_route_is_not_swallowed_by_conversation_id(client, session_factory):
    await _setup(client, session_factory)
    r = await client.get("/api/chat/groups")
    assert r.status_code == 200 and r.json() == []


async def test_group_from_another_workspace_is_404(client, session_factory):
    await _setup(client, session_factory, "a@example.com", "A")
    g = (await client.post("/api/chat/groups", json={"title": "Mine"})).json()
    await client.post("/api/auth/logout")
    await _setup(client, session_factory, "b@example.com", "B")

    assert (await client.get("/api/chat/groups")).json() == []
    assert (await client.patch(f"/api/chat/groups/{g['id']}", json={"title": "x"})).status_code == 404
    assert (await client.delete(f"/api/chat/groups/{g['id']}")).status_code == 404
    assert (await client.post("/api/chat", json={"group_id": g["id"]})).status_code == 404
    s = await _new_session(client)
    assert (await client.patch(f"/api/chat/{s['id']}", json={"group_id": g["id"]})).status_code == 404


# ── patch session ──


async def test_patch_session_rename_move_and_ungroup(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={"title": "G"})).json()
    s = await _new_session(client)

    r = await client.patch(f"/api/chat/{s['id']}", json={"title": "  Week 3 inventory "})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Week 3 inventory"
    assert r.json()["group_id"] is None  # omitted key untouched

    r = await client.patch(f"/api/chat/{s['id']}", json={"group_id": g["id"]})
    assert r.json()["group_id"] == g["id"] and r.json()["title"] == "Week 3 inventory"

    r = await client.patch(f"/api/chat/{s['id']}", json={"title": "Renamed"})
    assert r.json()["group_id"] == g["id"]  # group untouched when omitted

    r = await client.patch(f"/api/chat/{s['id']}", json={"group_id": None})
    assert r.json()["group_id"] is None and r.json()["title"] == "Renamed"

    assert (await client.patch(f"/api/chat/{s['id']}", json={"title": ""})).status_code == 422
    assert (await client.patch(f"/api/chat/{s['id']}", json={"title": "x" * 201})).status_code == 422
    assert (await client.patch(f"/api/chat/{uuid.uuid4()}", json={"title": "x"})).status_code == 404


async def test_group_session_count_excludes_archived(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={})).json()
    a = await _new_session(client, group_id=g["id"])
    await _new_session(client, group_id=g["id"])
    assert (await client.get("/api/chat/groups")).json()[0]["session_count"] == 2
    await client.post(f"/api/chat/{a['id']}/archive")
    assert (await client.get("/api/chat/groups")).json()[0]["session_count"] == 1


# ── R20 folder inheritance ──


async def test_create_in_group_inherits_folders_from_most_recent_live_session(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={})).json()
    old = await _new_session(client, group_id=g["id"])
    new = await _new_session(client, group_id=g["id"])
    empty = await _new_session(client, group_id=g["id"])  # no folders: skipped
    await _set_folders(session_factory, old["id"], FOLDER_A, age_s=600)
    await _set_folders(session_factory, new["id"], FOLDER_B, age_s=60)
    await _set_folders(session_factory, empty["id"], [], age_s=0)

    child = await _new_session(client, group_id=g["id"])
    assert child["folders"] == FOLDER_B
    assert child["group_id"] == g["id"]
    # the group advertises the same folders
    assert (await client.get("/api/chat/groups")).json()[0]["folders"] == FOLDER_B

    # archived sessions do not donate folders
    await client.post(f"/api/chat/{new['id']}/archive")
    await client.post(f"/api/chat/{child['id']}/archive")
    again = await _new_session(client, group_id=g["id"])
    assert again["folders"] == FOLDER_A


async def test_explicit_empty_folders_means_none(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={})).json()
    donor = await _new_session(client, group_id=g["id"])
    await _set_folders(session_factory, donor["id"], FOLDER_A)

    s = await _new_session(client, group_id=g["id"], folders=[])
    assert s["folders"] == []
    inherited = await _new_session(client, group_id=g["id"])
    assert inherited["folders"] == FOLDER_A


async def test_group_with_no_folders_gives_empty_and_ungrouped_inherits_nothing(client, session_factory):
    await _setup(client, session_factory)
    g = (await client.post("/api/chat/groups", json={})).json()
    assert (await _new_session(client, group_id=g["id"]))["folders"] == []
    assert (await _new_session(client))["folders"] == []


# ── archive ──


async def test_archive_unarchive_and_list_filtering(client, session_factory):
    await _setup(client, session_factory)
    keep = await _new_session(client)
    gone = await _new_session(client)

    r = await client.post(f"/api/chat/{gone['id']}/archive")
    assert r.status_code == 200, r.text
    assert r.json()["archived_at"] is not None

    live = {c["id"] for c in (await client.get("/api/chat")).json()}
    assert live == {keep["id"]}
    explicit = {c["id"] for c in (await client.get("/api/chat?archived=false")).json()}
    assert explicit == live
    archived = (await client.get("/api/chat?archived=true")).json()
    assert [c["id"] for c in archived] == [gone["id"]]
    assert archived[0]["archived_at"] is not None and "group_id" in archived[0]

    r = await client.post(f"/api/chat/{gone['id']}/unarchive")
    assert r.json()["archived_at"] is None
    assert {c["id"] for c in (await client.get("/api/chat")).json()} == {keep["id"], gone["id"]}
    assert (await client.get("/api/chat?archived=true")).json() == []
    assert (await client.post(f"/api/chat/{uuid.uuid4()}/archive")).status_code == 404


async def test_archived_session_is_still_readable_and_keeps_runs(client, session_factory):
    await _setup(client, session_factory)
    s = await _new_session(client)
    sent = await client.post(f"/api/chat/{s['id']}/messages", json={"text": "hi"})
    run_id = sent.json()["run_id"]
    await client.post(f"/api/chat/{s['id']}/archive")
    got = await client.get(f"/api/chat/{s['id']}")
    assert got.status_code == 200 and got.json()["archived_at"] is not None
    async with session_factory() as db:
        assert (await db.get(Run, uuid.UUID(run_id))) is not None


async def test_sending_to_an_archived_session_unarchives_it(client, session_factory):
    await _setup(client, session_factory)
    s = await _new_session(client)
    await client.post(f"/api/chat/{s['id']}/archive")
    sent = await client.post(f"/api/chat/{s['id']}/messages", json={"text": "back again"})
    assert sent.status_code == 200, sent.text
    assert (await client.get(f"/api/chat/{s['id']}")).json()["archived_at"] is None
    assert s["id"] in {c["id"] for c in (await client.get("/api/chat")).json()}


# ── message facts ──


async def _conversation_with_reply(client, session_factory, project_id, **run_over):
    """A conversation with one finished assistant turn built from a real Run."""
    s = await _new_session(client)
    sent = await client.post(f"/api/chat/{s['id']}/messages", json={"text": "go"})
    run_id = uuid.UUID(sent.json()["run_id"])
    async with session_factory() as db:
        run = await db.get(Run, run_id)
        run.status = "completed"
        run.messages = [{"role": "assistant", "content": "done", "tool_calls": []}]
        for k, v in run_over.items():
            setattr(run, k, v)
        conv = await db.get(Conversation, uuid.UUID(s["id"]))
        conv.messages = [*conv.messages, chat_api._assistant_message(run)]
        await db.commit()
    return s["id"], run_id


async def test_messages_carry_stop_reason_truncated_and_guardrails(client, session_factory):
    pid = await _setup(client, session_factory)
    guard = {"settings": {"number_check": True}, "overhead": {"repair_turns": 0}}
    cid, run_id = await _conversation_with_reply(
        client, session_factory, pid, stop_reason="max_tokens", guardrails=guard
    )
    msgs = (await client.get(f"/api/chat/{cid}")).json()["messages"]
    reply = next(m for m in msgs if m["role"] == "assistant")
    assert reply["stop_reason"] == "max_tokens"
    assert reply["truncated"] is True
    assert reply["guardrails"] == guard
    assert reply["analysis_runs"] == [] and reply["saved_files"] == []

    # Read from the run, so a later run update shows without rewriting the message.
    async with session_factory() as db:
        run = await db.get(Run, run_id)
        run.stop_reason = "out_of_steps"
        await db.commit()
    reply = next(m for m in (await client.get(f"/api/chat/{cid}")).json()["messages"] if m["role"] == "assistant")
    assert reply["stop_reason"] == "out_of_steps" and reply["truncated"] is False


async def test_clean_reply_has_null_stop_reason_and_user_messages_are_untouched(client, session_factory):
    pid = await _setup(client, session_factory)
    cid, _ = await _conversation_with_reply(client, session_factory, pid)
    msgs = (await client.get(f"/api/chat/{cid}")).json()["messages"]
    user_msg = next(m for m in msgs if m["role"] == "user")
    assert "stop_reason" not in user_msg and "saved_files" not in user_msg
    reply = next(m for m in msgs if m["role"] == "assistant")
    assert reply["stop_reason"] is None and reply["truncated"] is False and reply["guardrails"] is None


async def test_messages_carry_analysis_runs_and_saved_files_including_children(client, session_factory):
    pid = await _setup(client, session_factory)
    cid, run_id = await _conversation_with_reply(client, session_factory, pid)
    async with session_factory() as db:
        parent = await db.get(Run, run_id)
        child = Run(
            project_id=parent.project_id,
            harness_id=parent.harness_id,
            conversation_id=parent.conversation_id,
            parent_run_id=parent.id,
            root_run_id=parent.id,
            task_type="freeform",
            task_input={},
            status="completed",
        )
        db.add(child)
        await db.flush()
        db.add_all(
            [
                AnalysisRun(
                    project_id=pid, run_id=parent.id, purpose="sum", code="print(1)",
                    code_sha="aaa", row_count=3, status="completed",
                ),
                AnalysisRun(
                    project_id=pid, run_id=child.id, purpose="child calc", code="print(2)",
                    code_sha="bbb", row_count=1, status="failed",
                ),
                Finding(
                    run_id=parent.id, project_id=pid, schema_slug="connected_write",
                    subject={}, provenance={}, status="uploaded",
                    payload={
                        "filename": "week3.html", "content_sha256": "abc123",
                        "upload": {"path": "Week 3/tret/week3.html", "web_url": "https://x/w3", "status": "uploaded"},
                        "audit": {"filename": "week3.html.audit.json", "sha256": "def", "web_url": "https://x/a"},
                    },
                ),
                # child's write, no audit block yet (read defensively)
                Finding(
                    run_id=child.id, project_id=pid, schema_slug="connected_write",
                    subject={}, provenance={}, status="draft",
                    payload={"filename": "c.csv", "target_path": "Out", "relative_dir": "tret", "upload": None},
                ),
                # not a connected write: ignored
                Finding(
                    run_id=parent.id, project_id=pid, schema_slug="other",
                    subject={}, provenance={}, payload={},
                ),
            ]
        )
        await db.commit()

    reply = next(
        m for m in (await client.get(f"/api/chat/{cid}")).json()["messages"] if m["role"] == "assistant"
    )
    assert [(a["purpose"], a["code_sha"], a["row_count"], a["status"]) for a in reply["analysis_runs"]] == [
        ("sum", "aaa", 3, "completed"),
        ("child calc", "bbb", 1, "failed"),
    ]
    by_name = {f["filename"]: f for f in reply["saved_files"]}
    assert set(by_name) == {"week3.html", "c.csv"}
    w3 = by_name["week3.html"]
    assert w3["path"] == "Week 3/tret/week3.html" and w3["web_url"] == "https://x/w3"
    assert w3["status"] == "uploaded" and w3["sha256"] == "abc123"
    assert w3["audit_filename"] == "week3.html.audit.json" and w3["finding_id"]
    assert w3["audit_web_url"] == "https://x/a"
    c = by_name["c.csv"]
    assert c["path"] == "Out/tret/c.csv" and c["web_url"] is None
    assert c["status"] == "pending_approval" and c["audit_filename"] is None and c["audit_web_url"] is None


async def test_conversation_get_summary_fields_and_stable_shape(client, session_factory):
    await _setup(client, session_factory)
    s = await _new_session(client)
    got = (await client.get(f"/api/chat/{s['id']}")).json()
    for key in ("id", "title", "harness_id", "message_count", "updated_at", "folders", "messages",
                "active_run_id", "group_id", "archived_at"):
        assert key in got
