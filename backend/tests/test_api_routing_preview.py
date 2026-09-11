"""`POST /api/routing/preview` (`tret/api/routing.py`): a dry-run of
`ModelRouter.route()` — same inputs a run would compute, but nothing is
persisted and no run is ever created.

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_harnesses_api.py/test_runs_api.py.
`get_harness_engine` is monkeypatched to a fake carrying a real (offline)
`ModelCatalog` and a `NoPriors` — the same reason test_runs_api.py
monkeypatches it: the real engine's `OutcomePriors` opens its own session
against the process-wide (non-test) database, and a routing preview must
never touch that. `ProviderRegistry` is monkeypatched too, to a fake keyed on
an explicit provider set — exactly `test_router_and_packs.py`'s pattern —
so the deterministic fallback (never the network-calling LLM router path) is
what these tests exercise, regardless of what provider keys happen to be
configured in this environment's own .env.
"""
from __future__ import annotations

import uuid

import httpx
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

import tret.engine.tools as tools_module
from tests.evals.golden_world import install_sqlite_type_shims
from tret.adaptive import MAX_EXPLORATION
from tret.api import auth
from tret.api import routing as routing_api
from tret.db.engine import get_db
from tret.db.models import (
    Base,
    Harness,
    Pack,
    ProviderCredential,
    Project,
    Run,
    User,
    Workspace,
    WorkspaceMember,
)
from tret.net import MODE_OFF, MODE_ON
from tret.packs.links import set_harness_packs
from tret.providers.catalog import ModelCatalog, ProviderRegistry
from tret.router_llm.objectives import OBJECTIVES
from tret.router_llm.priors_base import NoPriors
from tret.router_llm.router import ModelRouter
from tret.services.credentials import get_fernet

# A one-page-ish block of extra instructions, used to pin that a preview's
# input_tokens actually grows with `system_prompt_extra` rather than the fixed
# 8192/None the endpoint used to hardcode regardless of what was asked for.
ONE_PAGE_EXTRA = "Cross-check every disclosed figure against the dataset. " * 80

# Two real, always-curated catalog entries (backend/tret/providers/models.yaml)
# used by the allowed-list/pinned-mode ceiling tests below — both `openrouter`
# (so the fixture's `_ALL_KEYS = {"openrouter"}` never excludes either) and
# both within a "premium"-capped harness's own ceiling. PREMIUM_MODEL is
# `cost_tier: premium` itself — used to prove a pin can't dodge a *lower*
# ceiling by lying about the field (fix for mode: "pinned" below).
PREMIUM_MODEL = "openrouter/openai/gpt-6-astra"
ECONOMY_MODEL = "openrouter/google/gemini-3.5-flash-lite"

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


class _FakeRegistry(ProviderRegistry):
    """`has_key` only — never `.get()` — matching `test_router_and_packs.py`'s
    `FakeRegistry` and `test_routing_objectives.py`'s `_Registry`: the
    deterministic fallback path never asks a registry for an actual client.
    """

    def __init__(self, providers: set[str]):
        self._providers = providers

    def has_key(self, provider: str) -> bool:
        return provider in self._providers


class _FakeEngine:
    """Stands in for `get_harness_engine()`'s singleton `HarnessEngine`: a
    real, offline `ModelCatalog` (curated entries only — no local/dynamic
    discovery is configured in tests) plus `NoPriors`, so a preview never
    opens a session against the real process-wide database the way
    `OutcomePriors` would.
    """

    def __init__(self):
        self.catalog = ModelCatalog()
        self.priors = NoPriors()


# Module-level default: enough candidates exist under "openrouter" alone for
# the deterministic fallback to pick something for every shape/objective this
# file uses. Individual tests override via `monkeypatch` when they need a
# different (or empty) provider set.
_ALL_KEYS = {"openrouter"}


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
    monkeypatch.setattr(routing_api, "get_harness_engine", lambda: _FakeEngine())
    monkeypatch.setattr(routing_api, "ProviderRegistry", lambda db_keys: _FakeRegistry(_ALL_KEYS))
    # Forces the deterministic fallback on every call in this file — the same
    # way test_routing_objectives.py's own fallback tests avoid the LLM router
    # path — so no fake Provider/complete_json is needed: a preview here never
    # makes a real (or fake) network call to a router model.
    monkeypatch.setattr(ModelRouter, "_resolve_router_model", lambda self, max_tier: None)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(routing_api.router)

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


