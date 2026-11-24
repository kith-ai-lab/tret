"""Jury preview, the per-pass freeze, routed_json_call with a freeze, judge markers, the API."""
# ruff: noqa: F811 - imported fixtures are used by name as test arguments
from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.extract_fakes import FakeProvider, http_error
from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_outcomes_served_by import _base_run
from tests.test_route_jury import FIVE, Registry, catalog, model, no_router_call  # noqa: F401 - fixture
from tret.api import eval_jury as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.batches.rubrics import JUROR_KEY, SEALED_CALL_KEY, is_judge_run
from tret.config import get_settings
from tret.db.engine import get_db
from sqlalchemy.exc import IntegrityError  # noqa: F401

from tret.db.models import RoutingFreeze, Workspace
from tret.engine.routed_call import CallServices, RoutedCallError, routed_json_call
from tret.evaluation import jury
from tret.router_llm.router import ModelRouter, RoutingDecision
from tret.services.outcomes import build_outcomes

SCHEMA = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}


def svc_for(script=(), models=FIVE):
    provider = FakeProvider(list(script))
    reg = Registry(provider)
    cat = catalog(*models)
    return CallServices(reg, ModelRouter(cat, reg), cat), provider


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(get_settings(), "eval_jury", "on")


def run(w, fn):
    async def go():
        async with w.factory() as db:
            out = await fn(db)
            await db.commit()
            return out

    return asyncio.run(go())


# ── freeze service ──────────────────────────────────────────────────────────────────────────
def test_preview_writes_nothing_and_calls_no_model(world):
    svc, provider = svc_for()
    sel = run(world, lambda db: jury.preview_jury(db, workspace_id=world.ws.id, k=3, services=svc))
    assert len(sel.picks) == 3 and provider.calls == []
    assert run(world, lambda db: db.execute(select(RoutingFreeze))).scalars().all() == []


def test_freeze_is_idempotent_never_repicks_and_release_ends_it(world):
    svc, _ = svc_for()
    first, created = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p1", k=2, services=svc, created_by="a@x"))
    assert created and first.status == "active" and first.k == 2 and first.selection["picks"]
    # the catalog changes between calls, and the repeat carries another policy: the stored pick stands
    svc2, _ = svc_for(models=(model("zzz/new", price="0.01"), *FIVE))
    again, created2 = run(world, lambda db: jury.freeze_jury(
        db, workspace_id=world.ws.id, pass_ref="p1", k=3, model_policy={"objective": "eco"}, services=svc2))
    assert not created2 and again.id == first.id and again.selection == first.selection and again.k == 2
    # another purpose or workspace is another pass
    other, c3 = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p1", purpose="sealed_call", k=1, services=svc))
    assert c3 and other.id != first.id and len(other.selection["picks"]) == 1
    rel = run(world, lambda db: jury.release_freeze(db, workspace_id=world.ws.id, freeze_id=first.id))
    assert rel.status == "released" and rel.released_at is not None and rel.selection == first.selection
    with pytest.raises(jury.JuryError) as e:  # a released pass reference is not reused
        run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p1", k=2, services=svc))
    assert e.value.status == 409
    # a new pass picks freely again
    _, c4 = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p2", k=2, services=svc2))
    assert c4


def test_freeze_validation_and_isolation(world):
    svc, _ = svc_for()
    for kw in ({"k": 0}, {"k": 9}, {"purpose": "nope"}, {"pass_ref": " "}):
        with pytest.raises(jury.JuryError):
            run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, **{"pass_ref": "p", "services": svc, **kw}))
    with pytest.raises(jury.JuryError):  # a pin never freezes
        run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p", model_policy={"mode": "pinned", "model": "a/b"}, services=svc))
    row, _ = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p", k=1, services=svc))
    with pytest.raises(jury.JuryError) as e:
        run(world, lambda db: jury.get_freeze(db, workspace_id=uuid.uuid4(), freeze_id=row.id))
    assert e.value.status == 404
    with pytest.raises(jury.JuryError):
        run(world, lambda db: jury.release_freeze(db, workspace_id=uuid.uuid4(), freeze_id=row.id))


def test_too_few_families_is_a_422_style_error(world):
    svc, _ = svc_for(models=FIVE[:2])
    with pytest.raises(jury.JuryError) as e:
        run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="p", k=3, services=svc))
    assert e.value.status == 422 and "Only 1 distinct model family" in str(e.value)


