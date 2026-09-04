"""`POST /api/analytics/emissions/whatif`: a read-only recompute of a window's
emissions under a scenario `factors` document, alongside the same window's
stored (`recorded`) figures.

Real sqlite database, `httpx.AsyncClient` + `ASGITransport` directly against
the app — same harness as test_runs_api.py. `get_catalog` and `get_settings`
are monkeypatched (in every module that binds its own `get_settings` name:
`tret.api.analytics`, `tret.services.emissions`, `tret.services.
emission_factors`) to a small fixed catalog and a fixed `Settings` instance,
so every assertion here is independent of whatever the real product catalog
and the real process environment happen to contain.

`FIXED_SETTINGS.emissions_baseline_model` is set to the same id every test run
uses, so the frontier-baseline counterfactual always resolves to "the run is
its own baseline" (avoided figures pinned at 0) — that comparison is not what
this endpoint exists to test, and pinning it keeps every other assertion from
having to account for it.
"""
from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from argon2 import PasswordHasher
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from tests.evals.golden_world import install_sqlite_type_shims
from tret.api import analytics as analytics_module
from tret.api import auth
from tret.config import Settings
from tret.db.engine import get_db
from tret.db.models import Base, Harness, Project, Run, User, Workspace, WorkspaceMember, utcnow
from tret.engine import extensions as extensions_module
from tret.engine.extensions import ExtensionAPI, GateResult
from tret.providers.catalog import ModelInfo
from tret.services import emission_factors as emission_factors_module
from tret.services import emissions as emissions_module
from tret.services.emissions import combine_accountings, energy_accounting

HASHER = PasswordHasher()
PASSWORD = "correct-horse-battery-1"


# ── a small, fixed catalog and settings — independent of the real product's ──
class FakeCatalog:
    """Only what `energy_accounting`/`resolve_baseline_model` touch:
    `.get(model_id)` and `.all(curated_only=...)`, over a fixed set of models
    the tests construct directly, so recompute assertions never depend on the
    real product catalog's contents.
    """

    def __init__(self, models: dict[str, ModelInfo]):
        self._models = models

    def get(self, model_id):
        return self._models.get(model_id)

    def all(self, curated_only: bool = False):
        return list(self._models.values())


def make_model(id_: str, provider: str = "anthropic", energy_class: str = "L") -> ModelInfo:
    return ModelInfo(
        id=id_,
        provider=provider,
        wire_id=id_.split("/")[-1],
        display_name=id_,
        context_window=200_000,
        input_price_per_mtok=Decimal("3"),
        output_price_per_mtok=Decimal("15"),
        cost_tier="standard",
        energy_class=energy_class,
    )


MODEL_ID = "anthropic/claude-test"
MODEL = make_model(MODEL_ID)
LOCAL_MODEL_ID = "local/qwen-test"
LOCAL_MODEL = make_model(LOCAL_MODEL_ID, provider="local", energy_class="S")
# Never registered in FAKE_CATALOG below — the "a run's model disappeared from
# the catalog" case. Still a real ModelInfo so a plausible stored accounting
# can be built for it at seed time (that construction never touches the live
# app's catalog).
GHOST_MODEL = make_model("ghost/nope")

FAKE_CATALOG = FakeCatalog({MODEL_ID: MODEL, LOCAL_MODEL_ID: LOCAL_MODEL})
# `emissions_baseline_model` pinned to MODEL_ID: every run in these tests that
# uses MODEL_ID is therefore compared against itself (avoided == 0 by
# construction), which is not what this endpoint's tests are about.
FIXED_SETTINGS = Settings(emissions_baseline_model=MODEL_ID)


def _account(model: ModelInfo, settings: Settings, tokens: tuple[int, int, int, int]) -> dict:
    return energy_accounting(model, *tokens, settings=settings, catalog=FAKE_CATALOG)


# ── sqlite app fixtures ───────────────────────────────────────────────────────
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


