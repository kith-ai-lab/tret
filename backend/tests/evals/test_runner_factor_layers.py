"""The runner snapshots a workspace's emissions factor layer at run start
(`tret/engine/harness.py`'s use of `tret/services/emission_settings.py`).

Real engine, real database, same `world` fixture every other golden-run test
in this directory uses — the point here is specifically that a run picks up
`Workspace.settings["emissions"]` and records which layer actually won, not
the doctrine or tool behaviour `test_golden_runs.py` already covers.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest
from golden_world import GOLDEN_MODEL, _replay_registry
from test_golden_runs import PERIL, SITE, divergence_happy_script

from tret.config import get_settings
from tret.db.models import Harness, Project, Run, Workspace
from tret.engine import harness as harness_module
from tret.providers.catalog import ModelInfo
from tret.services.energy_meter import MeterReading


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


# ── measured energy: a local segment's meter reaches the persisted run ──────
#
# `HarnessEngine._start_meter`/`_stop_meter` (engine/harness.py) — a local
# model segment starts the configured meter, a cloud one never even asks for
# one. These tests script a fake `EnergyMeter` directly rather than a real
# `nvidia-smi` (that lives in tests/test_energy_meter.py) and drive the real
# engine end to end, the same pattern every other test in this file uses.

@dataclass
class _FakeMeter:
    """A duck-typed `EnergyMeter` a test fully controls: what `stop()` reads
    back, and whether `start()`/`stop()` raise instead."""

    wh: float = 12.5
    shared_device: bool = False
    samples: int = 4
    duration_s: float = 8.0
    fail_start: bool = False
    fail_stop: bool = False
    started: bool = dc_field(default=False, init=False)
    stopped: bool = dc_field(default=False, init=False)

    def describe(self) -> dict:
        return {"kind": "fake", "interval_s": 1.0, "notes": "test double"}

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("boom: fake meter failed to start")

    async def stop(self) -> MeterReading | None:
        self.stopped = True
        if self.fail_stop:
            raise RuntimeError("boom: fake meter failed to stop")
        return MeterReading(
            wh=Decimal(str(self.wh)),
            samples=self.samples,
            duration_s=self.duration_s,
            kind="fake",
            note=None,
            shared_device=self.shared_device,
        )


def _local_model(model_id: str = "local/test-model") -> ModelInfo:
    return ModelInfo(
        id=model_id,
        provider="local",
        wire_id=model_id.split("/", 1)[1],
        display_name=model_id,
        context_window=32768,
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        cost_tier="local",
        supports_tools=False,
        curated=False,
    )


async def _run_freeform(world, *, model_id: str, catalog, provider, monkeypatch=None):
    """Build a freeform harness pinned to `model_id`, run one turn through
    it, and read the run back. Mirrors the ad-hoc `Harness`/`Run` construction
    B4's "Workspace B" side uses above — pinned mode never consults the LLM
    router, so a single `ScriptedTurn(text=...)` with no tool calls is enough.
    """
    from tret.engine.harness import HarnessEngine
    from tret.router_llm.priors import NoPriors

    async with world.session_factory() as db:
        harness = Harness(
            workspace_id=world.workspace_id,
            name="Measured Energy Harness",
            task_profile="freeform",
            model_policy={"mode": "pinned", "model": model_id},
            tool_names=[],
            loop_config={"max_iterations": 4, "max_output_tokens": 2048, "temperature": 0.0},
            created_by=world.user_id,
        )
        db.add(harness)
        await db.flush()
        run = Run(
            project_id=world.project_id,
            harness_id=harness.id,
            task_type="freeform",
            task_input={},
            created_by=world.user_id,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    engine = HarnessEngine(catalog=catalog, priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)):
        await engine.execute(run_id)
    return await world.read_back(run_id, provider=provider)


def _catalog_with_local(model: ModelInfo):
    from tret.providers.catalog import ModelCatalog

    catalog = ModelCatalog()
    catalog._local = {model.id: model}
    return catalog


async def test_a_local_run_with_a_fake_meter_records_measured_energy(world, monkeypatch):
    from replay_provider import ReplayProvider, ScriptedTurn

    model = _local_model()
    fake_meter = _FakeMeter(wh=12.5, shared_device=True)
    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: fake_meter)

    provider = ReplayProvider([ScriptedTurn(text="All done.")])
    result = await _run_freeform(
        world, model_id=model.id, catalog=_catalog_with_local(model), provider=provider
    )
    run = result.run

    assert run.status == "completed", run.error
    assert fake_meter.started is True
    assert fake_meter.stopped is True
    assert run.energy_accounting["energy_source"] == "measured"
    assert run.energy_accounting["energy_wh"] == pytest.approx(12.5)
    assert float(run.energy_wh) == pytest.approx(12.5)

    meter_block = run.energy_accounting["energy_meter"]
    assert meter_block["kind"] == "fake"
    assert meter_block["samples"] == 4
    assert meter_block["duration_s"] == pytest.approx(8.0)
    assert meter_block["interval_s"] == pytest.approx(1.0)
    assert meter_block["shared_device"] is True

    caveat_keys = {c["key"] for c in run.energy_accounting["caveats"]}
    assert "shared_device_measurement" in caveat_keys
    shared_caveat = next(
        c for c in run.energy_accounting["caveats"] if c["key"] == "shared_device_measurement"
    )
    assert shared_caveat["direction"] == "overstates"
    assert shared_caveat["applies"] is True


async def test_a_cloud_run_never_starts_the_meter(world, monkeypatch):
    """`meter_for_settings` is checked only after `deployment_for` confirms a
    local provider — a cloud-only run must never even call it."""
    from replay_provider import ReplayProvider

    calls: list[object] = []

    def _spy(settings):
        calls.append(settings)
        return _FakeMeter()  # would blow up the test if it were ever used

    monkeypatch.setattr(harness_module, "meter_for_settings", _spy)

    provider = ReplayProvider(divergence_happy_script())
    result = await world.run(
        provider=provider,
        task_type="divergence_assessment",
        task_input={"site_id": SITE, "peril": PERIL},
    )
    run = result.run

    assert run.status == "completed", run.error
    assert calls == []  # never called for GOLDEN_MODEL (anthropic, cloud)
    assert run.energy_accounting["energy_source"] == "estimated"
    assert "energy_meter" not in run.energy_accounting


async def test_a_meter_that_raises_still_completes_with_the_estimate(world, monkeypatch):
    from replay_provider import ReplayProvider, ScriptedTurn

    model = _local_model("local/flaky-model")
    fake_meter = _FakeMeter(fail_start=True, fail_stop=True)
    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: fake_meter)

    provider = ReplayProvider([ScriptedTurn(text="All done.")])
    result = await _run_freeform(
        world, model_id=model.id, catalog=_catalog_with_local(model), provider=provider
    )
    run = result.run

    assert run.status == "completed", run.error
    assert run.energy_accounting["energy_source"] == "estimated"
    assert "energy_meter" not in run.energy_accounting


async def test_a_meter_that_returns_none_falls_back_to_the_estimate(world, monkeypatch):
    """`stop()` returning `None` (a missing binary, an unreachable server) is
    not an error — it is the meter's own way of saying "nothing to report"."""
    from replay_provider import ReplayProvider, ScriptedTurn

    model = _local_model("local/no-reading-model")

    class _NoneMeter:
        def describe(self) -> dict:
            return {"kind": "fake", "interval_s": 1.0, "notes": ""}

        async def start(self) -> None:
            return None

        async def stop(self):
            return None

    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: _NoneMeter())

    provider = ReplayProvider([ScriptedTurn(text="All done.")])
    result = await _run_freeform(
        world, model_id=model.id, catalog=_catalog_with_local(model), provider=provider
    )
    run = result.run

    assert run.status == "completed", run.error
    assert run.energy_accounting["energy_source"] == "estimated"
    assert "energy_meter" not in run.energy_accounting


