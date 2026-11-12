"""`start_batch` / `batch_status` / `batch_results` (engine/batch_tools.py), the
parent-run cost roll-up (`core.rollup_parent_cost`) and the harness seeding that
offers the tools. Built on the world/client fixtures of `test_collections_api.py`
and the `PromptEngine` of `test_batches_prompt.py`; the tools are called with a
`RunContext` over a fresh session on the client's loop."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import json
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tests.test_batches_prompt import PromptEngine, engine  # noqa: F401 - fixtures
from tests.test_collections_api import REGISTER, client, run_db, settle, world  # noqa: F401 - fixtures
from tret.batches import core
from tret.config import get_settings
from tret.db.models import Base, Batch, Document, Harness, Project, Run, User, Workspace, WorkspaceMember
from tret.engine import delegation
from tret.engine.batch_tools import BATCH_TOOLS, batch_results, batch_status, start_batch
from tret.engine.harness import _subagent_tool_names
from tret.engine.tools import RunContext, ToolError, get_builtin_tools
from tret.services import collections as service
from tret.services.workspace import COLLECTION_HARNESS_LOOP_CONFIG, seed_chat_harness

ROWS = [{"company": "Acme", "country": "DE"}, {"company": "Globex", "country": "FR"}]
TEMPLATE = "Assess {company} in {country}."


class CostEngine(PromptEngine):
    """PromptEngine plus a per-label cost on the run."""

    def __init__(self, factory):
        super().__init__(factory)
        self.costs: dict[str, str] = {}

    async def execute(self, run_id):
        await super().execute(run_id)
        async with self.factory() as db:
            run = await db.get(Run, run_id)
            label = run.task_input["_label"].split(":", 1)[1]
            run.cost_usd = Decimal(self.costs.get(label, "0"))
            await db.commit()


@pytest.fixture
def cost_engine(world, monkeypatch, tmp_path):
    monkeypatch.setattr(get_settings(), "storage_dir", str(tmp_path / "storage"))
    eng = CostEngine(world.factory)
    monkeypatch.setattr(service, "get_harness_engine", lambda: eng)
    return eng


async def _parent(db, world, *, role="approver", user=None, **task_input) -> Run:
    user = user or world.user
    await db.merge(WorkspaceMember(user_id=user.id, workspace_id=world.ws.id, role=role))
    run = Run(
        id=uuid.uuid4(), project_id=world.project.id, harness_id=world.harness.id, task_type="chat",
        task_input={"message": "go", **task_input}, document_ids=[], created_by=user.id,
        status="running",
    )
    db.add(run)
    await db.commit()
    return run


def _ctx(db, world, run, workspace_id=None) -> RunContext:
    return RunContext(
        db=db, run_id=run.id, project_id=world.project.id, pack_id=None, doctrine_sha=None,
        model_used=None, document_ids=[], output_schemas={},
        workspace_id=workspace_id or world.ws.id,
    )


def call(client, world, tool, *, workspace_id=None, parent=None, **kwargs):
    """Run `tool(ctx, **kwargs)` inside the client's loop; returns (result, parent_id)."""

    async def go(db):
        run = parent or await _parent(db, world)
        out = await tool(_ctx(db, world, run, workspace_id), **kwargs)
        return out, run.id

    return run_db(client, world, go)


def start(client, world, **over):
    kwargs = {"name": "Supplier screen", "template": TEMPLATE, "rows": ROWS, **over}
    out, parent_id = call(client, world, start_batch, **kwargs)
    return json.loads(out), parent_id


# ── start_batch ──────────────────────────────────────────────────────────────
def test_start_batch_creates_and_starts_a_prompt_batch_for_the_calling_run(client, world, engine):
    out, parent_id = start(client, world)
    assert set(out) == {"batch_id", "name", "profile", "targets", "status", "estimated_cost_usd", "page", "advice"}
    assert out["profile"] == "prompt"
    assert out["targets"] == 2 and out["status"] == "running" and out["name"] == "Supplier screen"
    assert out["page"] == f"/batches/{out['batch_id']}" and out["estimated_cost_usd"] is None
    settle(client)
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["profile"] == "prompt" and d["status"] == "completed"
    assert d["parent_run_id"] == str(parent_id)
    assert d["harness_id"] == str(world.harness.id)  # the caller's own harness
    assert d["settings"]["concurrency"] == 3 and d["settings"]["review_schema"] is None
    # Two targets launch concurrently; which one reaches the engine first is
    # scheduling, not behaviour.
    assert sorted(engine.messages) == ["Assess Acme in DE.", "Assess Globex in FR."]
    assert d["created_by"] == str(world.user.id)