@pytest.fixture(autouse=True)
def _reset_extension_registry():
    """Every test starts with no workspace gate registered, same as a fresh
    process — `check_workspace_gate` opens its own session against the real
    process-wide engine (see `ExtensionAPI.check_workspace_gate`'s docstring),
    never this test's sqlite one, so a gate function must never touch the
    session it is handed; the ones registered below don't.
    """
    extensions_module._registry = None
    yield
    extensions_module._registry = None


@pytest_asyncio.fixture
async def client(session_factory, monkeypatch):
    monkeypatch.setattr(analytics_module, "get_catalog", lambda: FAKE_CATALOG)
    monkeypatch.setattr(analytics_module, "get_settings", lambda: FIXED_SETTINGS)
    monkeypatch.setattr(emissions_module, "get_settings", lambda: FIXED_SETTINGS)
    monkeypatch.setattr(emission_factors_module, "get_settings", lambda: FIXED_SETTINGS)

    app = FastAPI()
    app.include_router(auth.router)
    app.include_router(analytics_module.router)

    async def _get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = _get_db
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def make_workspace(name: str) -> Workspace:
    return Workspace(name=name, kind="team")


def make_user(email: str) -> User:
    return User(
        email=email,
        display_name=email.split("@")[0].title(),
        password_hash=HASHER.hash(PASSWORD),
        role="analyst",
    )


def make_member(user: User, workspace: Workspace, *, role: str = "owner") -> WorkspaceMember:
    return WorkspaceMember(user_id=user.id, workspace_id=workspace.id, role=role)


def make_run(
    *,
    project_id,
    harness_id,
    created_by,
    model_used: str,
    accounting: dict | None,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    model_timeline: list | None = None,
) -> Run:
    return Run(
        project_id=project_id,
        harness_id=harness_id,
        task_type="freeform",
        task_input={},
        document_ids=[],
        status="completed",
        messages=[],
        created_by=created_by,
        model_used=model_used,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        energy_wh=Decimal(str(accounting["energy_wh"])) if accounting else None,
        energy_accounting=accounting,
        model_timeline=model_timeline,
        created_at=utcnow(),
    )


async def login(client: httpx.AsyncClient, email: str) -> None:
    response = await client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text


async def make_tenant(seed, *, name: str, email: str):
    """One fully-seeded workspace: a user (its sole member), a project, a
    harness. Returned as `(workspace, user, project, harness)`.

    Seeded in two passes: `workspace`/`user` first so their (Python-side,
    default=uuid.uuid4) primary keys are assigned by the flush inside `seed`
    before anything below reads `workspace.id` — reading it any earlier would
    read the `None` the column has before that default runs.
    """
    workspace = make_workspace(name)
    user = make_user(email)
    await seed(workspace, user)

    project = Project(workspace_id=workspace.id, name=f"{name} Project")
    harness = Harness(
        workspace_id=workspace.id,
        name=f"{name} Harness",
        task_profile="freeform",
        model_policy={"mode": "auto"},
        tool_names=[],
    )
    await seed(project, harness, make_member(user, workspace))
    return workspace, user, project, harness


# ── identity scenario ─────────────────────────────────────────────────────────
async def test_identity_scenario_matches_recorded_and_zeroes_the_delta(client, seed):
    _, user, project, harness = await make_tenant(seed, name="Co", email="identity@example.com")
    tokens = (1_000_000, 200_000, 0, 0)
    accounting = _account(MODEL, FIXED_SETTINGS, tokens)
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=accounting,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run)

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["runs_recomputed"] == 1
    assert body["runs_skipped"] == 0
    assert body["scenario"]["totals"]["co2e_g"] == pytest.approx(body["recorded"]["totals"]["co2e_g"])
    assert body["scenario"]["totals"]["energy_wh"] == pytest.approx(
        body["recorded"]["totals"]["energy_wh"]
    )
    assert body["delta"]["co2e_g"] == pytest.approx(0.0, abs=1e-6)
    assert body["delta"]["co2e_pct"] == 0
    assert body["delta"]["energy_wh"] == pytest.approx(0.0, abs=1e-6)
    assert body["delta"]["avoided_usd"] == pytest.approx(0.0, abs=1e-6)
    assert "computed, not recorded" in body["basis"]
    assert "harness" in body["scenario"]["layer_note"]


