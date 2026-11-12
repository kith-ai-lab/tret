"""The shadow decision: recorded beside the live pick, never changing it."""
# ruff: noqa: F811 - the imported fixtures are used by name as test arguments
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from tests.test_collections_api import client, run_db, world  # noqa: F401 - fixtures
from tests.test_api_routing_preview import (  # noqa: F401 - fixtures and helpers
    _ALL_KEYS,
    _basic_setup,
    _FakeEngine,
    _FakeRegistry,
    engine,
    login,
    seed,
    session_factory,
)
from tests.test_router_exploration import (
    FIRES_AT_MAX_SEED,
    MAX_EXPLORATION,
    ROUTE_ARGS,
    _AnsweringRegistry,
    _catalog,
    _model,
    _policy,
    _prior,
)
from tret.api import auth
from tret.api import routing as routing_api
from tret.config import get_settings
from tret.db.engine import get_db
from tret.db.models import BatchTarget, Run, RunOutcome
from tret.router_llm import scoring
from tret.router_llm.priors import OutcomePriors, reset_priors_registry
from tret.router_llm.router import ModelRouter

NOW = datetime.now(timezone.utc)
HARNESS = uuid.uuid4()
OBJ = "balanced"


@pytest.fixture(autouse=True)
def _fresh_registry():
    reset_priors_registry()
    yield
    reset_priors_registry()


def _rows(model_id, n=12, ok=True):
    return [
        scoring.AgentRow(
            model_id=model_id, objective=OBJ, observed_at=NOW, outcome_class="delivered" if ok else "failed",
            cost_usd=0.05, energy_wh=None, input_tokens=1000, output_tokens=500, findings_rejected=0,
            is_batch=False, target_class=None,
        )
        for _ in range(n)
    ]


class _Stub:
    """Stub priors with the per-agent record."""

    def __init__(self, priors=None, rows=None, boom=False, cooling=()):
        self.priors, self.rows, self.boom = priors or {}, rows, boom
        self.cooling = set(cooling)
        self.agent_calls = 0

    async def cooldown_for(self, candidate_ids):
        return [{"model": m, "until": "x", "reason": "r"} for m in candidate_ids if m in self.cooling]

    async def for_key(self, *, task_shape, objective, size_band=None):
        return self.priors

    async def for_agent(self, harness_id):
        self.agent_calls += 1
        if self.boom:
            raise RuntimeError("boom")
        return self.rows

    def invalidate(self):
        pass


class _NoAgent:
    """A priors provider that predates `for_agent`."""

    async def for_key(self, *, task_shape, objective, size_band=None):
        return {}

    def invalidate(self):
        pass


def _comparable(d):
    j = d.to_json()
    j.pop("decided_at", None)
    j.pop("latency_ms", None)
    return j


def _router(priors, *models, seed=0, **kw):
    models = models or (_model("alpha"), _model("beta", price="2"))
    return ModelRouter(
        _catalog(*models), _AnsweringRegistry({"openrouter"}, models[0].id), priors,
        rng=random.Random(seed), **kw,
    )


async def _decide(router, **over):
    args = dict(model_policy=_policy(objective=OBJ), **ROUTE_ARGS)
    args.update(over)
    return await router.route(**args)


@pytest.fixture
def shadow_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "shadow")


def _priors():
    return _Stub(rows=_rows("test/alpha") + _rows("test/beta"))


# ── (i) off ──────────────────────────────────────────────────────────────────
async def test_off_has_no_shadow_key_and_equals_a_call_without_a_harness(monkeypatch):
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "off")
    with_id = await _decide(_router(_priors()), harness_id=HARNESS)
    without = await _decide(_router(_priors()))
    assert "shadow" not in with_id.to_json() and with_id.shadow is None
    assert _comparable(with_id) == _comparable(without)


