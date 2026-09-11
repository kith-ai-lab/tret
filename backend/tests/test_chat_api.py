"""Chat turn payload + per-turn routing/model overrides.

Most of this file is network- and DB-free: `_assistant_message` is exercised
with detached ORM `Run` objects (as in test_eco_accounting.py), and the
request-time helpers (`_validate_objective`, `_run_task_input`) are pure
functions called directly.

The final section (a harness may now link more than one pack) is the
exception — it drives `POST /api/chat` end to end against a real sqlite
database, `httpx.AsyncClient` + `ASGITransport` directly against the app,
same harness as test_harnesses_api.py — because what is under test there is
`send_message`'s own pack resolution, which needs a real `Conversation` /
`Harness` / `HarnessPack` row to resolve through.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI, HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import auth
from tret.api import chat as chat_api
from tret.api.chat import (
    SendMessageBody,
    _assistant_message,
    _capability_catalog,
    _run_task_input,
    _validate_objective,
)
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Harness,
    Pack,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceMember,
)
from tret.packs.links import set_harness_packs
from tret.providers.catalog import ModelInfo, energy_accounting
from tret.router_llm.objectives import OBJECTIVES


def _model(energy_class: str = "L") -> ModelInfo:
    return ModelInfo(
        id="anthropic/test",
        provider="anthropic",
        wire_id="test-1",
        display_name="Test",
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class=energy_class,
    )


def _run(**over) -> Run:
    base = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        harness_id=uuid.uuid4(),
        task_type="chat",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[{"role": "assistant", "content": "hello", "tool_calls": []}],
        iterations=1,
    )
    base.update(over)
    return Run(**base)


ROUTING = {
    "router_model": "anthropic/router",
    "routing_prompt_version": "v1",
    "candidates": ["anthropic/test"],
    "chosen_model": "anthropic/test",
    "reasoning": "only candidate",
    "confidence": None,
    "objective": "balanced",
    "fallback_used": False,
    "override": None,
    "latency_ms": 12,
    "decided_at": "2026-01-01T00:00:00+00:00",
}


# ── SendMessageBody ──────────────────────────────────────────────────────────


def test_send_message_body_defaults_omit_both_overrides():
    body = SendMessageBody(text="hi")
    assert body.model_override is None
    assert body.objective is None


def test_send_message_body_accepts_both_overrides():
    body = SendMessageBody(text="hi", model_override="anthropic/test", objective="eco")
    assert body.model_override == "anthropic/test"
    assert body.objective == "eco"


# ── objective validation ────────────────────────────────────────────────────


def test_validate_objective_accepts_every_valid_value():
    for objective in OBJECTIVES:
        _validate_objective(objective)  # must not raise


def test_validate_objective_accepts_none():
    _validate_objective(None)  # omitted entirely — must not raise


def test_validate_objective_rejects_garbage_with_the_allowed_values():
    with pytest.raises(HTTPException) as exc:
        _validate_objective("fastest")
    assert exc.value.status_code == 422
    for objective in OBJECTIVES:
        assert objective in exc.value.detail


# ── task_input threading ────────────────────────────────────────────────────


def test_task_input_omitting_both_overrides_is_unchanged():
    task_input = _run_task_input("hi", [], "caps", None, None)
    assert task_input == {"message": "hi", "_history": [], "_capabilities": "caps"}
    assert "_model_override" not in task_input
    assert "_objective" not in task_input


def test_task_input_threads_model_override():
    task_input = _run_task_input("hi", [], "caps", "anthropic/claude-x", None)
    assert task_input["_model_override"] == "anthropic/claude-x"
    assert "_objective" not in task_input


def test_task_input_threads_objective():
    task_input = _run_task_input("hi", [], "caps", None, "eco")
    assert task_input["_objective"] == "eco"
    assert "_model_override" not in task_input


def test_task_input_threads_both():
    task_input = _run_task_input("hi", [], "caps", "anthropic/claude-x", "quality")
    assert task_input["_model_override"] == "anthropic/claude-x"
    assert task_input["_objective"] == "quality"


# ── assistant message payload ───────────────────────────────────────────────


def test_assistant_message_carries_scopes_baseline_and_routing():
    accounting = energy_accounting(_model("L"), 100_000, 10_000, grid_g_per_kwh=400.0)
    run = _run(
        cost_usd=Decimal("0.45"),
        energy_wh=Decimal("132.0"),
        energy_accounting=accounting,
        routing=ROUTING,
        model_used="anthropic/test",
        input_tokens=100_000,
        output_tokens=10_000,
        cache_read_tokens=500,
        cache_write_tokens=50,
    )
    message = _assistant_message(run)
    assert message["role"] == "assistant"
    assert message["content"] == "hello"
    assert message["model_used"] == "anthropic/test"
    assert message["input_tokens"] == 100_000
    assert message["output_tokens"] == 10_000
    assert message["cache_read_tokens"] == 500
    assert message["cache_write_tokens"] == 50
    assert message["cost_usd"] == 0.45
    assert message["energy_wh"] == 132.0
    assert message["co2e_g"] == accounting["co2e_g"] > 0
    assert message["scope2_g"] == accounting["scopes"]["scope2_g"]
    assert message["scope3_g"] == accounting["scopes"]["scope3_g"]
    assert message["avoided_co2e_g"] == accounting["baseline"]["avoided_co2e_g"]
    # Full derivation/decision blocks, verbatim — for the expanded view's
    # EmissionsCalc/RoutingBadge reuse.
    assert message["energy"] == accounting
    assert message["routing"] == ROUTING


def test_assistant_message_names_the_empty_reply_case_in_plain_terms():
    # The engine's empty-reply guard ends the run `completed_without_output`;
    # the chat view says what happened in the person's terms, not the enum's.
    run = _run(
        status="completed_without_output",
        messages=[{"role": "assistant", "content": "", "tool_calls": [{"name": "lookup_dataset", "arguments": {}}]}],
    )
    message = _assistant_message(run)
    assert "no reply" in message["content"]
    assert "completed_without_output" not in message["content"]


def test_assistant_message_preserves_null_not_zero_when_unestimated():
    run = _run(cost_usd=Decimal("0.10"))
    message = _assistant_message(run)
    assert message["energy_wh"] is None
    assert message["co2e_g"] is None
    assert message["scope2_g"] is None
    assert message["scope3_g"] is None
    assert message["avoided_co2e_g"] is None
    assert message["energy"] is None
    assert message["routing"] is None


def test_assistant_message_carries_grounding():
    # Null when the engine never checked (the common case, and every run
    # before this column existed).
    assert _assistant_message(_run())["grounding"] is None

    verdict = {
        "checked": True,
        "status": "repaired",
        "attempts": 1,
        "unsupported": [],
        "first_unsupported": ["58", "2021"],
    }
    run = _run(grounding=verdict)
    assert _assistant_message(run)["grounding"] == verdict


def test_assistant_message_reports_failure_status_and_error_as_content():
    run = _run(status="failed", error="boom", messages=[])
    message = _assistant_message(run)
    assert message["status"] == "failed"
    assert message["content"] == "(run failed: boom)"


def test_assistant_message_summarizes_delegated_tool_activity():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"name": "run_harness_task", "arguments": {"task_type": "evidence_extraction"}}
                ],
            }
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [
        {"tool": "run_harness_task", "summary": "delegated evidence_extraction"}
    ]


# ── A1: a failed turn must not surface a prior turn's reply ────────────────
# `run.messages` begins with the injected `_history` prefix (`send_message`
# seeds `task_input["_history"]`; the engine puts it straight onto the front
# of `run.messages` — see `engine/harness.py`). Runs 3ccb2fd2 and 8474f908
# on cloud both failed before writing anything of their own and surfaced the
# *previous* turn's reply as if it were this run's.
_HIST_TURN_1 = [
    {"role": "user", "content": "first question", "tool_calls": [], "tool_call_id": None, "meta": {}},
    {"role": "assistant", "content": "OLD", "tool_calls": [], "tool_call_id": None, "meta": {}},
]


def test_assistant_message_does_not_leak_a_prior_turns_reply_into_a_failed_run():
    run = _run(
        status="failed",
        error="boom",
        task_input={"message": "new question", "_history": _HIST_TURN_1, "_capabilities": ""},
        messages=[
            *_HIST_TURN_1,
            {"role": "user", "content": "new question", "tool_calls": [], "tool_call_id": None, "meta": {}},
        ],
    )
    message = _assistant_message(run)
    assert message["content"] == "(run failed: boom)"
    assert "OLD" not in message["content"]


def test_assistant_message_uses_this_runs_own_text_after_the_history_prefix():
    run = _run(
        status="completed",
        task_input={"message": "new question", "_history": _HIST_TURN_1, "_capabilities": ""},
        messages=[
            *_HIST_TURN_1,
            {"role": "user", "content": "new question", "tool_calls": [], "tool_call_id": None, "meta": {}},
            {"role": "assistant", "content": "NEW", "tool_calls": [], "tool_call_id": None, "meta": {}},
        ],
    )
    message = _assistant_message(run)
    assert message["content"] == "NEW"


def test_assistant_message_accounts_for_a_trimmed_history_prefix():
    """`trim_history` (engine/compaction.py) can drop the oldest turns off the
    front of `_history` before it ever reaches `run.messages` — the skip
    count must shrink by exactly what was dropped (`run.compactions`), or it
    runs past the start of this run's own (shorter, post-trim) messages."""
    full_history = [
        {"role": "user", "content": "turn 1", "tool_calls": [], "tool_call_id": None, "meta": {}},
        {"role": "assistant", "content": "OLD 1", "tool_calls": [], "tool_call_id": None, "meta": {}},
        {"role": "user", "content": "turn 2", "tool_calls": [], "tool_call_id": None, "meta": {}},
        {"role": "assistant", "content": "OLD 2", "tool_calls": [], "tool_call_id": None, "meta": {}},
    ]
    run = _run(
        status="completed",
        task_input={"message": "new question", "_history": full_history, "_capabilities": ""},
        compactions=[{"kind": "history_trim", "iteration": 0, "dropped_history_turns": 2}],
        messages=[
            *full_history[2:],  # the engine dropped the oldest turn before recording
            {"role": "user", "content": "new question", "tool_calls": [], "tool_call_id": None, "meta": {}},
            {"role": "assistant", "content": "NEW", "tool_calls": [], "tool_call_id": None, "meta": {}},
        ],
    )
    message = _assistant_message(run)
    assert message["content"] == "NEW"