# ── a lower scenario grid factor ──────────────────────────────────────────────
async def test_scenario_grid_override_lowers_co2e_and_leaves_energy_unchanged(client, seed):
    _, user, project, harness = await make_tenant(seed, name="Co", email="grid@example.com")
    tokens = (1_000_000, 200_000, 0, 0)
    accounting = _account(MODEL, FIXED_SETTINGS, tokens)
    # Sanity: the fixed default is well above the scenario's 42, so the drop
    # below is not an artifact of which default the environment happens to have.
    assert FIXED_SETTINGS.grid_co2e_g_per_kwh > 42
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=accounting,
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run)

    await login(client, user.email)
    response = await client.post(
        "/api/analytics/emissions/whatif",
        json={"factors": {"grid": {"default": {"g_per_kwh": 42, "label": "scenario grid"}}}},
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["scenario"]["totals"]["co2e_g"] < body["recorded"]["totals"]["co2e_g"]
    # The grid factor does not change how much energy was drawn.
    assert body["scenario"]["totals"]["energy_wh"] == pytest.approx(
        body["recorded"]["totals"]["energy_wh"]
    )
    assert body["delta"]["co2e_g"] < 0
    assert isinstance(body["delta"]["co2e_pct"], int)
    assert body["delta"]["co2e_pct"] < 0


# ── B3: model_overrides in the scenario document actually apply ──────────────
async def test_scenario_model_override_prices_the_run_at_the_override_value(client, seed):
    """`_whatif_accounting`'s `fs_cache` used to key on `provider` alone and
    never pass `model_id` to `build_factor_set`, so a `model_overrides` entry
    for the run's own model — in the scenario document, which lands at the
    `harness` layer for a what-if recompute — could never match. Fixed by
    keying the cache on `(provider, model_id)` and threading `model_id`
    through.
    """
    _, user, project, harness = await make_tenant(seed, name="Co", email="modeloverride@example.com")
    tokens = (1_000_000, 200_000, 0, 0)
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, FIXED_SETTINGS, tokens),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run)

    await login(client, user.email)
    response = await client.post(
        "/api/analytics/emissions/whatif",
        json={
            "factors": {
                "model_overrides": {
                    MODEL_ID: {
                        "energy_wh_per_mtok": 42.0,
                        "label": "test scenario override",
                        "confidence": "measured",
                    }
                }
            }
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["runs_recomputed"] == 1
    assert body["scenario"]["totals"]["energy_wh"] != pytest.approx(
        body["recorded"]["totals"]["energy_wh"]
    )


# ── a workspace document that no longer validates ─────────────────────────────
async def test_a_broken_workspace_document_is_excluded_with_a_warning(client, seed, session_factory):
    """A stored workspace override document that no longer validates must not
    500 a read-only recompute — it is treated as no workspace layer for this
    scenario, and the response names why in `warnings`.
    """
    workspace, user, project, harness = await make_tenant(
        seed, name="Co", email="brokenworkspace@example.com"
    )
    async with session_factory() as db:
        row = await db.get(Workspace, workspace.id)
        # Missing the required `label` — fails `GridBlock._labels_required`.
        row.settings = {
            "emissions": {"grid": {"default": {"g_per_kwh": 42, "basis": "location_based"}}}
        }
        await db.commit()

    tokens = (1_000_000, 200_000, 0, 0)
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, FIXED_SETTINGS, tokens),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run)

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["runs_recomputed"] == 1
    assert body.get("warnings"), "expected a warnings entry naming the broken document"
    assert any("no longer validates" in w for w in body["warnings"])