# ── (ii) shadow changes nothing but the shadow key ───────────────────────────
async def test_shadow_adds_only_the_shadow_key(shadow_on, monkeypatch):
    on = await _decide(_router(_priors()), harness_id=HARNESS)
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "off")
    off = await _decide(_router(_priors()), harness_id=HARNESS)
    j_on, j_off = _comparable(on), _comparable(off)
    block = j_on.pop("shadow")
    assert j_on == j_off
    assert block["status"] == "scored" and block["harness_id"] == str(HARNESS)
    assert block["live_choice"] == on.chosen_model


async def test_live_gives_a_non_opted_agent_the_same_shadow_block_as_shadow(monkeypatch):
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "live")
    live = await _decide(_router(_priors(), shadow_rng=random.Random(4)), harness_id=HARNESS)
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "shadow")
    shadow = await _decide(_router(_priors(), shadow_rng=random.Random(4)), harness_id=HARNESS)
    assert live.shadow is not None and live.scored is None
    assert live.shadow == shadow.shadow and _comparable(live) == _comparable(shadow)


async def test_shadow_without_a_harness_id_records_nothing(shadow_on):
    assert (await _decide(_router(_priors()))).shadow is None


# ── (iii) the live rng is untouched ──────────────────────────────────────────
async def test_the_shadow_never_consumes_the_live_exploration_stream(monkeypatch):
    untried, proven = _model("untried"), _model("proven", price="2")
    priors = {proven.id: _prior(proven.id, 0.9, floor=0.8)}

    async def run(mode):
        monkeypatch.setattr(get_settings(), "router_agent_scoring", mode)
        router = ModelRouter(
            _catalog(untried, proven), _AnsweringRegistry({"openrouter"}, proven.id),
            _Stub(priors, rows=_rows(proven.id)), rng=random.Random(FIRES_AT_MAX_SEED),
        )
        out = []
        for _ in range(20):
            d = await _decide(router, model_policy=_policy(objective=OBJ, adaptive={"exploration": MAX_EXPLORATION}),
                              harness_id=HARNESS)
            out.append(d.chosen_model)
        return out

    off, on = await run("off"), await run("shadow")
    assert off == on and untried.id in on and proven.id in on


# ── (iv) override and pin carry no shadow ────────────────────────────────────
async def test_override_and_pinned_decisions_carry_no_shadow(shadow_on):
    stub = _priors()
    over = await _decide(_router(stub), harness_id=HARNESS, run_override="test/alpha")
    pinned = await _decide(
        _router(stub), harness_id=HARNESS, model_policy=_policy(mode="pinned", model="test/beta")
    )
    assert over.shadow is None and pinned.shadow is None and stub.agent_calls == 0


# ── (v) degraded providers ───────────────────────────────────────────────────
async def test_a_provider_without_for_agent_is_unavailable_and_the_live_pick_stands(shadow_on, monkeypatch):
    d = await _decide(_router(_NoAgent()), harness_id=HARNESS)
    assert d.shadow == {"version": scoring.SCORING_VERSION, "status": "unavailable"}
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "off")
    assert d.chosen_model == (await _decide(_router(_NoAgent()), harness_id=HARNESS)).chosen_model


async def test_a_failing_for_agent_is_status_error_and_the_live_pick_stands(shadow_on, monkeypatch):
    d = await _decide(_router(_Stub(boom=True)), harness_id=HARNESS)
    assert d.shadow == {"version": scoring.SCORING_VERSION, "status": "error"}
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "off")
    assert _comparable(d) | {"shadow": None} == _comparable(await _decide(_router(_Stub(boom=True)), harness_id=HARNESS)) | {"shadow": None}


async def test_for_agent_returning_none_is_unavailable(shadow_on):
    d = await _decide(_router(_Stub(rows=None)), harness_id=HARNESS)
    assert d.shadow["status"] == "unavailable"