def make_member(user: User, workspace: Workspace, *, role: str = "admin") -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_pack(workspace: Workspace, *, slug: str = "climate-risk") -> Pack:
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
            "task_types": [
                {
                    "slug": "divergence_assessment",
                    "display_name": "Divergence Assessment",
                    "shape": "verdict",
                    "instructions": "Compare disclosed emissions against the dataset.",
                    "output_contract": "verdict",
                }
            ],
        },
        source_path=f"/tmp/{slug}",
    )


def make_harness(
    workspace: Workspace,
    *,
    model_policy: dict | None = None,
    loop_config: dict | None = None,
    system_prompt_extra: str | None = None,
) -> Harness:
    return Harness(
        id=uuid.uuid4(),
        workspace_id=workspace.id,
        name="Analyst",
        model_policy=model_policy or {"mode": "auto", "max_cost_tier": "premium"},
        tool_names=[],
        system_prompt_extra=system_prompt_extra,
        loop_config=loop_config
        or {"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2},
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def _basic_setup(seed, session_factory):
    team = make_workspace("Co")
    project = Project(id=uuid.uuid4(), workspace_id=team.id, name="P")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    await seed(team, project, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    return team, admin, pack, harness


# ── preview by harness id ──────────────────────────────────────────────────
async def test_preview_by_harness_id_returns_decision_and_estimate(client, seed, session_factory):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["objective"] == "balanced"
    decision = result["decision"]
    assert decision["chosen_model"].startswith("openrouter/")
    assert decision["task_shape"] == "verdict"
    estimate = result["estimate"]
    assert estimate["input_tokens"] > 0
    assert estimate["max_output_tokens"] == 8192
    assert 0 <= estimate["cost_usd_low"] <= estimate["cost_usd_high"]
    assert estimate["router_overhead_usd"] == 0.0  # deterministic fallback: no router call made


async def test_unknown_harness_id_is_404(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview", json={"harness_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


# ── inline policy, no saved harness ─────────────────────────────────────────
async def test_inline_policy_works_without_saving(client, seed):
    """A form previewing edits it has not saved yet has no harness row at
    all — only an inline model_policy plus a pack/task selection."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    await seed(team, admin, make_member(admin, team), pack)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "pack_id": str(pack.id),
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["results"]) == 1
    assert body["results"][0]["decision"]["chosen_model"].startswith("openrouter/")


async def test_inline_without_model_policy_or_harness_id_is_422(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post("/api/routing/preview", json={})
    assert response.status_code == 422


# ── compare ──────────────────────────────────────────────────────────────────
async def test_compare_true_returns_four_objectives(client, seed, session_factory):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "compare": True,
        },
    )
    assert response.status_code == 200, response.text
    results = response.json()["results"]
    assert len(results) == 4
    assert {r["objective"] for r in results} == set(OBJECTIVES)
    for r in results:
        assert r["decision"]["chosen_model"].startswith("openrouter/")


async def test_preview_forces_exploration_to_zero_on_the_policy_it_routes_with(
    client, seed, session_factory, monkeypatch
):
    """A preview must be reproducible: `ModelRouter._maybe_explore`'s coin
    flip (router_llm/router.py) must never let two previews of the same
    policy — or `compare`'s four objectives within one call — disagree.
    `preview_routing` (api/routing.py) forces `adaptive.exploration` to 0 on
    the copy of the policy it hands to `route()`, never on `model_policy`
    itself. Verified by capturing what `ModelRouter.route` is actually
    called with, rather than by seeding a roll to fire — this fixture's
    harness/task carry a `verdict`-shaped task, not `extraction`, so
    exploration's own guardrails would never let it fire here regardless;
    the point under test is the policy mutation itself, not the coin flip.
    """
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)

    captured: list[dict] = []
    real_route = ModelRouter.route

    async def _capturing_route(self, *, model_policy, **kwargs):
        captured.append(model_policy)
        return await real_route(self, model_policy=model_policy, **kwargs)

    monkeypatch.setattr(ModelRouter, "route", _capturing_route)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "compare": True,
            "model_policy": {
                "mode": "auto",
                "max_cost_tier": "premium",
                "adaptive": {"exploration": MAX_EXPLORATION},
            },
        },
    )
    assert response.status_code == 200, response.text
    assert len(captured) == 4  # compare: true, one route() call per objective
    for policy in captured:
        assert policy["adaptive"]["exploration"] == 0


# ── never persists ───────────────────────────────────────────────────────────
async def test_preview_creates_no_run_row(client, seed, session_factory):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment", "compare": True},
    )
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        rows = (await db.execute(select(Run))).scalars().all()
    assert rows == []


# ── no candidates ────────────────────────────────────────────────────────────
async def test_no_candidate_policy_returns_409(client, seed, monkeypatch):
    """`max_cost_tier: local` with no local model available and no provider
    keys at all: the deterministic fallback returns None and `route()` raises
    `RoutingUnavailable` — the same scenario
    `test_router_cost_ceiling.py::test_fallback_capped_at_local_with_no_local_model_returns_none`
    covers directly against `fallback_model`."""
    monkeypatch.setattr(routing_api, "ProviderRegistry", lambda db_keys: _FakeRegistry(set()))
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"model_policy": {"mode": "auto", "max_cost_tier": "local"}, "task_type": "freeform"},
    )
    assert response.status_code == 409
    assert "no fallback model" in response.text or "No candidate models" in response.text


# ── auth ─────────────────────────────────────────────────────────────────────
async def test_unauthenticated_request_is_401(client):
    response = await client.post(
        "/api/routing/preview",
        json={"model_policy": {"mode": "auto", "max_cost_tier": "premium"}},
    )
    assert response.status_code == 401


# ── overrides actually flow through (the ~4x max_output_tokens bug) ─────────
async def test_harness_id_path_uses_harness_max_output_tokens_and_extra_prompt(
    client, seed, session_factory
):
    """A saved harness's own `loop_config.max_output_tokens` and
    `system_prompt_extra` must reach the router/estimate exactly as a real run
    of that harness would use them — not the hardcoded
    DEFAULT_MAX_OUTPUT_TOKENS/None a fully-inline preview used to fall back to
    regardless of what the harness actually specified."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        loop_config={"max_iterations": 24, "max_output_tokens": 32000, "temperature": 0.2},
        system_prompt_extra=ONE_PAGE_EXTRA,
    )
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment"},
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["results"][0]["estimate"]
    assert estimate["max_output_tokens"] == 32000
    # chars/4 estimator (engine/context.py) — the extra prompt alone is worth
    # at least this many tokens, so a preview that dropped it (or used the
    # zero-config _PreviewHarness stand-in) would read well under this floor.
    assert estimate["input_tokens"] >= len(ONE_PAGE_EXTRA) // 4


async def test_inline_path_respects_overridden_max_output_tokens_and_extra_prompt(client, seed):
    """The same fields, sent inline (no harness_id at all) — the "create a
    new harness" case, which has no saved row to read a default from."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    await seed(team, admin, make_member(admin, team), pack)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "pack_id": str(pack.id),
            "task_type": "divergence_assessment",
            "max_output_tokens": 32000,
            "system_prompt_extra": ONE_PAGE_EXTRA,
        },
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["results"][0]["estimate"]
    assert estimate["max_output_tokens"] == 32000
    assert estimate["input_tokens"] >= len(ONE_PAGE_EXTRA) // 4


async def test_override_on_saved_harness_beats_its_own_saved_values(client, seed, session_factory):
    """Editing a saved harness's form and previewing before Save must reflect
    the *unsaved* values, not silently revert to what is on disk."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        loop_config={"max_iterations": 24, "max_output_tokens": 8192, "temperature": 0.2},
        system_prompt_extra=None,
    )
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "max_output_tokens": 32000,
            "system_prompt_extra": ONE_PAGE_EXTRA,
        },
    )
    assert response.status_code == 200, response.text
    estimate = response.json()["results"][0]["estimate"]
    assert estimate["max_output_tokens"] == 32000
    assert estimate["input_tokens"] >= len(ONE_PAGE_EXTRA) // 4