# ── routed_json_call with a freeze ──────────────────────────────────────────────────────────
async def fcall(svc, freeze, **kw):
    return await routed_json_call(
        None, workspace_id=None, model_policy=None, purpose="t", system="s", prompt="p", schema=SCHEMA,
        est_input_tokens=5000, max_output_tokens=1000, services=svc, freeze=freeze, **kw,
    )


async def test_call_uses_the_frozen_model_without_routing():
    svc, provider = svc_for([{"x": 1}, {"x": 2}])
    sel = await svc.router.route_jury({}, 3)
    fid = uuid.uuid4()
    r = await fcall(svc, sel, juror_index=1, freeze_id=fid, pass_ref="p1")
    assert r.model == sel.picks[1].model and provider.calls[0]["model"] == svc.catalog.get(sel.picks[1].model).wire_id
    assert r.decision.frozen["freeze_id"] == str(fid) and r.decision.frozen["index"] == 1
    assert r.decision.frozen["family"] == sel.picks[1].family and "No routing call was made" in r.decision.reasoning
    assert "frozen" in r.decision.to_json()
    assert r.usage.input_tokens == 1000
    again = await fcall(svc, sel, model=sel.picks[2].model)
    assert again.model == sel.picks[2].model
    assert "frozen" not in RoutingDecision(
        router_model=None, routing_prompt_version="t", candidates=[], chosen_model="m", reasoning="r").to_json()


async def test_a_model_outside_the_freeze_is_refused():
    svc, provider = svc_for([{"x": 1}])
    sel = await svc.router.route_jury({}, 2)
    for kw in ({"model": "openrouter/x-ai/grok-a"}, {"juror_index": 2}, {"juror_index": -1}):
        with pytest.raises(RoutedCallError) as e:
            await fcall(svc, sel, **kw)
        assert e.value.kind == "not_in_freeze"
    assert provider.calls == []


async def test_retry_stays_on_the_frozen_model_and_failure_never_falls_back():
    svc, provider = svc_for([http_error(503), {"x": 1}])
    sel = await svc.router.route_jury({}, 3)
    r = await fcall(svc, sel, juror_index=0)
    assert r.attempts == 2 and {c["model"] for c in provider.calls} == {svc.catalog.get(sel.picks[0].model).wire_id}
    svc, provider = svc_for([http_error(500), http_error(500)])
    with pytest.raises(RoutedCallError) as e:
        await fcall(svc, sel, juror_index=0)
    assert e.value.kind == "provider" and len(provider.calls) == 2
    assert {c["model"] for c in provider.calls} == {svc.catalog.get(sel.picks[0].model).wire_id}


async def test_frozen_model_gone_from_the_catalog_fails_clearly():
    svc, provider = svc_for([{"x": 1}])
    sel = await svc.router.route_jury({}, 2)
    gone, provider = svc_for([{"x": 1}], models=(FIVE[3], FIVE[4]))
    with pytest.raises(RoutedCallError) as e:
        await fcall(gone, sel, juror_index=0)
    assert e.value.kind == "unroutable" and "no other model is substituted" in str(e.value) and provider.calls == []


async def test_pinned_policy_still_refused_with_a_freeze():
    svc, _ = svc_for()
    sel = await svc.router.route_jury({}, 1)
    with pytest.raises(RoutedCallError) as e:
        await routed_json_call(
            None, workspace_id=None, model_policy={"mode": "pinned", "model": "a/b"}, purpose="t", system="s", prompt="p",
            schema=SCHEMA, est_input_tokens=1, services=svc, freeze=sel)
    assert e.value.kind == "pinned_policy"


# ── judge markers ───────────────────────────────────────────────────────────────────────────
def test_is_judge_run_reads_both_markers():
    assert is_judge_run({JUROR_KEY: True}) and is_judge_run({SEALED_CALL_KEY: True})
    assert not is_judge_run({}) and not is_judge_run(None)