# ── A2: an empty lookup/search must be visible in the activity pill ────────


def test_assistant_message_flags_an_empty_dataset_lookup():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"id": "call_1", "name": "lookup_dataset", "arguments": {"dataset": "hazard_scores"}}
                ],
            },
            {
                "role": "tool",
                "content": "No rows in 'hazard_scores' match {\"score\": 999}.",
                "tool_call_id": "call_1",
                "meta": {"error": False},
            },
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [{"tool": "lookup_dataset", "summary": "no rows matched"}]


def test_assistant_message_flags_empty_document_and_connected_source_searches():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {"id": "call_1", "name": "search_documents", "arguments": {"query": "x"}},
                    {"id": "call_2", "name": "search_connected_files", "arguments": {"query": "y"}},
                ],
            },
            {
                "role": "tool",
                "content": "No matches for 'x' in the attached documents.",
                "tool_call_id": "call_1",
                "meta": {"error": False},
            },
            {
                "role": "tool",
                "content": "No connected-source matches for 'y'.",
                "tool_call_id": "call_2",
                "meta": {"error": False},
            },
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [
        {"tool": "search_documents", "summary": "no matches"},
        {"tool": "search_connected_files", "summary": "no matches"},
    ]


def test_assistant_message_flags_a_tool_error():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [{"id": "call_1", "name": "lookup_dataset", "arguments": {}}],
            },
            {
                "role": "tool",
                "content": "Unknown dataset 'nope'.",
                "tool_call_id": "call_1",
                "meta": {"error": True},
            },
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [{"tool": "lookup_dataset", "summary": "error"}]