# ── inline model_policy validation (never unvalidated) ──────────────────────
async def test_inline_invalid_objective_is_422(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "objective": "cheapest_possible"},
            "task_type": "freeform",
        },
    )
    assert response.status_code == 422


async def test_inline_invalid_max_cost_tier_is_422(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"model_policy": {"mode": "auto", "max_cost_tier": "free"}, "task_type": "freeform"},
    )
    assert response.status_code == 422


async def test_inline_allowed_wrong_type_is_422_not_500(client, seed):
    """`allowed: 5` used to reach `for m in policy.get("allowed") or []`,
    which is a TypeError (not iterable) rather than a validation failure."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"model_policy": {"mode": "auto", "allowed": 5}, "task_type": "freeform"},
    )
    assert response.status_code == 422


# ── a non-admin cannot lift a saved harness's policy via an inline override ──
async def test_analyst_cannot_lift_local_capped_harness_via_inline_policy(
    client, seed, session_factory
):
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(team, model_policy={"mode": "auto", "max_cost_tier": "local"})
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 403


async def test_admin_can_lift_local_capped_harness_via_inline_policy(client, seed, session_factory):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team, model_policy={"mode": "auto", "max_cost_tier": "local"})
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text


# ── cross-workspace isolation ────────────────────────────────────────────────
async def test_harness_id_from_another_workspace_is_404(client, seed):
    team_a = make_workspace("A")
    team_b = make_workspace("B")
    admin_a = make_user("admin-a@example.com")
    harness_b = make_harness(team_b)
    await seed(team_a, team_b, admin_a, make_member(admin_a, team_a), harness_b)
    await login(client, admin_a.email)

    response = await client.post("/api/routing/preview", json={"harness_id": str(harness_b.id)})
    assert response.status_code == 404


async def test_pack_id_from_another_workspace_is_404(client, seed):
    team_a = make_workspace("A")
    team_b = make_workspace("B")
    admin_a = make_user("admin-a@example.com")
    pack_b = make_pack(team_b)
    await seed(team_a, team_b, admin_a, make_member(admin_a, team_a), pack_b)
    await login(client, admin_a.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "pack_id": str(pack_b.id),
            "task_type": "freeform",
        },
    )
    assert response.status_code == 404


async def test_archived_harness_is_404(client, seed, session_factory):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        h.is_archived = True
        await db.commit()
    await login(client, admin.email)

    response = await client.post("/api/routing/preview", json={"harness_id": str(harness.id)})
    assert response.status_code == 404


async def test_provider_keys_are_workspace_scoped(client, seed, monkeypatch):
    """`load_db_keys` is never stubbed in this test — only `ProviderRegistry`
    itself, and only to turn whatever keys it actually loaded into
    `has_key()` answers (`_FakeRegistry(set(db_keys))` rather than the
    module-default `_FakeRegistry(_ALL_KEYS)` every other test in this file
    uses) — so this is a real check of `load_db_keys(db, ctx.id)`'s workspace
    scoping, not of the fake. A key stored for workspace B must not make
    workspace A's preview see any candidate."""
    monkeypatch.setattr(
        routing_api, "ProviderRegistry", lambda db_keys: _FakeRegistry(set(db_keys))
    )
    team_a = make_workspace("A")
    team_b = make_workspace("B")
    admin_a = make_user("admin-a@example.com")
    cred_b = ProviderCredential(
        workspace_id=team_b.id,
        provider="openrouter",
        encrypted_key=get_fernet().encrypt(b"sk-workspace-b"),
    )
    await seed(team_a, team_b, admin_a, make_member(admin_a, team_a), cred_b)
    await login(client, admin_a.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "freeform",
        },
    )
    assert response.status_code == 409