# ── a run whose model left the catalog ────────────────────────────────────────
async def test_run_with_unknown_model_is_skipped_from_both_sides(client, seed):
    _, user, project, harness = await make_tenant(seed, name="Co", email="ghost@example.com")
    good_tokens = (1_000_000, 200_000, 0, 0)
    good_run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, FIXED_SETTINGS, good_tokens),
        input_tokens=good_tokens[0],
        output_tokens=good_tokens[1],
    )
    ghost_tokens = (500_000, 100_000, 0, 0)
    ghost_run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used="ghost/nope",
        accounting=_account(GHOST_MODEL, FIXED_SETTINGS, ghost_tokens),
        input_tokens=ghost_tokens[0],
        output_tokens=ghost_tokens[1],
    )
    await seed(good_run, ghost_run)

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["runs_skipped"] == 1
    assert body["runs_recomputed"] == 1
    # Excluded from BOTH sides, not just scenario — the two totals must cover
    # the same population of runs.
    assert body["recorded"]["totals"]["runs"] == 1
    assert body["scenario"]["totals"]["runs"] == 1
    assert "no longer in the catalog" in body["basis"]


# ── mixed grid bases in the window ────────────────────────────────────────────
async def test_mixed_grid_bases_in_the_window_nulls_the_carbon_delta(client, seed):
    _, user, project, harness = await make_tenant(seed, name="Co", email="basis@example.com")
    tokens = (1_000_000, 200_000, 0, 0)
    location_settings = FIXED_SETTINGS.model_copy(update={"grid_co2e_basis": "location_based"})
    market_settings = FIXED_SETTINGS.model_copy(update={"grid_co2e_basis": "market_based"})
    run_location = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, location_settings, tokens),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    run_market = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, market_settings, tokens),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run_location, run_market)

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["recorded"]["totals"]["carbon_is_summable"] is False
    assert body["recorded"]["totals"]["co2e_g"] is None
    assert body["delta"]["co2e_g"] is None
    assert body["delta"]["co2e_pct"] is None
    # Energy and dollars stay populated — those are summable across bases.
    assert body["delta"]["energy_wh"] is not None


# ── validation ────────────────────────────────────────────────────────────────
async def test_invalid_factors_document_returns_422_naming_the_field(client, seed):
    _, user, _project, _harness = await make_tenant(seed, name="Co", email="invalid@example.com")
    await login(client, user.email)

    response = await client.post(
        "/api/analytics/emissions/whatif", json={"factors": {"pue": {"cloud": 0.5}}}
    )
    assert response.status_code == 422
    assert "pue.cloud" in response.json()["detail"]


async def test_days_bounds_are_enforced(client, seed):
    _, user, _project, _harness = await make_tenant(seed, name="Co", email="bounds@example.com")
    await login(client, user.email)

    too_low = await client.post("/api/analytics/emissions/whatif", json={"days": 0})
    assert too_low.status_code == 422
    too_high = await client.post("/api/analytics/emissions/whatif", json={"days": 3651})
    assert too_high.status_code == 422


# ── the workspace gate ────────────────────────────────────────────────────────
async def test_workspace_gate_refusal_is_403(client, seed):
    _, user, _project, _harness = await make_tenant(seed, name="Co", email="gate@example.com")

    async def veto(db, workspace_id, action):
        if action == "emissions_whatif":
            return GateResult(allowed=False, reason="paused", detail="billing is paused")
        return GateResult(allowed=True)

    ext = ExtensionAPI(None)
    ext.add_workspace_gate(veto)
    extensions_module._registry = ext

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 403
    assert response.json()["detail"]["reason"] == "paused"
    assert response.json()["detail"]["detail"] == "billing is paused"


# ── no writes, ever ───────────────────────────────────────────────────────────
async def test_recompute_never_writes_to_the_run(client, seed, session_factory):
    _, user, project, harness = await make_tenant(seed, name="Co", email="nowrites@example.com")
    tokens = (1_000_000, 200_000, 0, 0)
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, FIXED_SETTINGS, tokens),
        input_tokens=tokens[0],
        output_tokens=tokens[1],
    )
    await seed(run)

    def _snapshot(row: Run) -> dict:
        return {
            "energy_accounting": row.energy_accounting,
            "energy_wh": row.energy_wh,
            "model_used": row.model_used,
            "input_tokens": row.input_tokens,
            "output_tokens": row.output_tokens,
            "model_timeline": row.model_timeline,
            "status": row.status,
        }

    async with session_factory() as db:
        before = _snapshot((await db.execute(select(Run).where(Run.id == run.id))).scalar_one())

    await login(client, user.email)
    response = await client.post(
        "/api/analytics/emissions/whatif",
        json={"factors": {"grid": {"default": {"g_per_kwh": 42, "label": "scenario grid"}}}},
    )
    assert response.status_code == 200, response.text

    async with session_factory() as db:
        after = _snapshot((await db.execute(select(Run).where(Run.id == run.id))).scalar_one())
        row_count = len((await db.execute(select(Run))).scalars().all())

    assert after == before
    assert row_count == 1


