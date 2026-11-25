"""Shared fixtures for the Engine 2 (credibility) tests: seeded reports in the blob store, a fake judge
provider that answers from a SCORE=n token in the prompt, and services over the fake catalog."""
# ruff: noqa: F811
from __future__ import annotations

import asyncio
import hashlib
import re
import uuid
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.test_batches_core import world  # noqa: F401 - fixture
from tests.test_route_jury import FIVE, Registry, catalog, model, no_router_call  # noqa: F401
from tret.api import eval_credibility as api
from tret.api.auth import current_user
from tret.api.workspace import WorkspaceContext, current_workspace
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import Batch, BatchTarget, DisclosureReport, Project, Workspace
from tret.engine import direct
from tret.engine.routed_call import CallServices
from tret.evaluation import credibility as cr
from tret.evaluation import jury
from tret.providers.base import JsonCompletion, Usage
from tret.router_llm.router import ModelRouter
from tret.storage import get_store, normalised_key, reset_store

COST = Decimal("0.0200")
SCORE = re.compile(r"SCORE=(\d+)")
EXPERIMENT = [
    {"key": "open", "label": "Open", "weight": 1, "model_policy": {"mode": "auto", "open_weights": "require"}},
    {"key": "any", "label": "Any", "weight": 1, "model_policy": {"mode": "auto", "open_weights": "any"}},
]


def answer(score=5):
    return {"credibility_score": score, "summary": "s", "story_breaks": ["b"],
            "pushbacks": [{"pushback": f"p{i}", "evidence": "e"} for i in range(3)]}


class FakeJudge:
    def __init__(self):
        self.calls: list[dict] = []
        self.fail = None
        self.on_call = None
        self.payload = None

    async def complete_json(self, **kw):
        self.calls.append(kw)
        if self.on_call:
            await self.on_call(len(self.calls))
        if self.fail is not None:
            raise self.fail
        m = SCORE.search(kw["prompt"])
        payload = self.payload if self.payload is not None else answer(int(m.group(1)) if m else 5)
        return JsonCompletion(payload=payload, usage=Usage(2000, 300, reported_cost_usd=COST), model=kw["model"], served_by="fake")


def make_env(world, tmp_path, monkeypatch, models=FIVE, **settings):
    s = get_settings()
    base = {"eval_jury": "on", "eval_credibility": True, "eval_credibility_concurrency": 2,
            "storage_dir": str(tmp_path / "storage"), "batches_min_free_mb_per_target": 0,
            # keep unrelated gates out of tests that do not exercise them
            "eval_gate_clumping_min_n": 10_000, "eval_gate_failure_rate_pause": 1.0}
    for k, v in {**base, **settings}.items():
        monkeypatch.setattr(s, k, v)
    reset_store()
    judge = FakeJudge()
    reg = Registry(judge)
    cat = catalog(*models)
    svc = CallServices(reg, ModelRouter(cat, reg), cat)

    async def fake_services(db, workspace_id):
        return svc

    monkeypatch.setattr(direct, "get_call_services", fake_services)
    monkeypatch.setattr(jury, "get_call_services", fake_services)
    monkeypatch.setattr(cr, "get_session_factory", lambda: world.factory)
    return type("Env", (), {"world": world, "judge": judge, "svc": svc, "cat": cat})()


@pytest.fixture
def env(world, tmp_path, monkeypatch):
    yield make_env(world, tmp_path, monkeypatch)
    reset_store()


def run(coro):
    return asyncio.run(coro)


def fetch(world, fn):
    async def go():
        async with world.factory() as db:
            return await fn(db)

    return asyncio.run(go())


def seed_reports(world, texts: list[str], *, status="normalised", primary=True):
    """One report per text, each its own target. Returns (batch id, [report ids])."""

    async def go():
        async with world.factory() as db:
            project = (await db.execute(select(Project).where(Project.workspace_id == world.ws.id))).scalars().first()
            batch = Batch(id=uuid.uuid4(), workspace_id=world.ws.id, project_id=project.id, name="B", run_slug=f"b-{uuid.uuid4().hex[:6]}",
                          profile="disclosure", status="completed", settings={}, warnings=[])
            db.add(batch)
            await db.flush()
            ids = []
            for i, text in enumerate(texts):
                t = BatchTarget(id=uuid.uuid4(), collection_id=batch.id, position=i, slug=f"co{i}", company=f"Company {i}", market="US",
                                domicile_country="US", label=f"Company {i}", status="written", identifiers={}, known_urls=[], finding_ids=[],
                                attempts=1, written_files=[], task_input={})
                db.add(t)
                await db.flush()
                doc = uuid.uuid4()
                key = normalised_key(doc, "1")
                data = text.encode()
                await get_store().put(key, data)
                r = DisclosureReport(project_id=project.id, document_id=doc, batch_target_id=t.id, is_primary=primary, status=status,
                                     markdown_key=key, markdown_sha256=hashlib.sha256(data).hexdigest(), markdown_chars=len(text),
                                     normaliser_version="1", quarantine_reason="garbled" if status == "quarantined" else None)
                db.add(r)
                await db.flush()
                ids.append(r.id)
            await db.commit()
            return batch.id, ids

    return asyncio.run(go())


def body(i, score=5, words=40):
    return f"# Annual report\nUNIQUE{i}TOKEN SCORE={score}. " + " ".join(f"w{j}" for j in range(words))


def create(env, batch_id=None, ids=None, arms=None, **kw):
    async def go():
        async with env.world.factory() as db:
            p = await cr.create_pass(
                db, workspace_id=env.world.ws.id, arms=arms or EXPERIMENT, batch_id=batch_id,
                disclosure_report_ids=ids, created_by="a@example.com", services=env.svc, **kw)
            await db.commit()
            return p.id

    return asyncio.run(go())


def start(env, pid, budget=100, spawn=True):
    async def go():
        async with env.world.factory() as db:
            p = await cr.start_pass(db, workspace_id=env.world.ws.id, pass_id=pid, budget_usd=budget, services=env.svc, spawn=spawn)
            owner = p.lease_owner
        if spawn:
            assert await cr.drain(120)
        return owner

    return asyncio.run(go())


def get(env, pid):
    return fetch(env.world, lambda db: db.get(cr.CredibilityPass, pid))


def reviews(env, pid):
    return fetch(env.world, lambda db: db.execute(select(cr.CredibilityReview).where(cr.CredibilityReview.pass_id == pid))).scalars().all()


def client(env, role="approver"):
    app = FastAPI()
    app.include_router(api.router)

    async def _db():
        async with env.world.factory() as s:
            yield s

    async def _user():
        return env.world.user

    async def _ws():
        return WorkspaceContext(Workspace(id=env.world.ws.id, name="W", kind="team"), role)

    app.dependency_overrides[get_db] = _db
    app.dependency_overrides[current_user] = _user
    app.dependency_overrides[current_workspace] = _ws
    return TestClient(app)
