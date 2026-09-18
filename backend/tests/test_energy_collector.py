import asyncio
from decimal import Decimal
from types import SimpleNamespace
import pytest

from tret.services.energy_collector import CollectorPool, CollectedMeter
from tret.services.energy_meter import MeterReading
import tret.services.energy_collector as energy_collector


class FakeMeter:
    async def start(self):
        pass

    async def stop(self):
        return MeterReading(Decimal(10), 2, .1, "test", None, True, "gpu")

    def describe(self):
        return {"kind": "test"}


class CancelledStopMeter(FakeMeter):
    async def stop(self):
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_single_claim_measures_once_and_releases_host_lock(monkeypatch):
    ticks = iter((0.0, 0.1))
    monkeypatch.setattr(energy_collector, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    pool = CollectorPool(FakeMeter)
    claim = CollectedMeter(pool, {})
    await claim.start()
    await asyncio.sleep(.1)
    reading = await claim.stop()
    assert reading.wh == 10
    assert pool.file_lock is None
    assert await claim.stop() is None


@pytest.mark.asyncio
async def test_concurrent_shareless_claims_never_count_pool_twice():
    pool = CollectorPool(FakeMeter)
    a, b = CollectedMeter(pool, {}), CollectedMeter(pool, {})
    await a.start()
    await b.start()  # closes sole-a interval
    await asyncio.sleep(.1)
    assert await a.stop() is None  # overlap unallocated
    assert await b.stop() is None  # incomplete b cannot become a whole-run reading
    assert a.wh == 10
    assert b.wh == 10
    assert a.unallocated_wh == b.unallocated_wh == 10
    assert set(a.observation_ids) & set(b.observation_ids)
    assert pool.file_lock is None


@pytest.mark.asyncio
async def test_second_collector_cannot_observe_same_host(monkeypatch):
    ticks = iter((0.0, 0.0, 0.0, 0.1))
    monkeypatch.setattr(energy_collector, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    pool_a, pool_b = CollectorPool(FakeMeter), CollectorPool(FakeMeter)
    a, b = CollectedMeter(pool_a, {}), CollectedMeter(pool_b, {})
    await a.start()
    await b.start()
    await asyncio.sleep(.1)
    assert await b.stop() is None
    assert b.status == "collector_unavailable"
    assert (await a.stop()).wh == 10


@pytest.mark.asyncio
async def test_slow_start_is_outside_the_claim_interval(monkeypatch):
    ticks = iter((0.0, 0.1))
    monkeypatch.setattr(energy_collector, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    class SlowMeter(FakeMeter):
        async def start(self):
            await asyncio.sleep(.02)

        async def stop(self):
            return MeterReading(Decimal(".0001"), 2, .1, "test", None, True, "gpu")

    pool = CollectorPool(SlowMeter)
    claim = CollectedMeter(pool, {"interval_s": .2})
    await claim.start()
    await asyncio.sleep(.1)
    assert (await claim.stop()).complete is True
    assert claim.describe()["complete"] is True
    assert claim.describe()["missing_duration_s"] <= .005
    assert pool.file_lock is None


@pytest.mark.asyncio
async def test_short_claim_tolerance_is_capped_at_the_claims_own_duration(monkeypatch):
    ticks = iter((0.0, 0.1))
    monkeypatch.setattr(energy_collector, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    pool = CollectorPool(FakeMeter)
    # A coarse configured interval (5s) must not hand a 0.1s claim a 5s
    # tolerance — that would let a claim with almost no coverage at all read
    # as complete purely because the interval is coarse. The tolerance is
    # capped at the claim's own duration instead.
    claim = CollectedMeter(pool, {"interval_s": 5})
    await claim.start()
    await asyncio.sleep(.1)
    await claim.stop()
    assert claim.coverage_tolerance_s == pytest.approx(0.1)


@pytest.mark.asyncio
async def test_cancelled_meter_stop_marks_claim_incomplete_and_releases_lock():
    pool = CollectorPool(CancelledStopMeter)
    claim = CollectedMeter(pool, {})
    await claim.start()
    with pytest.raises(asyncio.CancelledError):
        await claim.stop()
    assert claim.complete is False
    assert claim.status == "meter_failure"
    assert pool.file_lock is None
    assert pool.active == {}