# ── measured energy: the meter must not outlive the run that started it ────
#
# Blocker 1 (review of the measured-energy work): the unconditional
# `_stop_meter` call after the loop was not inside a `try/finally`, so any
# exception out of the loop — or a cancellation of `execute()`'s own asyncio
# task, `CancelledError` being a `BaseException` `execute()`'s own
# `except Exception` never catches — skipped it, leaving a real meter's
# background sampling task (`NvidiaSmiMeter._loop`) forking `nvidia-smi`
# forever. `_TaskSpyMeter` stands in for that shape: an actual background
# `asyncio.Task` in `start()`, cancelled and awaited in `stop()`, so a test
# can assert the task is really gone rather than only that `stop()` was
# called.


@dataclass
class _TaskSpyMeter:
    wh: float = 5.0
    started: bool = dc_field(default=False, init=False)
    stopped: bool = dc_field(default=False, init=False)
    _task: "asyncio.Task | None" = dc_field(default=None, init=False, repr=False)

    def describe(self) -> dict:
        return {"kind": "fake", "interval_s": 1.0, "notes": "test double"}

    async def start(self) -> None:
        self.started = True
        self._task = asyncio.create_task(asyncio.sleep(3600))

    async def stop(self) -> MeterReading | None:
        self.stopped = True
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        return MeterReading(
            wh=Decimal(str(self.wh)),
            samples=2,
            duration_s=1.0,
            kind="fake",
            note=None,
            shared_device=False,
        )

    @property
    def task_is_gone(self) -> bool:
        return self._task is None