async def test_learning_off_is_recorded_as_such(shadow_on):
    d = await _decide(
        _router(_priors()), harness_id=HARNESS,
        model_policy=_policy(objective=OBJ, adaptive={"learn_from_outcomes": False}),
    )
    assert d.shadow["status"] == "learning_off"


# ── (vi) the pool ────────────────────────────────────────────────────────────
def _uncurated(name, **kw):
    m = _model(name, **kw)
    m.curated = False  # never a live candidate without a record that passes the priors gate
    return m


async def test_a_model_with_a_record_outside_the_live_candidates_joins_the_pool(shadow_on):
    a, b, extra = _model("alpha"), _model("beta", price="2"), _uncurated("extra")
    stub = _Stub(rows=_rows("test/alpha") + _rows("test/beta") + _rows(extra.id))
    d = await _decide(_router(stub, a, b, extra), harness_id=HARNESS)
    assert extra.id not in d.candidates
    ids = {e["model"] for e in d.shadow["models"]}
    assert extra.id in ids and {a.id, b.id} <= ids


async def test_a_model_the_policy_excludes_does_not_join_the_pool(shadow_on):
    a, b = _model("alpha"), _model("beta", price="2")
    premium = _uncurated("premium", cost_tier="premium")
    stranger = _uncurated("stranger")
    rows = _rows("test/alpha") + _rows("test/beta") + _rows(premium.id) + _rows(stranger.id)
    # above the cost tier
    d = await _decide(
        _router(_Stub(rows=rows), a, b, premium), harness_id=HARNESS,
        model_policy=_policy(objective=OBJ, max_cost_tier="economy"),
    )
    assert premium.id not in {e["model"] for e in d.shadow["models"]}
    # not in `allowed`
    d = await _decide(
        _router(_Stub(rows=rows), a, b, stranger), harness_id=HARNESS,
        model_policy=_policy(objective=OBJ, allowed=[a.id, b.id]),
    )
    assert stranger.id not in {e["model"] for e in d.shadow["models"]}


async def test_an_extra_pool_model_under_cooldown_is_dropped(shadow_on):
    a, b, extra = _model("alpha"), _model("beta", price="2"), _uncurated("extra")
    rows = _rows("test/alpha") + _rows("test/beta") + _rows(extra.id)
    kept = await _decide(_router(_Stub(rows=rows), a, b, extra), harness_id=HARNESS)
    dropped = await _decide(_router(_Stub(rows=rows, cooling={extra.id}), a, b, extra), harness_id=HARNESS)
    assert extra.id in {e["model"] for e in kept.shadow["models"]}
    assert extra.id not in {e["model"] for e in dropped.shadow["models"]}


async def test_an_extra_pool_model_that_does_not_fit_the_window_is_dropped(shadow_on):
    a, b, extra = _model("alpha"), _model("beta", price="2"), _uncurated("extra")
    extra.context_window = 1000
    stub = _Stub(rows=_rows("test/alpha") + _rows("test/beta") + _rows(extra.id))
    d = await _decide(_router(stub, a, b, extra), harness_id=HARNESS, min_context_window=50_000)
    assert extra.id not in {e["model"] for e in d.shadow["models"]}
    d = await _decide(_router(stub, a, b, extra), harness_id=HARNESS)
    assert extra.id in {e["model"] for e in d.shadow["models"]}


async def test_with_exploration_off_there_is_no_draw_and_the_blocks_are_identical(shadow_on):
    rng = random.Random(3)
    router = _router(_priors(), shadow_rng=rng)
    policy = _policy(objective=OBJ, adaptive={"exploration": 0})
    d1 = await _decide(router, harness_id=HARNESS, model_policy=policy)
    d2 = await _decide(router, harness_id=HARNESS, model_policy=policy)
    assert d1.shadow == d2.shadow
    assert d1.shadow["exploration"]["draw"] is None and d1.shadow["exploration"]["fired"] is False
    assert rng.random() == random.Random(3).random()  # never advanced


