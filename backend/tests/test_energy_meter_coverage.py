"""Coverage and optional cumulative-counter contracts for energy meters."""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal

import pytest

import tret.services.energy_meter as energy_meter
from tret.services.energy_meter import (
    MeterReading,
    NvidiaSmiMeter,
    NvmlCounterSnapshot,
    NvmlTotalEnergyAdapter,
)
from tret.services.energy_collector import CollectorPool, CollectedMeter


class _FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


async def test_long_sampling_gap_is_diagnostic_but_not_a_usable_reading(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0)
    meter._samples = [(0.0, 100.0), (1.0, 100.0), (5.0, 100.0), (6.0, 100.0)]
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "time", _FakeClock(6.0))

    reading = await meter.stop()

    # The current harness treats every returned reading as a complete measured
    # span, so fail closed until it explicitly supports partial allocation.
    assert reading is None
    diagnostic = meter._last_reading
    assert diagnostic is not None
    assert diagnostic.complete is False
    assert diagnostic.coverage == pytest.approx(2 / 6)
    assert diagnostic.covered_duration_s == pytest.approx(2.0)
    assert diagnostic.missing_duration_s == pytest.approx(4.0)
    assert diagnostic.wh == pytest.approx(Decimal("200") / Decimal("3600"))
    assert diagnostic.describe()["status"] == "incomplete"
    assert meter.describe()["status"] == "incomplete"


async def test_normal_series_exposes_complete_coverage(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0)
    meter._samples = [(0.0, 100.0), (1.0, 100.0)]
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "time", _FakeClock(1.5))

    reading = await meter.stop()

    assert reading is not None
    assert reading.complete is True
    assert reading.coverage == 1.0
    assert reading.covered_duration_s == pytest.approx(1.5)
    assert reading.missing_duration_s == 0.0


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"wh": Decimal("NaN")}, "wh"),
        ({"wh": Decimal("-1")}, "wh"),
        ({"duration_s": float("inf")}, "duration_s"),
        ({"coverage": float("nan")}, "coverage"),
        ({"coverage": 1.1}, "coverage"),
        ({"covered_duration_s": .1}, "reconcile"),
    ],
)
def test_meter_reading_rejects_invalid_numeric_values(kwargs, message):
    values = {
        "wh": Decimal("1"),
        "samples": 1,
        "duration_s": 1.0,
        "kind": "test",
        "note": None,
        "shared_device": False,
    }
    values.update(kwargs)
    with pytest.raises(ValueError, match=message):
        MeterReading(**values)


class _FakeNvml:
    def __init__(self, totals, device_ids=None) -> None:
        self.totals = iter(totals)
        self.device_ids = iter(device_ids or ["GPU-1"] * len(totals))
        self.initialized = False
        self.closed = False

    def nvmlInit(self):
        self.initialized = True

    def nvmlDeviceGetHandleByIndex(self, index):
        return index

    def nvmlDeviceGetUUID(self, handle):
        return next(self.device_ids)

    def nvmlDeviceGetTotalEnergyConsumption(self, handle):
        return next(self.totals)

    def nvmlShutdown(self):
        self.closed = True


def test_nvml_adapter_computes_valid_counter_delta(monkeypatch):
    backend = _FakeNvml([1_000, 3_000])
    adapter = NvmlTotalEnergyAdapter(backend=backend)
    clock = _FakeClock(10.0)
    monkeypatch.setattr(energy_meter, "time", clock)
    start = adapter.snapshot()
    clock.value = 12.0
    end = adapter.snapshot()

    delta = adapter.delta(start, end)

    assert backend.initialized is True
    assert delta.status == "complete"
    assert delta.complete is True
    assert delta.energy_wh == Decimal("2000") / Decimal("3600000")
    assert delta.duration_s == 2.0
    adapter.close()
    assert backend.closed is True


def test_nvml_adapter_detects_counter_reset_and_device_change():
    start = NvmlCounterSnapshot("GPU-1", 2_000, 1.0)
    reset = NvmlCounterSnapshot("GPU-1", 1_000, 2.0)
    changed = NvmlCounterSnapshot("GPU-2", 3_000, 2.0)

    assert NvmlTotalEnergyAdapter.delta(start, reset).status == "counter_reset"
    assert NvmlTotalEnergyAdapter.delta(start, changed).status == "device_changed"


def test_nvml_adapter_is_optional_and_reports_unavailable(monkeypatch):
    def _missing(name):
        raise ModuleNotFoundError(name)

    monkeypatch.setattr(energy_meter.importlib, "import_module", _missing)
    adapter = NvmlTotalEnergyAdapter()

    assert adapter.snapshot() is None
    assert adapter.status == "unavailable"
    assert "ModuleNotFoundError" in (adapter.detail or "")
    assert adapter.delta(None, None).status == "unavailable"