def test_start_batch_advice_is_null_without_evidence_and_never_fails_the_tool(client, world, engine, monkeypatch):
    out, _ = start(client, world)
    assert out["advice"] is None
    settle(client)
    from tret.batches import advice

    async def boom(*a, **k):
        raise RuntimeError("advice exploded")

    monkeypatch.setattr(advice, "batch_advice", boom)
    out, _ = start(client, world, name="Second")
    assert out["advice"] is None and out["status"] == "running"


def test_start_batch_returns_advice_computed_before_the_batch_is_counted(client, world, engine, monkeypatch):
    from tret.batches import advice

    seen = []

    async def fake(db, workspace_id, **kw):
        from tret.db.models import Batch

        seen.append((kw["profile"], kw["template"], len((await db.execute(select(Batch))).scalars().all())))
        return {
            "recommendation": {
                "model": "m/x", "reason": "because", "max_iterations": 13, "cost_cap_usd": 0.06,
                "concurrency": 2, "max_iterations_basis": "b", "cost_cap_basis": "c", "concurrency_reason": "r",
            }
        }

    monkeypatch.setattr(advice, "batch_advice", fake)
    out, _ = start(client, world)
    assert out["advice"] == {
        "recommended_model": "m/x", "max_iterations": 13, "cost_cap_usd": 0.06, "concurrency": 2, "reason": "because",
    }
    assert seen == [("prompt", TEMPLATE, 0)]  # before this batch exists


def test_start_batch_passes_max_cost_usd_and_row_memory_through(client, world, engine):
    out, _ = start(client, world, max_cost_usd=0.4, row_memory=True)
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["settings"]["max_cost_usd"] == 0.4 and d["settings"]["row_memory"] is True
    out, _ = start(client, world, name="Plain")
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["settings"]["max_cost_usd"] is None and d["settings"]["row_memory"] is False


def test_start_batch_estimates_from_the_harness_cap_and_passes_options(client, world, engine):
    async def cap(db):
        h = await db.get(Harness, world.harness.id)
        h.loop_config = {**(h.loop_config or {}), "max_cost_usd": 0.5}
        await db.commit()

    run_db(client, world, cap)
    out, _ = start(client, world, review_schema="any", concurrency=1, first_batch_size=1, label_column="country")
    assert out["estimated_cost_usd"] == pytest.approx(1.0)
    settle(client)
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["settings"]["review_schema"] == "any" and d["settings"]["label_column"] == "country"
    assert d["status"] == "gate"  # first_batch_size=1 holds the second row


def test_start_batch_reads_a_csv_document(client, world, engine, tmp_path):
    path = tmp_path / "rows.csv"
    path.write_bytes("company,country\nAcme,DE\nGlobex,FR\nInitech,US\n".encode())

    async def add_doc(db):
        doc = Document(
            id=uuid.uuid4(), project_id=world.project.id, filename="rows.csv", content_type="text/csv",
            byte_size=path.stat().st_size, storage_path=str(path), sha256="0" * 64,
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)

    doc_id = run_db(client, world, add_doc)
    out, _ = start(client, world, rows=None, document_id=doc_id)
    assert out["targets"] == 3
    settle(client)
    assert len(engine.messages) == 3


def test_start_batch_document_from_another_workspace_or_missing_is_refused(client, world, engine, tmp_path):
    path = tmp_path / "x.csv"
    path.write_bytes(b"company\nA\n")

    async def add_foreign(db):
        other = Project(id=uuid.uuid4(), workspace_id=world.other_ws.id, name="O")
        db.add(other)
        await db.flush()
        doc = Document(
            id=uuid.uuid4(), project_id=other.id, filename="x.csv", content_type="text/csv",
            byte_size=10, storage_path=str(path), sha256="0" * 64,
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)

    foreign = run_db(client, world, add_foreign)
    for doc_id in (foreign, str(uuid.uuid4())):
        with pytest.raises(ToolError, match="Document not found"):
            start(client, world, rows=None, document_id=doc_id)


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"template": "Hi {company} {region}"}, "region"),
        ({"rows": None}, "exactly one of rows or document_id"),
        ({"document_id": str(uuid.uuid4())}, "exactly one of rows or document_id"),
        ({"rows": []}, "rows"),
        ({"harness_id": str(uuid.uuid4())}, "harness_id"),
        ({"harness_id": "nope"}, "not a valid harness id"),
        ({"label_column": "nope"}, "label_column"),
        ({"review_schema": "has space"}, "review_schema"),
        ({"max_iterations": 0}, "max_iterations"),
    ],
)
def test_start_batch_validation_errors_become_tool_errors(client, world, engine, over, fragment):
    with pytest.raises(ToolError) as e:
        start(client, world, **over)
    assert fragment in str(e.value)
    assert client.get("/api/batches").json()["batches"] == []


def test_start_batch_refuses_inside_a_batch_run(client, world, engine):
    async def go(db):
        parent = await _parent(db, world, _collection_target_id=str(uuid.uuid4()))
        return await start_batch(_ctx(db, world, parent), name="Nested", template=TEMPLATE, rows=ROWS)

    with pytest.raises(ToolError, match="cannot start another batch"):
        run_db(client, world, go)