# ── (vii) the draw ───────────────────────────────────────────────────────────
async def test_a_seeded_shadow_rng_makes_the_stored_draw_reproducible(shadow_on):
    d1 = await _decide(_router(_priors(), shadow_rng=random.Random(5)), harness_id=HARNESS)
    d2 = await _decide(_router(_priors(), shadow_rng=random.Random(5)), harness_id=HARNESS)
    assert d1.shadow["exploration"]["draw"] == d2.shadow["exploration"]["draw"] == round(random.Random(5).random(), 6)


# ── for_agent against a database ─────────────────────────────────────────────
OTHER = uuid.uuid4()
T = datetime(2026, 11, 12, 12, tzinfo=timezone.utc)


def _outcome(model, *, harness=HARNESS, run_id=None, outcome_class="delivered", family=None, age_days=1.0, rejected=0):
    return RunOutcome(
        run_id=run_id or uuid.uuid4(), segment_index=0, harness_id=harness, task_type="freeform",
        task_shape="freeform", objective=OBJ, max_cost_tier="premium", size_band="s", model_id=model,
        provider="openrouter", outcome_class=outcome_class, quality_score=Decimal("0.8"),
        score_version="outcome-v1", components={}, iterations=3, cost_usd=Decimal("0.05"),
        input_tokens=1000, output_tokens=500, duration_ms=1, findings_rejected=rejected,
        task_family=family, observed_at=T - timedelta(days=age_days),
    )


def _target(run_id, status, **kw):
    return BatchTarget(
        id=uuid.uuid4(), collection_id=uuid.uuid4(), position=0, slug=f"t-{uuid.uuid4().hex[:6]}", company="C",
        market="US", status=status, run_id=run_id, **kw,
    )


def test_for_agent_reads_one_agents_rows_with_their_target_class(client, world):
    from sqlalchemy import text

    runs = {k: uuid.uuid4() for k in ("written", "completed", "gap", "review")}

    async def go(db):
        await db.execute(text("PRAGMA foreign_keys=OFF"))  # synthetic rows have no run or batch behind them
        db.add_all([
            _outcome("m/plain"),
            _outcome("m/plain", rejected=2),
            _outcome("m/other", harness=OTHER),
            _outcome("m/written", run_id=runs["written"], family="f@w"),
            _outcome("m/completed", run_id=runs["completed"], family="f@w"),
            _outcome("m/gap", run_id=runs["gap"], family="f@w", outcome_class="delivered"),
            _outcome("m/review", run_id=runs["review"], family="f@w"),
            _outcome("m/orphan", family="f@w"),  # batch row, no current target
            _outcome("m/handed", outcome_class="handed_off_capacity"),  # not a quality class
            _outcome("m/old", age_days=400.0),
        ])
        for key in ("written", "completed", "gap", "review"):
            db.add(_target(runs[key], key))
        await db.commit()
        p = OutcomePriors(world.factory, now=lambda: T)
        rows = await p.for_agent(HARNESS)
        again = await p.for_agent(HARNESS)
        none = await p.for_agent(None)
        return rows, again, none, p

    rows, again, none, p = run_db(client, world, go)
    by = {}
    for r in rows:
        by.setdefault(r.model_id, []).append(r)
    assert none is None and again is rows  # cached
    assert set(by) == {"m/plain", "m/written", "m/completed", "m/gap", "m/review", "m/orphan"}
    assert [scoring.acceptable(r) for r in by["m/plain"]].count(True) == 1
    assert scoring.acceptable(by["m/written"][0]) is True and scoring.acceptable(by["m/completed"][0]) is True
    assert scoring.acceptable(by["m/gap"][0]) is False
    assert scoring.acceptable(by["m/review"][0]) is None and by["m/review"][0].target_class == "unsettled"
    assert scoring.acceptable(by["m/orphan"][0]) is None and by["m/orphan"][0].is_batch is True
    assert by["m/orphan"][0].target_class is None
    assert all(r.cost_usd == 0.05 and r.input_tokens == 1000 for r in rows)
    # invalidate() clears the cache: the next call is a fresh list
    p.invalidate()
    assert p._agent_cache == {}