async def test_an_exception_mid_loop_still_stops_the_meters_background_task(world, monkeypatch):
    """A generic engine/provider exception — not the `ProviderError` the loop
    already knows how to recover from — must still stop the current
    segment's meter on its way out."""
    model = _local_model()
    spy_meter = _TaskSpyMeter()
    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: spy_meter)

    class _CrashingProvider:
        name = "crashing"

        async def stream(self, **kwargs):
            raise RuntimeError("boom: simulated engine bug")
            yield  # pragma: no cover - unreachable; keeps this an async generator

        async def complete_json(self, **kwargs):
            raise AssertionError("complete_json should not be reached in this test")

    result = await _run_freeform(
        world,
        model_id=model.id,
        catalog=_catalog_with_local(model),
        provider=_CrashingProvider(),
    )
    run = result.run

    assert run.status == "failed"
    assert "boom" in (run.error or "")
    assert spy_meter.started is True
    assert spy_meter.stopped is True
    assert spy_meter.task_is_gone


async def test_cancelling_the_execute_task_still_stops_the_meter(world, monkeypatch):
    """A client disconnecting, or the process shutting down, cancels the
    asyncio task running `execute()` directly — `CancelledError` never
    reaches `execute()`'s `except Exception`. The meter must still be
    stopped by `_execute_inner`'s own `finally`."""
    from tret.engine.harness import HarnessEngine
    from tret.router_llm.priors import NoPriors

    model = _local_model()
    spy_meter = _TaskSpyMeter()
    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: spy_meter)

    provider_entered = asyncio.Event()

    class _HangingProvider:
        name = "hanging"

        async def stream(self, **kwargs):
            provider_entered.set()
            await asyncio.Event().wait()  # never resolves on its own
            return
            yield  # pragma: no cover - unreachable; keeps this an async generator

        async def complete_json(self, **kwargs):
            raise AssertionError("complete_json should not be reached in this test")

    async with world.session_factory() as db:
        harness = Harness(
            workspace_id=world.workspace_id,
            name="Measured Energy Cancel Harness",
            task_profile="freeform",
            model_policy={"mode": "pinned", "model": model.id},
            tool_names=[],
            loop_config={"max_iterations": 4, "max_output_tokens": 2048, "temperature": 0.0},
            created_by=world.user_id,
        )
        db.add(harness)
        await db.flush()
        run = Run(
            project_id=world.project_id,
            harness_id=harness.id,
            task_type="freeform",
            task_input={},
            created_by=world.user_id,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    engine = HarnessEngine(catalog=_catalog_with_local(model), priors=NoPriors())
    with patch("tret.engine.harness.ProviderRegistry", _replay_registry(_HangingProvider())):
        task = asyncio.create_task(engine.execute(run_id))
        await asyncio.wait_for(provider_entered.wait(), timeout=5.0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert spy_meter.started is True
    assert spy_meter.stopped is True
    assert spy_meter.task_is_gone


# ── measured energy: a switch to an unmetered segment must not discard it ──
#
# Blocker 2: the post-loop recompute used to be gated on the FINAL segment's
# own `meter_reading` — so a local (metered) segment, switched to cloud, and
# cancelled before the cloud segment ever booked a turn, discarded the local
# segment's measurement entirely: the run persisted whatever the last
# `_book_usage` call had booked, which was still the per-token *estimate*
# (the meter had not stopped yet when that turn was booked).


async def test_switching_to_cloud_then_cancelling_still_recomputes_from_the_metered_segment(
    world, monkeypatch
):
    from replay_provider import ReplayProvider, ScriptedCall, ScriptedTurn
    from tret.engine.harness import HarnessEngine
    from tret.engine.supervisor import KIND_SWITCH, Intervention
    from tret.providers.catalog import ModelCatalog
    from tret.router_llm.priors import NoPriors

    # `_local_model()`'s default (`supports_tools=False`) is fine for every
    # other test here — they all pin the model, bypassing the router's
    # candidate filter entirely. This one needs `mode: "auto"` so the forced
    # switch below has somewhere to switch *from*, and the router's own
    # `_candidates()` drops any model with `supports_tools=False` before this
    # harness's `allowed` list is even consulted — so the local model needs
    # one of its own here, not the shared helper's.
    local_model = ModelInfo(
        id="local/switchable-model",
        provider="local",
        wire_id="switchable-model",
        display_name="local/switchable-model",
        context_window=32768,
        input_price_per_mtok=Decimal("0"),
        output_price_per_mtok=Decimal("0"),
        cost_tier="local",
        supports_tools=True,
        curated=False,
    )
    cloud_model = next(m for m in ModelCatalog().all(curated_only=True) if m.provider != "local")
    catalog = _catalog_with_local(local_model)

    fake_meter = _FakeMeter(wh=7.5, shared_device=True)
    monkeypatch.setattr(harness_module, "meter_for_settings", lambda settings: fake_meter)

    async with world.session_factory() as db:
        harness = Harness(
            workspace_id=world.workspace_id,
            name="Measured Energy Switch-Then-Cancel Harness",
            task_profile="freeform",
            # `allowed` names only the local model: "auto" with a single
            # candidate is what makes `route()` choose it deterministically
            # (its own `len(candidates) == 1` shortcut, no LLM router call
            # needed) — the point of this test is what happens *after* a
            # switch, not which model routing would otherwise have picked.
            # `fake_assess` below names the cloud model as its switch target
            # directly; the engine does not require a switch target to be a
            # member of `allowed` (`test_model_switch.py`'s own fake picks
            # from `candidates` only by its own convention, not an engine
            # rule), so this restriction is exactly as effective at forcing
            # the starting model as a two-candidate list would be, without
            # depending on `ReplayProvider.complete_json`'s "first candidate"
            # fallback picking the one this test needs first.
            model_policy={"mode": "auto", "allowed": [local_model.id]},
            # A tool call, not a bare text answer: a freeform turn with no tool
            # calls is treated as the model's final answer and breaks the loop
            # before the switch/supervisor section ever runs (see
            # `_execute_inner`'s "not tool_calls" branch) — this harness needs
            # to reach that section on iteration 1 for the forced switch below
            # to happen at all. `file_data_request` needs no seeded data.
            tool_names=["file_data_request"],
            loop_config={"max_iterations": 4, "max_output_tokens": 2048, "temperature": 0.0},
            created_by=world.user_id,
        )
        db.add(harness)
        await db.flush()
        run = Run(
            project_id=world.project_id,
            harness_id=harness.id,
            task_type="freeform",
            task_input={},
            created_by=world.user_id,
        )
        db.add(run)
        await db.commit()
        run_id = run.id

    engine = HarnessEngine(catalog=catalog, priors=NoPriors())

    def fake_assess(state, *, candidates, priors=None):
        # Force the switch the moment it is first checked, and cancel the run
        # in that same beat: the next iteration's top-of-loop cancellation
        # check fires before the new (cloud) segment ever gets a turn.
        engine.cancel(run_id)
        return Intervention(
            kind=KIND_SWITCH,
            target=cloud_model,
            reason="capability_stall",
            detail="forced by the test",
            evidence={"from": state.model.id, "to": cloud_model.id},
        )

    provider = ReplayProvider(
        [
            ScriptedTurn(
                text="Working on it.",
                tool_calls=[
                    ScriptedCall(
                        "file_data_request",
                        {
                            "subject": {"kind": "test"},
                            "what_is_missing": "n/a",
                            "why_needed": "exercising the switch-then-cancel path",
                        },
                    )
                ],
            )
        ]
    )
    with (
        patch("tret.engine.harness.assess", side_effect=fake_assess),
        patch("tret.engine.harness.ProviderRegistry", _replay_registry(provider)),
    ):
        await engine.execute(run_id)

    result = await world.read_back(run_id, provider=provider)
    run = result.run

    assert run.status == "cancelled"
    assert fake_meter.started is True
    assert fake_meter.stopped is True
    # The cloud segment never booked a single turn, so it did no work and does
    # not count toward the run's energy source: the run is cleanly "measured",
    # and the local segment's real 7.5 Wh reading is what is persisted, not the
    # per-token estimate `_book_usage` had booked before the meter stopped.
    assert run.energy_accounting["energy_source"] == "measured"
    assert run.energy_accounting["energy_wh"] == pytest.approx(7.5)
    assert float(run.energy_wh) == pytest.approx(7.5)
    meter_block = run.energy_accounting["energy_meter"]
    assert meter_block["kind"] == "fake"
    assert meter_block["shared_device"] is True
    # `model_timeline` is now rewritten on every recompute, single segment or
    # not — its first entry is the run's own record of what the local segment
    # actually measured, and must agree with the roll-up above.
    assert run.model_timeline is not None
    assert run.model_timeline[0]["model"] == local_model.id
    assert run.model_timeline[0]["energy_accounting"]["energy_wh"] == pytest.approx(7.5)