def test_a_headroom_409_leaves_a_draft_and_says_so(client, world, engine, monkeypatch):
    monkeypatch.setattr(get_settings(), "batches_min_free_mb_per_target", 10**9)
    with pytest.raises(ToolError) as e:
        start(client, world)
    assert "was created" in str(e.value) and "not started" in str(e.value)
    assert "Not enough free space" in str(e.value)
    (row,) = client.get("/api/batches").json()["batches"]
    assert row["status"] == "draft"


# ── start_batch, disclosure profile ─────────────────────────────────────────
def start_disclosure(client, world, **over):
    kwargs = {
        "name": "Stage one", "profile": "disclosure", "register_csv": REGISTER,
        "target_slug": "test-output", "run_slug": "2026-11-06-stage-one", **over,
    }
    out, parent_id = call(client, world, start_batch, **kwargs)
    return json.loads(out), parent_id


def test_disclosure_from_register_csv_creates_a_collector_batch_for_the_calling_run(client, world, engine):
    async def cap(db):
        h = await db.get(Harness, world.harness.id)
        h.loop_config = dict(COLLECTION_HARNESS_LOOP_CONFIG)
        await db.commit()

    run_db(client, world, cap)
    out, parent_id = start_disclosure(client, world)
    assert out["profile"] == "disclosure" and out["targets"] == 5
    assert out["estimated_cost_usd"] == pytest.approx(8.0 * 5)
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["profile"] == "disclosure" and d["parent_run_id"] == str(parent_id)
    assert d["run_slug"] == "2026-11-06-stage-one"
    assert d["settings"]["target_slug"] == "test-output" and d["settings"]["write_root"] == "runs"
    assert d["settings"]["fiscal_year"] == 2025 and d["settings"]["auto_write"] is True
    assert d["settings"]["concurrency"] == 3
    # The register breaks the briefing shape: the count equals what the batch stored.
    assert out["warnings_count"] == len(d["warnings"]) > 0
    assert out["warnings"] == d["warnings"][:5]
    settle(client)


def test_disclosure_without_a_collector_harness_leaves_no_batch(client, world, engine):
    async def archive(db):
        h = await db.get(Harness, world.harness.id)
        h.is_archived = True
        await db.commit()

    run_db(client, world, archive)
    with pytest.raises(ToolError, match="no Disclosure Collector harness"):
        start_disclosure(client, world)
    assert client.get("/api/batches").json()["batches"] == []


def test_disclosure_defaults_the_run_slug_and_passes_options(client, world, engine):
    out, _ = start_disclosure(
        client, world, run_slug=None, name="Stage 1: 48 Companies!", write_root="out",
        fiscal_year=2024, auto_write=False, concurrency=2,
    )
    d = client.get(f"/api/batches/{out['batch_id']}").json()
    assert d["run_slug"].endswith("-stage-1-48-companies")
    assert len(d["run_slug"].split("-")[0]) == 4
    assert d["settings"]["write_root"] == "out" and d["settings"]["fiscal_year"] == 2024
    assert d["settings"]["auto_write"] is False and d["settings"]["concurrency"] == 2
    settle(client)


def test_disclosure_from_a_register_document(client, world, engine, tmp_path):
    path = tmp_path / "register.csv"
    path.write_bytes(REGISTER.encode())

    async def add_doc(db):
        doc = Document(
            id=uuid.uuid4(), project_id=world.project.id, filename="register.csv", content_type="text/csv",
            byte_size=path.stat().st_size, storage_path=str(path), sha256="0" * 64,
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)

    doc_id = run_db(client, world, add_doc)
    out, _ = start_disclosure(client, world, register_csv=None, register_document_id=doc_id)
    assert out["targets"] == 5 and out["profile"] == "disclosure"
    settle(client)


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"target_slug": None}, "target_slug: required"),
        ({"register_csv": None}, "exactly one of register_document_id or register_csv"),
        ({"register_document_id": str(uuid.uuid4())}, "exactly one of register_document_id or register_csv"),
        ({"register_csv": None, "register_document_id": str(uuid.uuid4())}, "Document not found"),
        ({"target_slug": "elsewhere"}, "allowed: test-output, disclosures"),
        ({"run_slug": "Bad Slug"}, "run_slug"),
        ({"template": TEMPLATE}, "template belongs to the 'prompt' profile"),
        ({"rows": ROWS}, "rows belongs to the 'prompt' profile"),
        ({"review_schema": "any"}, "review_schema belongs to the 'prompt' profile"),
    ],
)
def test_disclosure_validation_errors_are_tool_errors_before_any_write(client, world, engine, over, fragment):
    with pytest.raises(ToolError) as e:
        start_disclosure(client, world, **over)
    assert fragment in str(e.value)
    assert client.get("/api/batches").json()["batches"] == []