# ── tenancy isolation ─────────────────────────────────────────────────────────
async def test_another_workspaces_runs_are_never_included(client, seed):
    _, user_a, project_a, harness_a = await make_tenant(seed, name="A", email="tenant-a@example.com")
    _, user_b, project_b, harness_b = await make_tenant(seed, name="B", email="tenant-b@example.com")

    tokens_a = (1_000_000, 200_000, 0, 0)
    tokens_b = (5_000_000, 900_000, 0, 0)
    accounting_a = _account(MODEL, FIXED_SETTINGS, tokens_a)
    run_a = make_run(
        project_id=project_a.id,
        harness_id=harness_a.id,
        created_by=user_a.id,
        model_used=MODEL_ID,
        accounting=accounting_a,
        input_tokens=tokens_a[0],
        output_tokens=tokens_a[1],
    )
    run_b = make_run(
        project_id=project_b.id,
        harness_id=harness_b.id,
        created_by=user_b.id,
        model_used=MODEL_ID,
        accounting=_account(MODEL, FIXED_SETTINGS, tokens_b),
        input_tokens=tokens_b[0],
        output_tokens=tokens_b[1],
    )
    await seed(run_a, run_b)

    await login(client, user_a.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["recorded"]["totals"]["runs"] == 1
    assert body["runs_recomputed"] == 1
    assert body["recorded"]["totals"]["co2e_g"] == pytest.approx(accounting_a["co2e_g"])


# ── a run that switched model mid-run ─────────────────────────────────────────
async def test_multi_model_run_is_recomputed_segment_by_segment(client, seed):
    """A run with `model_timeline` is mirrored the way `engine/harness.py`
    produced it: one `energy_accounting` call per segment, combined —
    never one call over the run's running totals against a single model.
    """
    _, user, project, harness = await make_tenant(seed, name="Co", email="timeline@example.com")
    seg1_tokens = (600_000, 100_000, 0, 0)
    seg2_tokens = (400_000, 100_000, 0, 0)
    seg1 = _account(MODEL, FIXED_SETTINGS, seg1_tokens)
    seg2 = _account(LOCAL_MODEL, FIXED_SETTINGS, seg2_tokens)
    stored = combine_accountings([seg1, seg2])
    model_timeline = [
        {
            "model": MODEL_ID,
            "provider": "anthropic",
            "input_tokens": seg1_tokens[0],
            "output_tokens": seg1_tokens[1],
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
        {
            "model": LOCAL_MODEL_ID,
            "provider": "local",
            "input_tokens": seg2_tokens[0],
            "output_tokens": seg2_tokens[1],
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        },
    ]
    run = make_run(
        project_id=project.id,
        harness_id=harness.id,
        created_by=user.id,
        model_used=MODEL_ID,
        accounting=stored,
        input_tokens=seg1_tokens[0] + seg2_tokens[0],
        output_tokens=seg1_tokens[1] + seg2_tokens[1],
        model_timeline=model_timeline,
    )
    await seed(run)

    await login(client, user.email)
    response = await client.post("/api/analytics/emissions/whatif", json={"factors": {}})
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["runs_recomputed"] == 1
    assert body["runs_skipped"] == 0
    # Identity scenario over the same two segments must reproduce the stored total.
    assert body["scenario"]["totals"]["co2e_g"] == pytest.approx(body["recorded"]["totals"]["co2e_g"])
    assert body["scenario"]["totals"]["energy_wh"] == pytest.approx(
        body["recorded"]["totals"]["energy_wh"]
    )