def test_assistant_message_keeps_delegation_summary_over_an_empty_result():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "run_harness_task",
                        "arguments": {"task_type": "evidence_extraction"},
                    }
                ],
            },
            {
                "role": "tool",
                "content": "No rows in 'x' match {}.",
                "tool_call_id": "call_1",
                "meta": {"error": False},
            },
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [
        {"tool": "run_harness_task", "summary": "delegated evidence_extraction"}
    ]


def test_assistant_message_leaves_a_successful_lookup_summary_blank():
    run = _run(
        messages=[
            {
                "role": "assistant",
                "content": "done",
                "tool_calls": [{"id": "call_1", "name": "lookup_dataset", "arguments": {}}],
            },
            {
                "role": "tool",
                "content": "3 rows: [...]",
                "tool_call_id": "call_1",
                "meta": {"error": False},
            },
        ]
    )
    message = _assistant_message(run)
    assert message["activity"] == [{"tool": "lookup_dataset", "summary": ""}]


# ── send_message's pack resolution (a harness may link more than one pack) ──
HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


class _NoopEngine:
    async def execute(self, run_id):  # pragma: no cover - never actually asserted on
        return None


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
async def seed(session_factory):
    async def _seed(*rows):
        async with session_factory() as db:
            db.add_all(rows)
            await db.commit()

    return _seed


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    # The engine is never actually invoked here — these tests only assert on
    # the Run row `send_message` creates before the background task starts.
    monkeypatch.setattr(chat_api, "get_harness_engine", lambda: _NoopEngine())

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(chat_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_user(email: str) -> User:
    return User(
        id=uuid.uuid4(),
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role="analyst",
    )


def make_workspace(name: str) -> Workspace:
    return Workspace(id=uuid.uuid4(), name=name, kind="team")


def make_member(user: User, workspace: Workspace, *, role: str) -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_pack(workspace: Workspace, *, slug: str, task_types: list[dict] | None = None) -> Pack:
    return Pack(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        slug=slug,
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={
            "pack": slug,
            "version": "1.0.0",
            "display_name": slug.title(),
            "task_types": task_types or [],
        },
        source_path=f"/tmp/{slug}",
    )


async def login_(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def test_a_chat_turn_on_a_pack_linked_harness_resolves_to_the_primary_pack(
    client, seed, session_factory
):
    """A pack-linked chat harness loads that pack's doctrine into the turn —
    deliberate (see chat.py::send_message's comment): `resolve_pack_for_task`
    falls back to the primary (first-linked) pack for task_type "chat"."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    pack = make_pack(team, slug="pack-a")
    await seed(team, project, user, make_member(user, team, role="analyst"), pack)

    async with session_factory() as db:
        harness = Harness(
            workspace_id=team.id,
            name="Pack Chat",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(harness)
        await db.flush()
        await set_harness_packs(db, harness, [pack.id])
        await db.commit()
        harness_id = harness.id

    await login_(client, user.email)
    created = await client.post("/api/chat", json={"harness_id": str(harness_id)})
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]

    sent = await client.post(
        f"/api/chat/{conversation_id}/messages", json={"text": "Hello"}
    )
    assert sent.status_code == 200, sent.text
    run_id = uuid.UUID(sent.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.pack_id == pack.id


async def test_a_chat_turn_on_the_default_pack_less_harness_leaves_pack_id_none(
    client, seed, session_factory
):
    """A chat harness with no linked packs — built directly here rather than
    through workspace seeding, which by default links every installed pack
    (see test_workspace_service.py) — must resolve to `pack_id=None`,
    byte-identical to before a harness could link any pack at all."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    await seed(team, project, user, make_member(user, team, role="analyst"))

    async with session_factory() as db:
        harness = Harness(
            workspace_id=team.id,
            name="Chat Assistant",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(harness)
        await db.commit()
        harness_id = harness.id

    await login_(client, user.email)
    created = await client.post("/api/chat", json={"harness_id": str(harness_id)})
    assert created.status_code == 200, created.text
    conversation_id = created.json()["id"]

    sent = await client.post(
        f"/api/chat/{conversation_id}/messages", json={"text": "Hello"}
    )
    assert sent.status_code == 200, sent.text
    run_id = uuid.UUID(sent.json()["run_id"])

    async with session_factory() as db:
        run = await db.get(Run, run_id)
    assert run.pack_id is None


async def test_an_archived_harness_id_falls_back_to_the_default_chat_harness(
    client, seed, session_factory
):
    """An archived harness id on POST /api/chat reads as unknown: the
    conversation lands on the default chat harness instead. The composer
    persists its harness choice in localStorage, so a stale id from a
    since-archived harness is an expected input, and POST /api/runs already
    refuses archived harnesses — chat must not honor them either."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    user = make_user("analyst@example.com")
    await seed(team, project, user, make_member(user, team, role="analyst"))

    async with session_factory() as db:
        default_chat = Harness(
            workspace_id=team.id,
            name="Chat Assistant",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        archived = Harness(
            workspace_id=team.id,
            name="Retired Specialist",
            task_profile="freeform",
            model_policy={"mode": "auto"},
            tool_names=[],
            is_archived=True,
        )
        db.add_all([default_chat, archived])
        await db.commit()
        default_id, archived_id = default_chat.id, archived.id

    await login_(client, user.email)
    created = await client.post("/api/chat", json={"harness_id": str(archived_id)})
    assert created.status_code == 200, created.text
    assert created.json()["harness_id"] == str(default_id)


# ── _capability_catalog excludes the chat front door as a delegation target ──


async def test_capability_catalog_lists_a_task_type_only_under_its_specialist_harness(
    session_factory, seed
):
    """A pack linked to both the seeded Chat Assistant (every pack, by
    default — see test_workspace_service.py) and a specialist harness must
    show up in the catalog only under the specialist — never also as
    `[harness: Chat Assistant]`, which would advertise chat itself as a
    delegation target `run_harness_task` refuses to honor."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    pack = make_pack(
        team,
        slug="climate-risk",
        task_types=[{"slug": "assess_risk", "display_name": "Assess Risk"}],
    )
    await seed(team, project, pack)

    async with session_factory() as db:
        chat_harness = Harness(
            workspace_id=team.id,
            name="Chat Assistant",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        specialist = Harness(
            workspace_id=team.id,
            name="Climate Analyst",
            task_profile="assess_risk",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add_all([chat_harness, specialist])
        await db.flush()
        await set_harness_packs(db, chat_harness, [pack.id])
        await set_harness_packs(db, specialist, [pack.id])
        await db.commit()

    async with session_factory() as db:
        catalog = await _capability_catalog(db, team.id, project.id)

    assert "[harness: Climate Analyst]" in catalog
    assert "[harness: Chat Assistant]" not in catalog


async def test_capability_catalog_with_only_a_chat_harness_reports_no_specialist_tasks(
    session_factory, seed
):
    """A workspace whose only harness is the chat front door reports no
    delegatable task types, even though the chat harness itself is linked to
    a pack that declares one."""
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    pack = make_pack(
        team, slug="climate-risk", task_types=[{"slug": "assess_risk", "display_name": "Assess Risk"}]
    )
    await seed(team, project, pack)

    async with session_factory() as db:
        chat_harness = Harness(
            workspace_id=team.id,
            name="Chat Assistant",
            task_profile="chat",
            model_policy={"mode": "auto"},
            tool_names=[],
        )
        db.add(chat_harness)
        await db.flush()
        await set_harness_packs(db, chat_harness, [pack.id])
        await db.commit()

    async with session_factory() as db:
        catalog = await _capability_catalog(db, team.id, project.id)

    assert "(no specialist tasks installed)" in catalog