def test_disclosure_only_params_are_refused_for_a_prompt_batch(client, world, engine):
    for over, fragment in (
        ({"target_slug": "test-output"}, "target_slug belongs to the 'disclosure' profile"),
        ({"register_csv": REGISTER}, "register_csv belongs to the 'disclosure' profile"),
        ({"run_slug": "abc-def"}, "run_slug belongs to the 'disclosure' profile"),
        ({"auto_write": True}, "auto_write belongs to the 'disclosure' profile"),
    ):
        with pytest.raises(ToolError) as e:
            start(client, world, **over)
        assert fragment in str(e.value)
    assert client.get("/api/batches").json()["batches"] == []


def test_a_duplicate_disclosure_run_slug_says_to_pass_another(client, world, engine):
    start_disclosure(client, world)
    with pytest.raises(ToolError) as e:
        start_disclosure(client, world)
    assert "already exists" in str(e.value) and "Pass another run_slug" in str(e.value)
    settle(client)


def test_an_analyst_cannot_start_a_disclosure_batch(client, world, engine):
    async def go(db):
        parent = await _parent(db, world, role="analyst")
        return await start_batch(
            _ctx(db, world, parent), name="X", profile="disclosure", register_csv=REGISTER,
            target_slug="test-output",
        )

    with pytest.raises(ToolError, match="approver role"):
        run_db(client, world, go)
    assert client.get("/api/batches").json()["batches"] == []


def test_the_spec_describes_the_disclosure_profile():
    spec = get_builtin_tools()["start_batch"]
    assert "profile 'disclosure' is the Kith disclosure collection" in spec.description
    assert "ask for its slug if the user does not name one" in spec.description


# ── batch_status / batch_results ─────────────────────────────────────────────
def test_batch_status_and_results_shapes(client, world, engine):
    engine.behaviour["row-2"] = "fail"
    out, _ = start(client, world)
    settle(client)
    bid = out["batch_id"]
    status = json.loads(call(client, world, batch_status, batch_id=bid)[0])
    assert status["batch_id"] == bid and status["name"] == "Supplier screen"
    assert status["status"] == "completed"
    assert status["counts"]["completed"] == 1 and status["counts"]["failed"] == 1
    assert status["cost_usd"] == 0 and status["pending_reviews"] == []
    assert status["page"] == f"/batches/{bid}"

    res = json.loads(call(client, world, batch_results, batch_id=bid)[0])
    assert res["total"] == 2 and res["offset"] == 0
    first, second = res["rows"]
    assert set(first) == {"position", "label", "status", "answer", "finding", "cost_usd", "error"}
    assert first["label"] == "Acme" and first["status"] == "completed"
    assert first["answer"].startswith("Answer for row-1")
    assert second["status"] == "failed" and second["error"]

    failed = json.loads(call(client, world, batch_results, batch_id=bid, status="failed")[0])
    assert failed["total"] == 1 and failed["rows"][0]["label"] == "Globex"
    paged = json.loads(call(client, world, batch_results, batch_id=bid, offset=1, limit=1)[0])
    assert paged["total"] == 2 and [r["label"] for r in paged["rows"]] == ["Globex"]


def test_batch_status_lists_pending_reviews_and_results_truncate_answers(client, world, engine):
    engine.behaviour["row-1"] = "finding"
    out, _ = start(client, world, review_schema="any")
    settle(client)
    bid = out["batch_id"]
    status = json.loads(call(client, world, batch_status, batch_id=bid)[0])
    assert status["status"] == "review"
    (pending,) = status["pending_reviews"]
    assert pending["target_label"] == "Acme" and pending["schema_slug"] == "connected_write"
    assert pending["finding_id"]

    async def long_answer(db):
        t = (await core.load_targets(db, [uuid.UUID(bid)]))[0]
        t.outcome = {**(t.outcome or {}), "answer": "y" * 5000}
        await db.commit()

    run_db(client, world, long_answer)
    res = json.loads(call(client, world, batch_results, batch_id=bid)[0])
    assert len(res["rows"][0]["answer"]) == 2000
    assert res["rows"][0]["finding"]["schema_slug"] == "connected_write"


def test_other_workspaces_and_bad_ids_are_not_found(client, world, engine):
    out, _ = start(client, world)
    settle(client)
    for tool in (batch_status, batch_results):
        with pytest.raises(ToolError, match="Batch not found"):
            call(client, world, tool, workspace_id=world.other_ws.id, batch_id=out["batch_id"])
        with pytest.raises(ToolError, match="not a valid batch id"):
            call(client, world, tool, batch_id="nope")
        with pytest.raises(ToolError, match="Batch not found"):
            call(client, world, tool, batch_id=str(uuid.uuid4()))