def test_for_agent_failure_is_logged_not_raised_and_not_cached(client, world, monkeypatch):
    async def go(db):
        p = OutcomePriors(world.factory, now=lambda: T)

        async def boom(self, harness_id):
            raise RuntimeError("db down")

        monkeypatch.setattr(OutcomePriors, "_agent_rows", boom)
        got = await p.for_agent(HARNESS)
        return got, dict(p._agent_cache)

    got, cache = run_db(client, world, go)
    assert got is None and cache == {}


# ── the preview endpoint ─────────────────────────────────────────────────────
@pytest_asyncio.fixture
async def preview_client(session_factory, monkeypatch):
    from tret.providers.catalog import ModelCatalog  # noqa: F401

    monkeypatch.setattr(routing_api, "get_harness_engine", lambda: _FakeEngine())
    monkeypatch.setattr(routing_api, "ProviderRegistry", lambda db_keys: _FakeRegistry(_ALL_KEYS))
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)
    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(routing_api.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _preview(c, harness, **extra):
    r = await c.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment", **extra},
    )
    assert r.status_code == 200, r.text
    return r.json()["results"][0]["decision"]


async def test_the_preview_endpoint_carries_a_shadow_block_only_when_shadow_is_on(
    preview_client, seed, session_factory, monkeypatch
):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(preview_client, admin.email)
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "off")
    off = await _preview(preview_client, harness)
    assert "shadow" not in off
    monkeypatch.setattr(get_settings(), "router_agent_scoring", "shadow")
    on = await _preview(preview_client, harness)
    # the preview's engine has no per-agent record, so the block says so
    assert on["shadow"]["status"] == "unavailable"
    assert on["chosen_model"] == off["chosen_model"]


def _run(run_id, stamp=None):
    return Run(
        id=run_id, project_id=uuid.uuid4(), harness_id=HARNESS, task_type="freeform",
        task_input={} if stamp is None else {"_collection_id": stamp}, document_ids=[],
        status="completed", model_used="m/x",
    )


def test_for_agent_recognises_pre_family_batch_rows_by_the_run_stamp(client, world):
    from sqlalchemy import text

    stamped, plain, matched, infra, timeout = (uuid.uuid4() for _ in range(5))

    async def go(db):
        await db.execute(text("PRAGMA foreign_keys=OFF"))
        db.add_all([
            _run(stamped, str(uuid.uuid4())), _run(plain), _run(matched), _run(infra), _run(timeout),
            _outcome("m/stamped", run_id=stamped),  # NULL task_family, stamped, no target
            _outcome("m/plain", run_id=plain),  # NULL task_family, not stamped
            _outcome("m/matched", run_id=matched),  # NULL task_family, judged by its target
            _outcome("m/infra", run_id=infra),
            _outcome("m/timeout", run_id=timeout),
            _target(matched, "written"),
            _target(infra, "failed", error="OSError: no space left on device"),
            _target(timeout, "failed", error="timeout"),
        ])
        await db.commit()
        return await OutcomePriors(world.factory, now=lambda: T).for_agent(HARNESS)

    rows = {r.model_id: r for r in run_db(client, world, go)}
    assert rows["m/stamped"].is_batch is True and scoring.acceptable(rows["m/stamped"]) is None
    assert rows["m/plain"].is_batch is False and scoring.acceptable(rows["m/plain"]) is True
    assert rows["m/matched"].is_batch is True and scoring.acceptable(rows["m/matched"]) is True
    assert rows["m/infra"].target_class == "infra" and scoring.acceptable(rows["m/infra"]) is None
    assert rows["m/timeout"].target_class == "timeout" and scoring.acceptable(rows["m/timeout"]) is None