@pytest.mark.asyncio
async def test_nvml_meter_counter_and_reset_paths():
    from tret.services.energy_meter import NvmlEnergyMeter
    backend = _FakeNvml([1_000, 3_601_000])
    meter = NvmlEnergyMeter(adapter=NvmlTotalEnergyAdapter(backend=backend))
    await meter.start()
    reading = await meter.stop()
    assert reading.wh == 1
    assert reading.energy_boundary == "gpu"
    assert meter.describe()["device_ids"] == ["GPU-1"]
    assert backend.closed

    reset_backend = _FakeNvml([3_000, 1_000])
    reset = NvmlEnergyMeter(adapter=NvmlTotalEnergyAdapter(backend=reset_backend))
    await reset.start()
    assert await reset.stop() is None
    assert reset.describe()["status"] == "counter_reset"


@pytest.mark.asyncio
async def test_nvml_initial_unavailable_uses_labelled_sampled_fallback(monkeypatch):
    from tret.services.energy_meter import NvmlEnergyMeter
    def missing(_name):
        raise ModuleNotFoundError("optional binding absent")
    monkeypatch.setattr(energy_meter.importlib, "import_module", missing)
    class Sampled:
        def __init__(self, **_kw):
            pass
        async def start(self):
            pass
        async def stop(self):
            return MeterReading(Decimal(2), 2, 1, "nvidia_smi", None, True, "gpu")
        def describe(self):
            return {"kind": "nvidia_smi", "status": "complete"}
    monkeypatch.setattr(energy_meter, "NvidiaSmiMeter", Sampled)
    meter = NvmlEnergyMeter()
    await meter.start()
    assert (await meter.stop()).wh == 2
    assert meter.describe()["kind"] == "nvidia_smi"
    assert "optional binding absent" in meter.describe()["fallback_reason"]


@pytest.mark.asyncio
async def test_collector_excludes_slow_initial_nvidia_probe_from_claim():
    class SlowInitialProbe(NvidiaSmiMeter):
        async def _sample(self):
            await asyncio.sleep(.02)
            return 100.0

    pool = CollectorPool(lambda: SlowInitialProbe(interval_s=.2))
    claim = CollectedMeter(pool, {"interval_s": .2})
    await claim.start()
    await asyncio.sleep(1.0)
    reading = await claim.stop()

    assert reading is not None
    assert reading.complete is True
    assert reading.duration_s == pytest.approx(1.0, abs=.05)
    assert reading.covered_duration_s == pytest.approx(reading.duration_s, abs=.01)


@pytest.mark.asyncio
async def test_collector_rejects_slow_final_nvml_query_as_excess_coverage():
    """500ms of excess coverage on a ~1s claim must still be rejected.

    The claim runs long enough (>1s) that the tolerance's 10% cap applies —
    without it, the default 1s-interval tolerance would swallow the excess
    outright and this slow final query would wrongly read as a good
    measurement. See `test_collector_accepts_ordinary_jitter_within_tolerance`
    below for the jitter this tolerance exists to accept.
    """
    class DelayedEndNvml(_FakeNvml):
        def __init__(self):
            super().__init__([1_000, 3_601_000])
            self.calls = 0

        def nvmlDeviceGetTotalEnergyConsumption(self, handle):
            self.calls += 1
            if self.calls == 2:
                time.sleep(.5)
            return next(self.totals)

    from tret.services.energy_meter import NvmlEnergyMeter
    backend = DelayedEndNvml()
    pool = CollectorPool(
        lambda: NvmlEnergyMeter(adapter=NvmlTotalEnergyAdapter(backend=backend))
    )
    claim = CollectedMeter(pool, {})  # default 1.0s interval
    await claim.start()
    await asyncio.sleep(1.05)
    assert await claim.stop() is None
    assert claim.complete is False
    assert claim.status == "incomplete_claim_interval"
    assert claim.coverage_tolerance_s == pytest.approx(claim.describe()["duration_s"] * 0.1, abs=.01)
    assert pool.file_lock is None


@pytest.mark.asyncio
async def test_collector_accepts_ordinary_jitter_within_tolerance(monkeypatch):
    """A 1.5ms discrepancy on a 0.2s-interval claim is ordinary scheduler
    jitter, not a real gap, and must not discard the measurement."""
    import tret.services.energy_collector as energy_collector
    from types import SimpleNamespace

    ticks = iter((0.0, 0.2))
    monkeypatch.setattr(
        energy_collector, "time", SimpleNamespace(monotonic=lambda: next(ticks))
    )

    class JitteryMeter:
        async def start(self):
            pass

        async def stop(self):
            # 1.5ms more covered duration than the claim's own wall clock.
            return MeterReading(Decimal(10), 2, .2015, "test", None, True, "gpu")

        def describe(self):
            return {"kind": "test"}

    pool = CollectorPool(JitteryMeter)
    claim = CollectedMeter(pool, {"interval_s": .2})
    await claim.start()
    reading = await claim.stop()

    assert reading is not None
    assert claim.complete is True
    assert claim.status == "complete"
    assert claim.coverage_tolerance_s == pytest.approx(.2)
    assert claim.coverage_discrepancy_s == pytest.approx(.0015, abs=1e-6)
    assert pool.file_lock is None