# ── no persistence ────────────────────────────────────────────────────────────
async def test_inline_override_preview_does_not_modify_saved_harness(client, seed, session_factory):
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)
    async with session_factory() as db:
        before = await db.get(Harness, harness.id)
        before_policy = dict(before.model_policy)
        before_updated_at = before.updated_at

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "max_cost_tier": "economy"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        after = await db.get(Harness, harness.id)
        assert after.model_policy == before_policy
        assert after.updated_at == before_updated_at


# ── a missing `allowed` does not lift a saved harness's allow-list ─────────
async def test_analyst_missing_allowed_field_is_403(client, seed, session_factory):
    """Dropping `allowed` from an inline override used to compute
    `set(inline.allowed or []) - set(saved_allowed)` as an *empty* set (no
    model named, nothing "outside"), which the router then treated as no
    allow-list restriction at all — a non-admin escaping the harness's own
    allow-list simply by not mentioning it."""
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        model_policy={
            "mode": "auto",
            "max_cost_tier": "premium",
            "allowed": [PREMIUM_MODEL, ECONOMY_MODEL],
        },
    )
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 403


async def test_analyst_allowed_subset_is_200(client, seed, session_factory):
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        model_policy={
            "mode": "auto",
            "max_cost_tier": "premium",
            "allowed": [PREMIUM_MODEL, ECONOMY_MODEL],
        },
    )
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "allowed": [ECONOMY_MODEL]},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["decision"]["chosen_model"] == ECONOMY_MODEL


