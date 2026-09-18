"""One sampled device pool per event loop, with cross-process exclusion.

Membership changes close the previous observation interval. With concurrent
shareless claims, the interval is unallocated and neither run substitutes a
partial measurement for its estimator. This deliberately favors honest missing
coverage over counting the same GPU board energy once for every request.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import os
from pathlib import Path
import tempfile
import time
import uuid
import weakref


class CollectorPool:
    def __init__(self, factory):
        self.factory = factory
        self.lock = asyncio.Lock()
        self.active = {}
        self.meter = None
        self.file_lock = None
        self.status = "not_started"

    def _acquire_host(self):
        # Every nvidia-smi index selection shares this lock: an all-device
        # collector must never overlap a per-index collector in another worker.
        try:
            import fcntl
            path = Path(tempfile.gettempdir()) / f"tret-energy-collector-{os.getuid()}.lock"
            fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)
            handle = os.fdopen(fd, "a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                handle.close()
                return False
            self.file_lock = handle
            return True
        except (ImportError, OSError):
            return False

    async def _flush(self):
        if self.meter is None:
            return
        meter, self.meter = self.meter, None
        try:
            reading = await meter.stop()
        except BaseException:
            for claim in self.active.values():
                claim.complete = False
                claim.status = "meter_failure"
            raise
        observation_id = str(uuid.uuid4())
        attributable = reading is not None and reading.complete and len(self.active) == 1
        for claim in self.active.values():
            claim.observation_ids.append(observation_id)
            claim.last_description = meter.describe()
            if attributable:
                claim.wh += reading.wh
                claim.samples += reading.samples
                claim.covered_s += reading.duration_s
                claim.boundary = reading.energy_boundary
                claim.kind = reading.kind
                claim.status = "complete"
            else:
                claim.complete = False
                claim.status = "unallocated_concurrency" if len(self.active) > 1 else "incomplete"
                if reading is not None:
                    claim.unallocated_wh += reading.wh

    async def _start_interval(self):
        if self.active and self.file_lock is not None:
            self.meter = self.factory()
            try:
                await self.meter.start()
            except BaseException:
                meter, self.meter = self.meter, None
                for claim in self.active.values():
                    claim.complete = False
                    claim.status = "meter_start_failure"
                # A cancelled/timed-out start may have opened a device session
                # or spawned a sampler.  Best-effort teardown keeps a failed
                # interval from leaking state into the next claim.
                if meter is not None:
                    try:
                        await asyncio.wait_for(meter.stop(), timeout=2.0)
                    except BaseException:
                        pass
                raise

    async def begin(self, claim):
        async with self.lock:
            if not self.active and not self._acquire_host():
                claim.complete = False
                claim.status = "collector_unavailable"
            try:
                await self._flush()
                self.active[claim.claim_id] = claim
                if self.file_lock is None:
                    claim.complete = False
                    claim.status = "collector_unavailable"
                await self._start_interval()
            except BaseException:
                self.active.pop(claim.claim_id, None)
                if not self.active:
                    self._release_host()
                raise

    def _release_host(self):
        if self.file_lock is not None:
            self.file_lock.close()
            self.file_lock = None

    async def end(self, claim):
        async with self.lock:
            if claim.claim_id not in self.active:
                return
            try:
                await self._flush()
            finally:
                self.active.pop(claim.claim_id, None)
                if self.active:
                    await self._start_interval()
                else:
                    self._release_host()


class CollectedMeter:
    def __init__(self, pool, description):
        self.pool = pool
        self.initial_description = description
        self.last_description = {}
        self.claim_id = str(uuid.uuid4())
        self.wh = Decimal(0)
        self.samples = 0
        self.covered_s = 0.0
        self.unallocated_wh = Decimal(0)
        self.complete = True
        self.status = "not_started"
        self.boundary = "gpu"
        self.kind = "unknown"
        self.observation_ids = []
        self.started = None
        self.stopped = None

    async def start(self):
        if self.started is not None:
            raise RuntimeError("collector claim has already started")
        await self.pool.begin(self)
        # The claim starts only after the pool has successfully established its
        # observation interval.  Collector setup latency must not become an
        # apparent uncovered workload span.
        self.started = time.monotonic()

    async def stop(self):
        if self.stopped is not None or self.started is None:
            return None
        # The workload ends before meter teardown. Include time waiting to
        # acquire/start the collector, but do not call teardown latency work.
        self.stopped = time.monotonic()
        await self.pool.end(self)
        duration = max(0, self.stopped - self.started)
        interval = float(self.initial_description.get("interval_s") or 1.0)
        # Tolerance tracks the sampling interval rather than a fixed
        # millisecond: the meter's own start/stop timestamps sit on the other
        # side of `task.cancel(); await task`, and an NVML final query is
        # timestamped after a `to_thread` hardware call at both ends (see
        # `NvmlTotalEnergyAdapter.snapshot`) — ordinary event-loop/thread
        # jitter on that scale must not discard a real measurement. Floored at
        # 50ms so a sub-second interval still has a usable allowance, and
        # capped at 10% of the claim's own duration once that duration exceeds
        # 1s, so a long claim still catches a genuine gap rather than letting
        # a coarse interval swallow it.
        base_tolerance = max(interval, 0.05)
        if duration > 1.0:
            self.coverage_tolerance_s = min(base_tolerance, duration * 0.1)
        else:
            # Capped at the claim's own duration: an allowance larger than
            # the claim itself would let a coarse interval wave through a
            # claim with no coverage at all (duration ~0).
            self.coverage_tolerance_s = min(base_tolerance, duration)
        # Recorded regardless of outcome — visible in `describe()`'s
        # diagnostics — so a rejected claim shows exactly what tolerance was
        # applied and how far coverage missed it, not just that it failed.
        self.coverage_discrepancy_s = abs(duration - self.covered_s)
        if self.coverage_discrepancy_s > self.coverage_tolerance_s:
            self.complete = False
            if self.status == "complete":
                self.status = "incomplete_claim_interval"
        if not self.complete or not self.observation_ids:
            return None
        from tret.services.energy_meter import MeterReading
        return MeterReading(self.wh, self.samples, self.covered_s, self.kind,
                            "Single-claim GPU board allocation; other applications are not observed.",
                            True, self.boundary)

    def describe(self):
        duration = max(0, (self.stopped or time.monotonic()) - self.started) if self.started else 0
        coverage = min(1.0, self.covered_s / duration) if duration else 0
        return self.initial_description | self.last_description | {
            "collector": "process_device_pool_v1", "allocation_method": "single_active_claim",
            "claim_id": self.claim_id, "observation_ids": list(self.observation_ids),
            "status": self.status, "complete": self.complete and bool(self.observation_ids),
            "duration_s": duration,
            "coverage": coverage, "covered_duration_s": self.covered_s,
            "missing_duration_s": max(0, duration - self.covered_s),
            "coverage_tolerance_s": getattr(self, "coverage_tolerance_s", 0),
            "coverage_discrepancy_s": getattr(self, "coverage_discrepancy_s", 0),
            "unallocated_pool_wh": float(self.unallocated_wh),
            "allocation_note": "Unallocated pools may appear in several claim diagnostics; never sum them across claims.",
        }


_POOLS = weakref.WeakKeyDictionary()


def collected_meter(factory, *, key):
    """Factory called inside the running server loop; settings identify a pool."""
    loop = asyncio.get_running_loop()
    pools = _POOLS.setdefault(loop, {})
    pool = pools.setdefault(key, CollectorPool(factory))
    return CollectedMeter(pool, factory().describe())