@pytest.mark.parametrize("marker", [JUROR_KEY, SEALED_CALL_KEY])
def test_build_outcomes_skips_judge_runs(marker):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import StaticPool

    from tests.evals.golden_world import install_sqlite_type_shims
    from tret.db.models import Base

    install_sqlite_type_shims()

    async def go():
        engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            control = _base_run(model_used="openrouter/openai/gpt-a", task_input={})
            judged = _base_run(model_used="openrouter/openai/gpt-a", task_input={marker: True})
            out = (await build_outcomes(db, control), await build_outcomes(db, judged))
        await engine.dispose()
        return out

    control, judged = asyncio.run(go())
    assert len(control) == 1 and judged == []


# ── API ─────────────────────────────────────────────────────────────────────────────────────
def client(w, state=None, role="approver", svc=None, monkeypatch=None):
    state = state if state is not None else {"ws": w.ws.id}
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with w.factory() as s:
            yield s

    async def _user():
        return w.user

    async def _ws():
        return WorkspaceContext(Workspace(id=state["ws"], name="W", kind="team"), role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    c = TestClient(app)
    c.state = state
    return c


@pytest.fixture
def served(monkeypatch):
    svc, provider = svc_for()

    async def fake(db, workspace_id):
        return svc

    monkeypatch.setattr(jury, "get_call_services", fake)
    return provider


def test_api_is_a_404_while_off(world, monkeypatch, served):
    monkeypatch.setattr(get_settings(), "eval_jury", "off")
    with client(world) as c:
        assert c.post("/api/eval/jury/preview", json={}).status_code == 404
        assert c.post("/api/eval/jury/freezes", json={"pass_ref": "p"}).status_code == 404
        assert c.get(f"/api/eval/jury/freezes/{uuid.uuid4()}").status_code == 404
        assert c.post(f"/api/eval/jury/freezes/{uuid.uuid4()}/release").status_code == 404


def test_api_preview_freeze_get_release_roles_and_isolation(world, on, served):
    with client(world) as c:
        r = c.post("/api/eval/jury/preview", json={"k": 3})
        assert r.status_code == 200
        body = r.json()
        assert len(body["picks"]) == 3 and body["llm_call"] is False and body["picks"][0]["why"]
        assert {"family", "rank", "alternatives"} <= set(body["picks"][0]) and "skipped_families" in body
        assert c.post("/api/eval/jury/preview", json={"k": 9}).status_code == 422
        assert c.post("/api/eval/jury/preview", json={"model_policy": {"mode": "pinned", "model": "a/b"}}).status_code == 422
        assert c.post("/api/eval/jury/preview", json={"k": 5, "model_policy": {"max_cost_tier": "local"}}).status_code == 422
        assert c.post("/api/eval/jury/preview", json={"purpose": "x"}).status_code == 422
        assert c.post("/api/eval/jury/preview", json={"model": "a/b"}).status_code == 422  # a model is never named
        assert served.calls == []

        f = c.post("/api/eval/jury/freezes", json={"pass_ref": "pass-1", "k": 2})
        assert f.status_code == 200 and f.json()["created"] is True and f.json()["status"] == "active"
        fid = f.json()["id"]
        g = c.post("/api/eval/jury/freezes", json={"pass_ref": "pass-1", "k": 3})
        assert g.json()["created"] is False and g.json()["id"] == fid and g.json()["selection"] == f.json()["selection"]
        assert c.get(f"/api/eval/jury/freezes/{fid}").json()["selection"] == f.json()["selection"]

        analyst = client(world, role="analyst")
        assert analyst.post("/api/eval/jury/freezes", json={"pass_ref": "p9"}).status_code == 403
        assert analyst.post(f"/api/eval/jury/freezes/{fid}/release").status_code == 403
        assert analyst.post("/api/eval/jury/preview", json={}).status_code == 200  # previews are open to members
        assert analyst.get(f"/api/eval/jury/freezes/{fid}").status_code == 200

        elsewhere = client(world, state={"ws": uuid.uuid4()})
        assert elsewhere.get(f"/api/eval/jury/freezes/{fid}").status_code == 404
        assert elsewhere.post(f"/api/eval/jury/freezes/{fid}/release").status_code == 404

        rel = c.post(f"/api/eval/jury/freezes/{fid}/release")
        assert rel.status_code == 200 and rel.json()["status"] == "released"
        assert c.post("/api/eval/jury/freezes", json={"pass_ref": "pass-1"}).status_code == 409
        assert c.get(f"/api/eval/jury/freezes/{uuid.uuid4()}").status_code == 404


# ── review fixes ────────────────────────────────────────────────────────────────────────────
async def test_frozen_model_outside_the_calls_own_policy_is_refused():
    svc, provider = svc_for([{"x": 1}])
    sel = await svc.router.route_jury({}, 2)
    for policy in ({"max_cost_tier": "local"}, {"allowed": ["openrouter/x-ai/grok-a"]}, {"open_weights": "require"}):
        with pytest.raises(RoutedCallError) as e:
            await routed_json_call(
                None, workspace_id=None, model_policy=policy, purpose="t", system="s", prompt="p", schema=SCHEMA,
                est_input_tokens=5000, services=svc, freeze=sel, juror_index=0,
            )
        assert e.value.kind == "frozen_outside_policy" and "no other model is substituted" in str(e.value)
    assert provider.calls == []


async def test_frozen_decision_record_is_filled_like_a_routed_one():
    svc, _ = svc_for([{"x": 1}])
    sel = await svc.router.route_jury({}, 2)
    r = await routed_json_call(
        None, workspace_id=None, model_policy={"objective": "eco", "max_cost_tier": "standard"}, purpose="t",
        system="s", prompt="p", schema=SCHEMA, est_input_tokens=5000, services=svc, freeze=sel, juror_index=0,
    )
    d = r.decision
    assert d.task_shape == "extraction" and d.max_cost_tier == "standard" and d.objective == "eco"
    assert d.open_weights and d.effort and d.context_fit["mode"] == "fit" and d.context_fit["required"] == 5000 + 4096
    assert d.routing_prompt_version != sel.ranking_version and d.frozen["ranking_version"] == sel.ranking_version


async def test_model_without_a_freeze_is_an_error_not_ignored():
    svc, provider = svc_for([{"x": 1}])
    with pytest.raises(RoutedCallError) as e:
        await routed_json_call(
            None, workspace_id=None, model_policy=None, purpose="t", system="s", prompt="p", schema=SCHEMA,
            est_input_tokens=1, services=svc, model="openrouter/openai/gpt-a",
        )
    assert e.value.kind == "not_in_freeze" and provider.calls == []


def test_a_concurrent_freeze_of_the_same_pass_returns_the_winner(world, monkeypatch):
    svc, _ = svc_for()
    winner, _ = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="race", k=2, services=svc))
    real = jury.find_freeze
    calls = {"n": 0}

    async def blind_first(db, **kw):  # the pre-check misses the row the other request inserts
        calls["n"] += 1
        return None if calls["n"] == 1 else await real(db, **kw)

    monkeypatch.setattr(jury, "find_freeze", blind_first)
    got, created = run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="race", k=3, services=svc))
    assert not created and got.id == winner.id and got.k == 2
    # and a released winner is a 409, not a 500
    run(world, lambda db: jury.release_freeze(db, workspace_id=world.ws.id, freeze_id=winner.id))
    calls["n"] = 0
    with pytest.raises(jury.JuryError) as e:
        run(world, lambda db: jury.freeze_jury(db, workspace_id=world.ws.id, pass_ref="race", k=3, services=svc))
    assert e.value.status == 409


def test_task_family_is_scoped_to_the_callers_workspace_on_the_server(world, on):
    seen = []

    class Spy:
        async def route_jury(self, policy, k, **kw):
            seen.append(kw["task_family"])
            return await real.route_jury(policy, k, **kw)

    svc, _ = svc_for()
    real = svc.router
    spied = CallServices(svc.registry, Spy(), svc.catalog)
    run(world, lambda db: jury.preview_jury(db, workspace_id=world.ws.id, task_family="disclosure:fy2025", services=spied))
    assert seen == [f"disclosure:fy2025@{world.ws.id}"]
    for bad in ("disclosure:fy2025@" + str(uuid.uuid4()),):
        with pytest.raises(jury.JuryError):  # a caller-supplied workspace suffix is never honoured
            run(world, lambda db: jury.preview_jury(db, workspace_id=world.ws.id, task_family=bad, services=spied))
    with client(world) as c:  # and the HTTP API takes no family at all
        assert c.post("/api/eval/jury/preview", json={"task_family": "x@y"}).status_code == 422