async def test_admin_missing_allowed_field_is_200(client, seed, session_factory):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        model_policy={
            "mode": "auto",
            "max_cost_tier": "premium",
            "allowed": [PREMIUM_MODEL, ECONOMY_MODEL],
        },
    )
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text


async def test_analyst_widened_allowed_is_403(client, seed, session_factory):
    """Explicitly naming a model outside the saved `allowed` list — as
    opposed to the omission case above — was already caught before this fix;
    kept here as the missing direct test the review named."""
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(
        team,
        model_policy={"mode": "auto", "max_cost_tier": "premium", "allowed": [ECONOMY_MODEL]},
    )
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "allowed": [ECONOMY_MODEL, PREMIUM_MODEL]},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 403


# ── mode: "pinned" is held to the same ceiling as mode: "auto" ─────────────
async def test_analyst_pinned_model_exceeds_ceiling_is_403(client, seed, session_factory):
    """A pin's own `max_cost_tier` field is never read by the router (it goes
    straight to the named model), so the permission check must resolve the
    pin's *actual* cost_tier from the catalog rather than trusting the field
    — otherwise `{"mode": "pinned", "model": ..., "max_cost_tier": "local"}`
    would dress a premium pin as compliant and sail through."""
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(team, model_policy={"mode": "auto", "max_cost_tier": "local"})
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "pinned", "model": PREMIUM_MODEL},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 403


async def test_admin_pinned_model_bypasses_ceiling_is_200(client, seed, session_factory):
    """Unlike a non-admin, a workspace admin may preview a pin above the
    harness's own ceiling — same asymmetry `mode: "auto"` already has."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team, model_policy={"mode": "auto", "max_cost_tier": "local"})
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "pinned", "model": PREMIUM_MODEL},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["decision"]["chosen_model"] == PREMIUM_MODEL


# ── pack_id on the harness-id path resolves like the inline path ───────────
async def test_unsaved_pack_not_linked_previews_200_on_harness_id_path(client, seed, session_factory):
    """A form previewing a pack it just added but has not Saved yet — the
    pack exists in the workspace but `packs_for_harness` does not return it,
    since no link row has been written. The harness-id path used to 404 this
    ('pack not found on this harness'); it must resolve the same way the
    inline (no harness_id) path always has."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    linked_pack = make_pack(team, slug="climate-risk")
    new_pack = make_pack(team, slug="new-pack")
    harness = make_harness(team)
    await seed(team, admin, make_member(admin, team), linked_pack, new_pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [linked_pack.id])  # new_pack is deliberately NOT linked
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "pack_id": str(new_pack.id),
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 200, response.text


async def test_explicit_null_pack_id_previews_without_a_pack(client, seed, session_factory):
    """`pack_id: null` is a real request for "no pack", distinct from
    omitting the field (which resolves from the saved harness's own links)."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "pack_id": None, "task_type": "freeform"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["results"][0]["decision"]["task_shape"] == "freeform"


async def test_explicit_null_pack_id_with_non_generic_task_type_is_422(client, seed, session_factory):
    """`engine/harness.py`'s own real-run rule: a task_type nobody declares
    (here, because there is deliberately no pack) fails before the first
    token unless it is one of the engine's own generic types (chat/freeform)."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "pack_id": None,
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 422


async def test_harness_id_path_pack_id_from_another_workspace_is_404(client, seed):
    team_a = make_workspace("A")
    team_b = make_workspace("B")
    admin_a = make_user("admin-a@example.com")
    harness_a = make_harness(team_a)
    pack_b = make_pack(team_b)
    await seed(team_a, team_b, admin_a, make_member(admin_a, team_a), harness_a, pack_b)
    await login(client, admin_a.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness_a.id), "pack_id": str(pack_b.id), "task_type": "freeform"},
    )
    assert response.status_code == 404