# ── registration and who is offered the tools ────────────────────────────────
def test_the_tools_are_registered_and_never_offered_to_subagents():
    assert set(BATCH_TOOLS) <= set(get_builtin_tools())
    assert not set(BATCH_TOOLS) & delegation.SUBAGENT_ALLOWED_TOOLS
    assert not set(BATCH_TOOLS) & delegation.DELEGATION_TOOLS
    narrowed = _subagent_tool_names(
        [*BATCH_TOOLS, "read_document"], {delegation.ALLOWED_TOOLS_KEY: [*BATCH_TOOLS, "read_document"]},
        delegated=True,
    )
    assert narrowed == ["read_document"]
    assert _subagent_tool_names([*BATCH_TOOLS, "read_document"], {}, delegated=False) == ["read_document"]


def test_the_descriptions_carry_the_batch_vs_delegation_guidance():
    spec = get_builtin_tools()["start_batch"]
    assert "For a few independent items whose results you need now, use delegate_parallel instead." in spec.description
    assert "outlives this run" in spec.description and "batch_status and batch_results" in spec.description
    assert "2000" in get_builtin_tools()["batch_results"].description


@pytest_asyncio.fixture
async def seed_db():
    install_sqlite_type_shims()
    eng = create_async_engine(
        "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(eng, expire_on_commit=False)() as session:
        yield session
    await eng.dispose()


@pytest.fixture(autouse=False)
def _no_packs(monkeypatch):
    monkeypatch.setenv("TRET_SEED_DEFAULT_PACKS", "false")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def _chat_harness(db, tools) -> Harness:
    ws = Workspace(id=uuid.uuid4(), name="Acme", kind="team")
    db.add(ws)
    await db.flush()
    h = Harness(workspace_id=ws.id, name="Chat Assistant", task_profile="chat", tool_names=list(tools))
    db.add(h)
    await db.flush()
    return h


async def test_seeding_offers_batch_tools_with_delegation_and_not_without(seed_db, _no_packs):
    with_delegation = await _chat_harness(seed_db, ["run_harness_task", "read_document"])
    await seed_chat_harness(seed_db, with_delegation.workspace_id)
    await seed_db.refresh(with_delegation)
    assert set(BATCH_TOOLS) <= set(with_delegation.tool_names)

    again = len(with_delegation.tool_names)
    await seed_chat_harness(seed_db, with_delegation.workspace_id)
    await seed_db.refresh(with_delegation)
    assert len(with_delegation.tool_names) == again  # idempotent

    without = await _chat_harness(seed_db, ["read_document"])
    await seed_chat_harness(seed_db, without.workspace_id)
    await seed_db.refresh(without)
    assert not set(BATCH_TOOLS) & set(without.tool_names)


async def test_a_fresh_chat_harness_lists_the_batch_tools(seed_db, _no_packs):
    ws = Workspace(id=uuid.uuid4(), name="Fresh", kind="team")
    seed_db.add(ws)
    await seed_db.flush()
    await seed_chat_harness(seed_db, ws.id)
    chat = (await seed_db.execute(select(Harness).where(Harness.workspace_id == ws.id))).scalars().one()
    assert set(BATCH_TOOLS) <= set(chat.tool_names)
    assert len(chat.tool_names) == len(set(chat.tool_names))


# ── parent-run cost roll-up ──────────────────────────────────────────────────
def _parent_cost(client, world, parent_id) -> Decimal:
    async def go(db):
        run = await db.get(Run, parent_id, populate_existing=True)
        return run.delegated_cost_usd

    return Decimal(str(run_db(client, world, go)))


def _settings(client, world, bid):
    async def go(db):
        return dict((await db.get(Batch, uuid.UUID(bid), populate_existing=True)).settings)

    return run_db(client, world, go)


def _finish_parent(client, world, parent_id, bid):
    """The parent turn ends; the next recompute (or settle) catches the roll-up up."""

    async def go(db):
        (await db.get(Run, parent_id)).status = "completed"
        await db.commit()
        await core._recompute(db, await db.get(Batch, uuid.UUID(bid)))

    run_db(client, world, go)


def test_rollup_waits_for_the_parent_then_catches_up_once(client, world, cost_engine):
    cost_engine.costs = {"row-1": "0.10", "row-2": "0.25"}
    out, parent_id = start(client, world)
    settle(client)
    bid = out["batch_id"]
    # Parent still running: its own cost cap must not see the batch's spend yet.
    assert _parent_cost(client, world, parent_id) == Decimal("0")
    assert "rolled_up_cost_usd" not in _settings(client, world, bid)

    _finish_parent(client, world, parent_id, bid)
    assert _parent_cost(client, world, parent_id) == Decimal("0.35")  # 0.10 + 0.25
    assert _settings(client, world, bid)["rolled_up_cost_usd"] == pytest.approx(0.35)

    async def again(db):
        c = await db.get(Batch, uuid.UUID(bid))
        await core.rollup_parent_cost(db, c)
        await core.rollup_parent_cost(db, c)

    run_db(client, world, again)  # idempotent
    assert _parent_cost(client, world, parent_id) == Decimal("0.35")
    assert client.get(f"/api/batches/{bid}").json()["cost_usd"] == pytest.approx(0.35)


def test_a_retry_rolls_up_only_the_new_spend(client, world, cost_engine):
    cost_engine.costs = {"row-1": "0.10", "row-2": "0.25"}
    cost_engine.behaviour["row-2"] = "fail"
    out, parent_id = start(client, world)
    settle(client)
    bid = out["batch_id"]
    _finish_parent(client, world, parent_id, bid)
    assert _parent_cost(client, world, parent_id) == Decimal("0.35")
    cost_engine.behaviour.pop("row-2")
    cost_engine.costs["row-2"] = "0.07"
    d = client.get(f"/api/batches/{bid}").json()
    tid = d["targets"][1]["id"]
    assert client.post(f"/api/batches/{bid}/targets/{tid}/retry").status_code == 200
    settle(client)
    # 0.10 + 0.25 (archived attempt) + 0.07 = 0.42; only the 0.07 is added.
    assert _parent_cost(client, world, parent_id) == Decimal("0.42")
    assert _settings(client, world, bid)["rolled_up_cost_usd"] == pytest.approx(0.42)


def test_a_batch_without_a_parent_and_a_rollup_failure_never_break_a_settle(client, world, cost_engine, monkeypatch):
    cost_engine.costs = {"row-1": "0.10", "row-2": "0.25"}
    out, parent_id = start(client, world)
    settle(client)
    bid = out["batch_id"]
    _finish_parent(client, world, parent_id, bid)

    async def no_parent(db):
        c = await db.get(Batch, uuid.UUID(bid))
        c.parent_run_id = None
        await db.commit()
        await core.rollup_parent_cost(db, c)

    run_db(client, world, no_parent)
    assert _parent_cost(client, world, parent_id) == Decimal("0.35")

    async def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(core, "load_targets", boom)

    async def failing(db):
        c = await db.get(Batch, uuid.UUID(bid))
        c.parent_run_id = parent_id
        await core.rollup_parent_cost(db, c)  # must not raise

    run_db(client, world, failing)


# ── guards: role, CSV document, draining, once-only seeding ───────────────────
def test_start_batch_needs_the_approver_role_of_the_runs_creator(client, world, engine):
    analyst = User(id=uuid.uuid4(), email="an@example.com", display_name="An Analyst", role="analyst")

    async def go(db):
        db.add(analyst)
        await db.commit()
        parent = await _parent(db, world, role="analyst", user=analyst)
        return await start_batch(_ctx(db, world, parent), name="X", template=TEMPLATE, rows=ROWS)

    with pytest.raises(ToolError, match="needs the approver role"):
        run_db(client, world, go)
    assert client.get("/api/batches").json()["batches"] == []

    async def stranger(db):  # no membership row at all
        parent = await _parent(db, world)
        await db.delete(await db.get(WorkspaceMember, (world.user.id, world.ws.id)))
        await db.commit()
        return await start_batch(_ctx(db, world, parent), name="X", template=TEMPLATE, rows=ROWS)

    with pytest.raises(ToolError, match="needs the approver role"):
        run_db(client, world, stranger)


def test_start_batch_refuses_a_non_csv_or_oversized_document(client, world, engine, tmp_path):
    path = tmp_path / "f.bin"
    path.write_bytes(b"company\nA\n")

    async def add(db, filename, ctype, size):
        doc = Document(
            id=uuid.uuid4(), project_id=world.project.id, filename=filename, content_type=ctype,
            byte_size=size, storage_path=str(path), sha256="0" * 64,
        )
        db.add(doc)
        await db.commit()
        return str(doc.id)

    async def make(db):
        return (
            await add(db, "report.pdf", "application/pdf", 10),
            await add(db, "big.csv", "text/csv", 2_000_001),
            await add(db, "ok.txt", "text/plain", 10),
        )

    pdf, big, ok = run_db(client, world, make)
    with pytest.raises(ToolError, match="does not look like a CSV"):
        start(client, world, rows=None, document_id=pdf)
    with pytest.raises(ToolError, match="at most 2000000 bytes"):
        start(client, world, rows=None, document_id=big)
    out, _ = start(client, world, rows=None, document_id=ok, template="Assess {company}.")  # text/plain is accepted
    assert out["targets"] == 1


def test_start_batch_refuses_while_the_server_is_draining(client, world, engine, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(core, "_app", SimpleNamespace(state=SimpleNamespace(draining=True)))
    with pytest.raises(ToolError, match="retry in a minute"):
        start(client, world)
    assert client.get("/api/batches").json()["batches"] == []


async def test_a_removed_batch_tool_stays_removed_after_reseeding(seed_db, _no_packs):
    h = await _chat_harness(seed_db, ["run_harness_task", "read_document"])
    await seed_chat_harness(seed_db, h.workspace_id)
    await seed_db.refresh(h)
    assert set(BATCH_TOOLS) <= set(h.tool_names)
    h.tool_names = [n for n in h.tool_names if n != "start_batch"]  # a deliberate removal
    await seed_db.flush()
    await seed_chat_harness(seed_db, h.workspace_id)
    await seed_db.refresh(h)
    assert "start_batch" not in h.tool_names


# ── batch_compare ────────────────────────────────────────────────────────────
from tests.test_batch_pairwise import scored, w  # noqa: E402,F401 - fixtures
from tret.engine.batch_tools import BATCH_COMPARE_SEEDED_KEY, BATCH_TOOLS_SEEDED_KEY, batch_compare  # noqa: E402


def _compare_ctx(w, workspace_id=None):
    run = Run(id=uuid.uuid4(), project_id=w.project.id, harness_id=w.harness.id, task_type="chat",
              task_input={}, document_ids=[], status="running")
    return run, RunContext(
        db=w.db, run_id=run.id, project_id=w.project.id, pack_id=None, doctrine_sha=None, model_used=None,
        document_ids=[], output_schemas={}, workspace_id=workspace_id or w.ws.id,
    )


async def test_batch_compare_returns_the_shape_and_the_ten_biggest_gaps(w):
    rows_a = [(f"r{i:02d}", "completed", 50.0, "x") for i in range(12)]
    # |delta| grows with i, alternating sign; the two smallest (i=0, 1) must be cut.
    rows_b = [(f"r{i:02d}", "completed", 50.0 + (i + 1) * (1 if i % 2 else -1), "y") for i in range(12)]
    a, _, _ = await scored(w, "A", "a", rows_a)
    b, _, _ = await scored(w, "B", "b", rows_b)
    _, ctx = _compare_ctx(w)
    out = json.loads(await batch_compare(ctx, batch_a=str(a.id), batch_b=str(b.id)))
    assert set(out) == {"same_family", "quality_comparable", "quality_note", "shared", "only_a", "only_b",
                        "summary", "rows", "page"}
    assert out["same_family"] and out["quality_comparable"] and out["shared"] == 12
    assert out["page"] == f"/batches/compare?a={a.id}&b={b.id}"
    assert set(out["summary"]) == {"wins_a", "wins_b", "ties", "mean_quality_delta", "interval90", "delivered",
                                   "pairwise", "disputed_gaps_a", "disputed_gaps_b", "disputed_primaries_a",
                                   "disputed_primaries_b", "cost_a", "cost_b", "energy_a", "energy_b",
                                   "acceptable_a", "acceptable_b"}
    assert out["summary"]["pairwise"]["judged"] == 0
    assert len(out["rows"]) == 10
    deltas = [abs(r["delta_quality"]) for r in out["rows"]]
    assert deltas == sorted(deltas, reverse=True) and deltas[0] == 12 and deltas[-1] == 3
    assert set(out["rows"][0]) == {"label", "a", "b", "delta_quality"}
    assert set(out["rows"][0]["a"]) == {"status", "quality", "flags"}


async def test_batch_compare_stays_inside_the_workspace_and_refuses_one_batch(w):
    a, _, _ = await scored(w, "A", "a", [("acme", "completed", 70, "x")])
    b, _, _ = await scored(w, "B", "b", [("acme", "completed", 70, "x")])
    _, ctx = _compare_ctx(w)
    with pytest.raises(ToolError, match="Batch not found"):
        await batch_compare(_compare_ctx(w, w.other_ws.id)[1], batch_a=str(a.id), batch_b=str(b.id))
    with pytest.raises(ToolError, match="not a valid batch id"):
        await batch_compare(ctx, batch_a="nope", batch_b=str(b.id))
    with pytest.raises(ToolError, match="different batches"):
        await batch_compare(ctx, batch_a=str(a.id), batch_b=str(a.id))
    unscored = await scored(w, "C", "c", [("acme", "completed", 70, "x")], fy=2024)
    out = json.loads(await batch_compare(ctx, batch_a=str(a.id), batch_b=str(unscored[0].id)))
    assert out["quality_comparable"] is False and out["quality_note"] and out["summary"]["pairwise"] is None


def test_batch_compare_is_registered_like_the_other_batch_tools():
    assert "batch_compare" in BATCH_TOOLS and "batch_compare" in get_builtin_tools()
    assert not {"batch_compare"} & delegation.SUBAGENT_ALLOWED_TOOLS


async def test_batch_compare_is_offered_once_to_existing_chat_harnesses(seed_db, _no_packs):
    old_tools = ["run_harness_task", "start_batch", "batch_status", "batch_results", "read_document"]
    h = await _chat_harness(seed_db, old_tools)
    ws = await seed_db.get(Workspace, h.workspace_id)
    ws.settings = {BATCH_TOOLS_SEEDED_KEY: True}  # the earlier one-time backfill already ran
    await seed_db.flush()
    await seed_chat_harness(seed_db, h.workspace_id)
    await seed_db.refresh(h)
    assert "batch_compare" in h.tool_names
    assert (await seed_db.get(Workspace, h.workspace_id)).settings[BATCH_COMPARE_SEEDED_KEY] is True
    # an operator who then removes it is not re-seeded
    h.tool_names = [t for t in h.tool_names if t != "batch_compare"]
    await seed_db.flush()
    await seed_chat_harness(seed_db, h.workspace_id)
    await seed_db.refresh(h)
    assert "batch_compare" not in h.tool_names


async def test_batch_compare_is_not_offered_to_a_harness_that_dropped_the_batch_tools(seed_db, _no_packs):
    h = await _chat_harness(seed_db, ["run_harness_task", "read_document"])
    ws = await seed_db.get(Workspace, h.workspace_id)
    ws.settings = {BATCH_TOOLS_SEEDED_KEY: True}  # seeded once; the operator removed all three
    await seed_db.flush()
    await seed_chat_harness(seed_db, h.workspace_id)
    await seed_db.refresh(h)
    assert not {"batch_compare", "start_batch"} & set(h.tool_names)
    assert (await seed_db.get(Workspace, h.workspace_id)).settings[BATCH_COMPARE_SEEDED_KEY] is True



async def test_batch_compare_without_comparable_quality_gives_no_quality_and_orders_by_label(w):
    a, _, _ = await scored(w, "A", "a", [("zed", "completed", 90, "x"), ("amy", "completed", 10, "x")])
    other, _, _ = await scored(
        w, "B", "b", [("zed", "completed", 10, "x"), ("amy", "completed", 90, "x")], judge="anthropic/claude-sonnet-5-5"
    )
    _, ctx = _compare_ctx(w)
    out = json.loads(await batch_compare(ctx, batch_a=str(a.id), batch_b=str(other.id)))
    assert out["quality_comparable"] is False
    assert [r["label"] for r in out["rows"]] == ["Amy", "Zed"]
    assert all(r["delta_quality"] is None and r["a"]["quality"] is None and r["b"]["quality"] is None for r in out["rows"])
    assert "label" in get_builtin_tools()["batch_compare"].description


def test_start_batch_advice_failure_in_the_database_leaves_the_tool_working(client, world, engine, monkeypatch):
    from sqlalchemy import text

    from tret.batches import advice

    async def bad(db, workspace_id, **kw):
        await db.execute(text("select * from no_such_table_at_all"))

    monkeypatch.setattr(advice, "batch_advice", bad)
    out, _ = start(client, world)
    assert out["advice"] is None and out["status"] == "running"


def test_row_memory_on_the_prompt_profile_and_exact_message_when_off(client, world, engine):
    first, _ = start(client, world)
    settle(client)
    engine.messages.clear()
    off, _ = start(client, world, name="Off")
    settle(client)
    assert sorted(engine.messages) == ["Assess Acme in DE.", "Assess Globex in FR."]  # byte for byte
    engine.messages.clear()
    on, _ = start(client, world, name="On", row_memory=True)
    settle(client)
    assert len(engine.messages) == 2
    for m in engine.messages:
        base, _, block = m.partition("\n\n")
        assert base in ("Assess Acme in DE.", "Assess Globex in FR.")
        assert block.startswith("EARLIER ATTEMPTS AT THIS TARGET") and "delivered" in block


# ── start_batch with an experiment ────────────────────────────────────────────
def test_start_batch_with_an_experiment_creates_and_starts_every_arm(client, world, engine):
    arms = [{"model_override": "anthropic/claude-sonnet-5"}, {"objective": "eco"}]
    out, _ = start(client, world, experiment={"mode": "replicate", "arms": arms}, objective="balanced")
    assert out["page"] == f"/batches/experiments/{out['experiment_id']}"
    assert [a["key"] for a in out["arms"]] == ["A", "B"] and "batch_id" not in out
    for a in out["arms"]:
        d = client.get(f"/api/batches/{a['batch_id']}").json()
        assert d["status"] in ("running", "completed") and d["experiment"]["id"] == out["experiment_id"]


def test_start_batch_experiment_errors_are_tool_errors_and_plain_batches_are_unchanged(client, world, engine):
    with pytest.raises(ToolError, match="arms"):
        start(client, world, experiment={"mode": "split", "arms": [{"objective": "eco"}]})
    assert client.get("/api/batches/experiments").json() == {"experiments": []}
    out, _ = start(client, world, objective="eco")
    assert "experiment_id" not in out and out["page"].startswith("/batches/")
    assert client.get(f"/api/batches/{out['batch_id']}").json()["settings"]["objective"] == "eco"
