"""The runner snapshots a workspace's emissions factor layer at run start
(`tret/engine/harness.py`'s use of `tret/services/emission_settings.py`).

Real engine, real database, same `world` fixture every other golden-run test
in this directory uses — the point here is specifically that a run picks up
`Workspace.settings["emissions"]` and records which layer actually won, not
the doctrine or tool behaviour `test_golden_runs.py` already covers.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import patch

from golden_world import GOLDEN_MODEL, _replay_registry
from test_golden_runs import PERIL, SITE, divergence_happy_script

from tret.config import get_settings
from tret.db.models import Harness, Project, Run, Workspace
from tret.engine import harness as harness_module


async def _set_workspace_emissions(world, doc: dict) -> None:
    async with world.session_factory() as db:
        workspace = await db.get(Workspace, world.workspace_id)
        settings = dict(workspace.settings or {})
        settings["emissions"] = doc
        workspace.settings = settings
        await db.commit()


async def test_a_workspace_override_is_recorded_as_the_workspace_layer(world):
    from replay_provider import ReplayProvider

    await _set_workspace_emissions(
        world,
        {
            "grid": {
                "default": {
                    "g_per_kwh": 90,
                    "basis": "location_based",
                    "label": "test workspace override",
                }
            }
        },
    )

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error
    assert run.energy_accounting["grid_co2e_layer"] == "workspace"
    assert run.energy_accounting["grid_co2e_g_per_kwh"] == 90.0


async def test_a_workspace_hourly_table_is_recorded_as_hourly(world):
    """A workspace `grid.tables` entry referenced by the default entry
    applies at the run's actual start time — a 24-row diurnal profile always
    has a value for whatever hour the run happens to start in, so this needs
    no clock mocking to assert `grid_temporal == "hourly"`.
    """
    from replay_provider import ReplayProvider

    diurnal_rows = "\n".join(f"{h},{100 + h}" for h in range(24))
    await _set_workspace_emissions(
        world,
        {
            "grid": {
                "default": {
                    "g_per_kwh": 50,
                    "basis": "location_based",
                    "label": "annual fallback",
                    "table": "diurnal",
                },
                "tables": {
                    "diurnal": {
                        "label": "test diurnal profile",
                        "basis": "location_based",
                        "csv": "hour_utc,g_per_kwh\n" + diurnal_rows,
                    }
                },
            }
        },
    )

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error
    assert run.energy_accounting["grid_temporal"] == "hourly"
    assert run.energy_accounting["factors"]
    grid_factor = next(f for f in run.energy_accounting["factors"] if f["key"] == "grid_intensity")
    assert grid_factor["table"] == "diurnal"


async def test_a_naive_stored_created_at_is_treated_as_utc_not_now(world):
    """SQLite (what `world` runs on) has no genuine timezone-aware storage, so
    a `Run.created_at` round-trips naive there (see `reconcile.py`'s own
    `_as_aware_utc` docstring). The runner must treat that naive value as
    already being UTC — the same way the what-if endpoint's `_aware_utc`
    treats a naive stored `created_at` — never substitute `_utcnow()` for it:
    an hourly `grid.tables` entry would otherwise resolve against whatever
    hour the run happens to *execute* in rather than the hour it was actually
    created at.
    """
    from replay_provider import ReplayProvider
    from tret.engine.harness import HarnessEngine
    from tret.providers.catalog import ModelCatalog
    from tret.router_llm.priors import NoPriors

    diurnal_rows = "\n".join(f"{h},{100 + h}" for h in range(24))
    await _set_workspace_emissions(
        world,
        {
            "grid": {
                "default": {
                    "g_per_kwh": 50,
                    "basis": "location_based",
                    "label": "annual fallback",
                    "table": "diurnal",
                },
                "tables": {
                    "diurnal": {
                        "label": "test diurnal profile",
                        "basis": "location_based",
                        "csv": "hour_utc,g_per_kwh\n" + diurnal_rows,
                    }
                },
            }
        },
    )

    harness_id = await world.create_harness(tool_names=[])
    run_id = await world.create_run(
        harness_id=harness_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    # A naive `created_at`, years in the past and at an hour (3am UTC ->
    # 100 + 3 = 103) nothing near "now" would coincide with by accident.
    frozen_naive = datetime(2020, 1, 1, 3, 0, 0)
    async with world.session_factory() as db:
        run = await db.get(Run, run_id)
        run.created_at = frozen_naive
        await db.commit()
        assert run.created_at.tzinfo is None  # sanity: genuinely naive in storage

    provider = ReplayProvider(divergence_happy_script())
    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)

    result = await world.read_back(run_id, provider=provider)
    run = result.run
    assert run.status == "completed", run.error
    assert run.energy_accounting["grid_temporal"] == "hourly"
    grid_factor = next(f for f in run.energy_accounting["factors"] if f["key"] == "grid_intensity")
    assert grid_factor["value"] == 103.0  # hour 3 -> 100 + 3, from the frozen created_at


async def test_with_no_workspace_doc_the_layer_falls_back_to_env_or_global_default(world):
    """No `Workspace.settings["emissions"]` at all — the workspace layer
    contributes nothing, so the grid factor falls through to whichever of
    `env`/`global_default` this process's own `Settings` would already give
    it (see `emission_factors.py`'s `_env_or_global`)."""
    from replay_provider import ReplayProvider

    settings = get_settings()
    expected_layer = (
        "env" if "grid_co2e_g_per_kwh" in settings.model_fields_set else "global_default"
    )

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error
    assert run.energy_accounting["grid_co2e_layer"] == expected_layer


async def test_a_raising_layer_load_still_completes_the_run(world, monkeypatch):
    """A broken workspace document, or any other failure loading the layers,
    must fall back to `factors=None` (today's behaviour) rather than take the
    run down — see the try/except around `workspace_emissions_layers` in
    `HarnessEngine.execute`."""
    from replay_provider import ReplayProvider

    async def _boom(db, workspace_id):
        raise RuntimeError("boom: simulated failure loading emissions layers")

    monkeypatch.setattr(harness_module, "workspace_emissions_layers", _boom)

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error
    # No layers loaded at all -> the same env/global_default fallback as the
    # no-doc case above, never a crash and never a stuck "workspace" layer.
    assert run.energy_accounting["grid_co2e_layer"] in ("env", "global_default")


# ── B3: model_overrides actually apply on a real run ─────────────────────────
async def test_a_workspace_model_override_for_the_runs_model_is_recorded(world):
    """`emission_settings.factor_set_for` used to have no `model_id` parameter
    and never passed one to `build_factor_set`, so a workspace's
    `model_overrides` entry for the run's own model could never match —
    `_factors_for` always resolved `model_override=None` regardless of what
    was configured. Fixed by threading `model_info.id` through at both call
    sites in `harness.py`.
    """
    from replay_provider import ReplayProvider

    await _set_workspace_emissions(
        world,
        {
            "model_overrides": {
                GOLDEN_MODEL: {
                    "energy_wh_per_mtok": 42.0,
                    "label": "metered on our own box",
                    "confidence": "measured",
                }
            }
        },
    )

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run
    assert run.status == "completed", run.error
    record = next(f for f in run.energy_accounting["factors"] if f["key"] == "energy_class")
    assert record["value"] == 42.0
    assert record["strategy"] == "measured"
    assert record["confidence"] == "measured"
    assert record["layer"] == "workspace"


# ── B4: per-run emissions state must never live on the shared engine ────────
async def test_concurrent_runs_do_not_bleed_workspace_emissions_layers(world, monkeypatch):
    """Two runs, in two different workspaces, sharing one `HarnessEngine`
    instance, executed concurrently: each must record ITS OWN workspace's
    grid factor, never the other's.

    Before the fix, `HarnessEngine.execute()` stored the run's workspace/
    managed documents on `self._emissions_workspace_doc` /
    `_emissions_managed_doc` — instance attributes on a process-wide engine.
    `_emissions_test_hook` (test-only, see `HarnessEngine.execute`'s
    docstring) interleaves the two calls deterministically at the narrowest
    window where that bug could bite: after run B has loaded and (pre-fix)
    overwritten the engine's shared documents, but before run A — paused at
    its own hook — has built its first segment from them.

    Does NOT touch the pre-existing `self.registry`/`self.router` race
    (`HarnessEngine.execute`'s own docstring notes it): the two runs use two
    different providers (`anthropic`, `kimi`), looked up by name from a fixed
    mapping, so whichever `execute()` call's `self.registry` reassignment
    "wins" the race still resolves each run's own provider correctly.
    """
    from replay_provider import ReplayProvider, ScriptedTurn
    from tret.engine.harness import HarnessEngine
    from tret.providers.catalog import ModelCatalog
    from tret.router_llm.priors import NoPriors

    await _set_workspace_emissions(
        world,
        {"grid": {"default": {"g_per_kwh": 90, "basis": "location_based", "label": "workspace A"}}},
    )

    async with world.session_factory() as db:
        workspace_b = Workspace(
            name="Workspace B",
            settings={
                "emissions": {
                    "grid": {
                        "default": {
                            "g_per_kwh": 900,
                            "basis": "location_based",
                            "label": "workspace B",
                        }
                    }
                }
            },
        )
        db.add(workspace_b)
        await db.flush()
        project_b = Project(workspace_id=workspace_b.id, name="Project B")
        harness_b = Harness(
            workspace_id=workspace_b.id,
            name="Harness B",
            task_profile="freeform",
            model_policy={"mode": "pinned", "model": "kimi/kimi-k2"},
            tool_names=[],
            loop_config={"max_iterations": 4, "max_output_tokens": 2048, "temperature": 0.0},
        )
        db.add_all([project_b, harness_b])
        await db.flush()
        run_b = Run(
            project_id=project_b.id,
            harness_id=harness_b.id,
            task_type="freeform",
            task_input={},
            created_by=world.user_id,
        )
        db.add(run_b)
        await db.commit()
        run_b_id = run_b.id

    harness_a_id = await world.create_harness(tool_names=[])  # pinned GOLDEN_MODEL (anthropic)
    run_a_id = await world.create_run(
        harness_id=harness_a_id,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )

    provider_a = ReplayProvider(divergence_happy_script())
    provider_b = ReplayProvider([ScriptedTurn(text="All done.")])
    providers_by_name = {"anthropic": provider_a, "kimi": provider_b}

    class _TwoProviderRegistry:
        """One replay provider per name — stands in for `ProviderRegistry`
        regardless of which run's `self.registry` reassignment lands last."""

        def __init__(self, db_keys=None):
            pass

        def has_key(self, provider_name: str) -> bool:
            return True

        def available_providers(self) -> list[str]:
            return list(providers_by_name)

        def get(self, provider_name: str):
            return providers_by_name[provider_name]

    monkeypatch.setattr(harness_module, "ProviderRegistry", _TwoProviderRegistry)

    engine = HarnessEngine(catalog=ModelCatalog(), priors=NoPriors())

    gate_a = asyncio.Event()
    started_b = asyncio.Event()

    async def hook_a():
        started_b.set()
        await gate_a.wait()

    async def hook_b():
        gate_a.set()

    task_a = asyncio.create_task(engine.execute(run_a_id, _emissions_test_hook=hook_a))
    await started_b.wait()
    task_b = asyncio.create_task(engine.execute(run_b_id, _emissions_test_hook=hook_b))
    await asyncio.gather(task_a, task_b)

    result_a = await world.read_back(run_a_id, provider=provider_a)
    result_b = await world.read_back(run_b_id, provider=provider_b)

    assert result_a.run.status == "completed", result_a.run.error
    assert result_b.run.status == "completed", result_b.run.error
    assert result_a.run.energy_accounting["grid_co2e_g_per_kwh"] == 90.0
    assert result_a.run.energy_accounting["grid_co2e_layer"] == "workspace"
    assert result_b.run.energy_accounting["grid_co2e_g_per_kwh"] == 900.0
    assert result_b.run.energy_accounting["grid_co2e_layer"] == "workspace"

    # No per-run emissions state lives on the engine instance itself.
    assert not hasattr(engine, "_emissions_workspace_doc")
    assert not hasattr(engine, "_emissions_managed_doc")