# ── tool_names (not a client-computed web_tools_enabled) drives the web- ───
# ── evidence block, via the same precedence engine/harness.py uses ─────────
async def test_web_search_excluded_when_research_egress_off(client, seed, session_factory, monkeypatch):
    """`tool_names` (mirroring the saved harness's own, here) must still be
    subject to `withheld_web_tools` — a deployment with research egress off
    must see a smaller estimate, the same way a real run would never include
    the web-evidence rules block for a withheld tool."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    harness.tool_names = ["web_search"]
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_ON)
    on_resp = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment"},
    )
    assert on_resp.status_code == 200, on_resp.text
    tokens_on = on_resp.json()["results"][0]["estimate"]["input_tokens"]

    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_OFF)
    off_resp = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment"},
    )
    assert off_resp.status_code == 200, off_resp.text
    tokens_off = off_resp.json()["results"][0]["estimate"]["input_tokens"]

    assert tokens_off < tokens_on


async def test_pack_task_web_tool_included_despite_empty_harness_tool_names(
    client, seed, session_factory, monkeypatch
):
    """The task's own declared `tools` take precedence over `tool_names`
    (empty here) — the same precedence `api/harnesses.py::get_harness` and a
    real run apply, so a pack that declares `web_search` for one of its task
    types still gets the web-evidence block even on a harness with no tools
    of its own enabled."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = Pack(
        id=uuid.uuid4(),
        workspace_id=team.id,
        slug="web-pack",
        version="1.0.0",
        doctrine_sha="deadbeef",
        manifest={
            "pack": "web-pack",
            "version": "1.0.0",
            "display_name": "Web Pack",
            "task_types": [
                {
                    "slug": "web_check",
                    "display_name": "Web Check",
                    "shape": "verdict",
                    "instructions": "Check disclosures against current web sources.",
                    "output_contract": "verdict",
                    "tools": ["web_search"],
                }
            ],
        },
        source_path="/tmp/web-pack",
    )
    harness = make_harness(team)  # tool_names=[] — nothing enabled on the harness itself
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_ON)
    on_resp = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "web_check"},
    )
    assert on_resp.status_code == 200, on_resp.text
    tokens_on = on_resp.json()["results"][0]["estimate"]["input_tokens"]

    monkeypatch.setattr(tools_module, "_research_mode", lambda: MODE_OFF)
    off_resp = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "web_check"},
    )
    assert off_resp.status_code == 200, off_resp.text
    tokens_off = off_resp.json()["results"][0]["estimate"]["input_tokens"]

    assert tokens_off < tokens_on


# ── compare: true is gated to approver role or higher ───────────────────────
async def test_analyst_cannot_use_compare(client, seed, session_factory):
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "compare": True,
        },
    )
    assert response.status_code == 403


async def test_approver_can_use_compare(client, seed, session_factory):
    team = make_workspace("Co")
    approver = make_user("approver@example.com")
    pack = make_pack(team)
    harness = make_harness(team)
    await seed(team, approver, make_member(approver, team, role="approver"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, approver.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "compare": True,
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["results"]) == 4


# ── explicit null overrides (the missing test the review named) ────────────
async def test_explicit_null_system_prompt_extra_clears_saved_value(client, seed, session_factory):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    pack = make_pack(team)
    harness = make_harness(team, system_prompt_extra=ONE_PAGE_EXTRA)
    await seed(team, admin, make_member(admin, team), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, admin.email)

    omitted = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "divergence_assessment"},
    )
    assert omitted.status_code == 200, omitted.text
    tokens_with_extra = omitted.json()["results"][0]["estimate"]["input_tokens"]

    explicit_null = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "task_type": "divergence_assessment",
            "system_prompt_extra": None,
        },
    )
    assert explicit_null.status_code == 200, explicit_null.text
    tokens_without_extra = explicit_null.json()["results"][0]["estimate"]["input_tokens"]

    assert tokens_without_extra < tokens_with_extra


# ── no harness_id at all means no saved ceiling — admin-only outright ───────
async def test_analyst_inline_without_harness_id_is_403(client, seed):
    """With no `harness_id`, `_check_inline_policy_permission` never runs at
    all — there is no saved policy to hold a non-admin to. Before this fix an
    analyst could drop `harness_id` and preview any policy, including
    `max_cost_tier: "premium"`, on the workspace's own provider key."""
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    await seed(team, analyst, make_member(analyst, team, role="analyst"))
    await login(client, analyst.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "freeform",
        },
    )
    assert response.status_code == 403


async def test_approver_inline_without_harness_id_is_403(client, seed):
    """Admin, not merely approver, is the bar for this path — a stricter role
    than `compare: true` needs, since here there is no saved ceiling at all
    to fall back on, not just a 4x-the-calls multiplier."""
    team = make_workspace("Co")
    approver = make_user("approver@example.com")
    await seed(team, approver, make_member(approver, team, role="approver"))
    await login(client, approver.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "freeform",
        },
    )
    assert response.status_code == 403


# `test_inline_policy_works_without_saving` above already covers the
# admin/200 counterpart to the two 403s just above (an admin previewing
# inline with no harness_id at all).


# ── a saved mode: "pinned" policy has a real ceiling, not a cosmetic one ───
async def test_analyst_saved_pinned_ceiling_derived_from_pin_model(client, seed, session_factory):
    """A saved `mode: "pinned"` policy usually never sets `max_cost_tier` (the
    field is cosmetic in pinned mode — `route()` never reads it for a pin),
    which used to leave `saved_tier` at its DEFAULT_MAX_COST_TIER ("premium")
    fallback regardless of how cheap the actually-pinned model is — no
    effective ceiling at all. The real ceiling must come from what the saved
    pin resolves to (`ECONOMY_MODEL`'s own catalog `cost_tier`, "economy")."""
    team = make_workspace("Co")
    analyst = make_user("analyst@example.com")
    pack = make_pack(team)
    harness = make_harness(team, model_policy={"mode": "pinned", "model": ECONOMY_MODEL})
    await seed(team, analyst, make_member(analyst, team, role="analyst"), pack, harness)
    async with session_factory() as db:
        h = await db.get(Harness, harness.id)
        await set_harness_packs(db, h, [pack.id])
        await db.commit()
    await login(client, analyst.email)

    above = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "divergence_assessment",
        },
    )
    assert above.status_code == 403

    within = await client.post(
        "/api/routing/preview",
        json={
            "harness_id": str(harness.id),
            "model_policy": {"mode": "auto", "max_cost_tier": "economy"},
            "task_type": "divergence_assessment",
        },
    )
    assert within.status_code == 200, within.text


# ── an unresolved task_type always 422s, not just the pack_id: null case ───
async def test_harness_id_omitted_pack_id_unknown_task_type_is_422(client, seed, session_factory):
    """Omitting `pack_id` resolves the harness's own linked pack
    (`resolve_pack_for_task`), which falls back to the primary linked pack for
    a task_type nothing declares — `task_config` then returns None for it,
    and that used to reach the router with no check at all unless `pack_id`
    was the explicit-`null` case."""
    _team, admin, _pack, harness = await _basic_setup(seed, session_factory)
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={"harness_id": str(harness.id), "task_type": "not_a_real_task"},
    )
    assert response.status_code == 422
    assert "unknown_task_type" in response.text


async def test_inline_no_pack_pack_only_task_type_is_422(client, seed):
    """The inline (no harness_id) path never checked task_type resolution at
    all before this fix — a pack-only task_type with no pack given used to
    preview confidently under `task_config`'s silent freeform default."""
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "divergence_assessment",
        },
    )
    assert response.status_code == 422
    assert "unknown_task_type" in response.text


# ── max_output_tokens is bounded, not just positive ─────────────────────────
async def test_max_output_tokens_above_ceiling_is_422(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "freeform",
            "max_output_tokens": routing_api.MAX_OUTPUT_TOKENS_CEILING + 1,
        },
    )
    assert response.status_code == 422


async def test_max_output_tokens_at_ceiling_is_200(client, seed):
    team = make_workspace("Co")
    admin = make_user("admin@example.com")
    await seed(team, admin, make_member(admin, team))
    await login(client, admin.email)

    response = await client.post(
        "/api/routing/preview",
        json={
            "model_policy": {"mode": "auto", "max_cost_tier": "premium"},
            "task_type": "freeform",
            "max_output_tokens": routing_api.MAX_OUTPUT_TOKENS_CEILING,
        },
    )
    assert response.status_code == 200, response.text
