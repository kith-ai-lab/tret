"""Measured energy: an optional meter sampled around a local model segment.

`tret/services/emissions.py::energy_accounting` has accepted a
`measured_energy_wh` figure for a while — it replaces the per-token estimate
with an operator-supplied IT-load reading — but nothing produced one for a
self-hosted run until this module. Two shapes cover the deployment reality:

* **`NvidiaSmiMeter`** — samples `nvidia-smi --query-gpu=power.draw` on an
  interval and integrates watts x seconds into Wh. Only ever available on an
  NVIDIA host: bare metal with the driver installed, or a container with the
  NVIDIA Container Toolkit runtime (Ollama-in-Docker without it cannot see
  the GPU at all, and `nvidia-smi` simply is not there). It always reports
  `shared_device=True` — this is GPU-board power, not a per-process figure,
  so on a box running anything else besides the one model server it
  OVERSTATES this run's actual share.
* **`ExternalReadingMeter`** — wraps a caller-supplied Wh figure from
  metering tret has no way to reach itself: a Mac's `powermetrics` (needs
  sudo, so it is not something a server process can shell out to on the
  operator's behalf), a smart PDU, a cluster's own accounting. This is the
  same value a caller can instead hand straight to `Router.run`/`arun` or
  `tret run --measured-wh` — see their own docstrings — this class exists so
  "an external reading" is a first-class `EnergyMeter` too, not a special
  case bypassing the interface.

`meter_for_settings` is the only thing `engine/harness.py` calls: it reads
`TRET_LOCAL_ENERGY_METER` and returns whichever of the above (today, only
`NvidiaSmiMeter`) the operator configured, or `None` for the default
(unmetered, today's estimate-only behaviour). `ExternalReadingMeter` is built
directly by a caller that already has a number in hand, never by this
function — there is no setting that would supply one.

Every meter here is best-effort by construction: a missing binary, a process
that will not start, a parse failure on one sample, a `stop()` that raises —
none of it may fail or meaningfully delay the run it is measuring. The
caller (`engine/harness.py`) falls back to the ordinary per-token estimate
whenever `stop()` returns `None`, exactly as if metering had never been
turned on.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

log = logging.getLogger("tret.energy_meter")

__all__ = [
    "EnergyMeter",
    "MeterReading",
    "NvidiaSmiMeter",
    "NvmlCounterDelta",
    "NvmlCounterSnapshot",
    "NvmlTotalEnergyAdapter",
    "NvmlEnergyMeter",
    "ExternalReadingMeter",
    "meter_for_settings",
    "MIN_INTERVAL_S",
]

# The floor `meter_for_settings` clamps TRET_LOCAL_ENERGY_METER_INTERVAL_S to.
# Below this, `nvidia-smi`'s own ~10-20ms invocation overhead starts to matter
# relative to the sleep between samples, and a busy box gains nothing a 0.2s
# interval doesn't already give it in the direction that matters (LLM turns
# run for seconds to minutes, not milliseconds).
MIN_INTERVAL_S = 0.2

# A periodic sampler can be delayed by normal scheduling jitter. Once the
# distance between readings exceeds this many configured intervals, however,
# interpolating across the hole would turn an unobserved span into apparently
# complete measured energy. Such spans are retained as diagnostics but are
# not returned to the current accounting caller as usable measurements.
MAX_SAMPLE_GAP_MULTIPLIER = 2.5

ENERGY_BOUNDARIES = frozenset({"gpu", "node_it", "facility", "partial", "unknown"})


def _nonnegative_decimal(value: object, *, field_name: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result


def _nonnegative_finite(value: object, *, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} must be a finite non-negative number") from exc
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field_name} must be a finite non-negative number")
    return result

DEFAULT_NVIDIA_SMI_COMMAND: tuple[str, ...] = (
    "nvidia-smi",
    "--query-gpu=index,power.draw",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True)
class MeterReading:
    """What one meter produced for one `ModelSegment`'s lifetime.

    `energy_boundary` states what the Wh includes. The caller applies PUE only
    where that boundary permits it; facility, partial and unknown readings are
    never silently expanded. `shared_device=True` says the figure could not be attributed to
    this run alone (host-level power on a box that may be running other
    things) — `engine/harness.py` turns that into the
    `shared_device_measurement` caveat, direction "overstates".
    """

    wh: Decimal
    samples: int
    duration_s: float
    kind: str
    note: str | None
    shared_device: bool
    energy_boundary: str = "node_it"
    complete: bool = True
    coverage: float = 1.0
    covered_duration_s: float | None = None
    missing_duration_s: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "wh", _nonnegative_decimal(self.wh, field_name="wh"))
        duration = _nonnegative_finite(self.duration_s, field_name="duration_s")
        object.__setattr__(self, "duration_s", duration)
        if not isinstance(self.samples, int) or isinstance(self.samples, bool) or self.samples < 0:
            raise ValueError("samples must be a non-negative integer")
        if self.energy_boundary not in ENERGY_BOUNDARIES:
            raise ValueError(f"unsupported energy boundary: {self.energy_boundary!r}")
        coverage = _nonnegative_finite(self.coverage, field_name="coverage")
        if coverage > 1.0:
            raise ValueError("coverage must be between 0 and 1")
        object.__setattr__(self, "coverage", coverage)
        missing = _nonnegative_finite(
            self.missing_duration_s, field_name="missing_duration_s"
        )
        object.__setattr__(self, "missing_duration_s", missing)
        covered = duration * coverage if self.covered_duration_s is None else _nonnegative_finite(
            self.covered_duration_s, field_name="covered_duration_s"
        )
        if covered > duration + 1e-9 or missing > duration + 1e-9:
            raise ValueError("coverage durations cannot exceed duration_s")
        if abs(covered - duration * coverage) > 1e-9 or abs(covered + missing - duration) > 1e-9:
            raise ValueError("coverage durations must reconcile to duration_s and coverage")
        object.__setattr__(self, "covered_duration_s", covered)
        if self.complete and (coverage < 1.0 - 1e-9 or missing > 1e-9):
            raise ValueError("a complete reading must cover its full duration")

    def describe(self) -> dict:
        """Serializable measurement quality metadata for accounting callers."""
        return {
            "complete": self.complete,
            "coverage": self.coverage,
            "covered_duration_s": self.covered_duration_s,
            "missing_duration_s": self.missing_duration_s,
            "status": "complete" if self.complete else "incomplete",
        }


class EnergyMeter(Protocol):
    """One measurement span: `start()` when a local model segment begins,
    `stop()` when it ends (a model switch, or the run itself finishing).
    `describe()` is static — it never changes once constructed — and is used
    to render the `energy_meter` accounting block's `kind`/`interval_s`
    fields even when `stop()` had to return `None`."""

    async def start(self) -> None: ...

    async def stop(self) -> MeterReading | None: ...

    def describe(self) -> dict: ...


# ── nvidia-smi ────────────────────────────────────────────────────────────────
async def _run_command(command: tuple[str, ...], *, timeout_s: float = 2.0) -> str:
    """Run `command`, return its stdout as text.

    Raises `FileNotFoundError` when the binary itself does not exist (the
    common case: no NVIDIA driver, or Ollama-in-Docker without the NVIDIA
    Container Toolkit runtime), `OSError` for anything else that stops the
    process from starting, and `asyncio.TimeoutError` when it started but
    never returned within `timeout_s` — a wedged `nvidia-smi` must not be
    allowed to pile up subprocesses or stall the sampling loop indefinitely.
    On that timeout (and on this call itself being cancelled, e.g. the
    segment stopping mid-sample) the process is killed and reaped before the
    exception propagates, so nothing is left running behind the caller's
    back. Extracted to its own module-level function — never called any
    other way from `NvidiaSmiMeter` — purely so a test can monkeypatch
    `tret.services.energy_meter._run_command` and script a deterministic
    sequence of readings without a real `nvidia-smi` anywhere on the test
    machine.
    """
    proc = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise
    return stdout.decode("utf-8", errors="replace")


def _parse_power_draw(text: str, gpu_index: int | None) -> float | None:
    """Sum `power.draw` across every reported GPU (or just `gpu_index`, if
    given) from one `nvidia-smi --query-gpu=index,power.draw` invocation.

    Returns `None` when nothing in the output could be parsed as a reading —
    a blank line, a header the `noheader` flag failed to suppress, a
    "[Not Supported]" cell some drivers emit for power draw on certain cards
    — so the caller can skip the sample rather than integrate a bogus zero.
    A negative reading (a driver quirk on some cards) is skipped the same
    way: it is not a real power draw, and folding it in would subtract
    energy from the run rather than simply contributing nothing. A run must
    never fail, or record negative energy, over one bad reading.
    """
    total = 0.0
    found = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            idx = int(parts[0])
            watts = float(parts[1])
        except ValueError:
            continue  # a "[Not Supported]" cell, a stray header, etc.
        if gpu_index is not None and idx != gpu_index:
            continue
        if not math.isfinite(watts) or watts < 0:
            continue  # a bad reading; not a real (zero-power) sample either
        total += watts
        found = True
    return total if found else None


@dataclass
class NvidiaSmiMeter:
    """Samples `nvidia-smi --query-gpu=power.draw` every `interval_s` and
    integrates watts x seconds (trapezoid rule) into Wh across the run.

    `gpu_index=None` (the default) sums every GPU `nvidia-smi` reports — the
    right default when a single local model server is the only thing on the
    box; set it to isolate one accelerator on a shared multi-GPU host.

    Always `shared_device=True`: this is whatever the selected GPU board(s)
    drew for the sampled interval, not a per-process or whole-host figure — `nvidia-smi`
    has no notion of "this one inference request's share" — so it overstates
    this run's own draw on any box running something else concurrently.
    """

    interval_s: float = 1.0
    gpu_index: int | None = None
    command: tuple[str, ...] = DEFAULT_NVIDIA_SMI_COMMAND
    _task: asyncio.Task | None = field(default=None, init=False, repr=False, compare=False)
    _samples: list[tuple[float, float]] = field(default_factory=list, init=False, repr=False)
    _start_ts: float | None = field(default=None, init=False, repr=False)
    _missing_binary: bool = field(default=False, init=False, repr=False)
    _warned: bool = field(default=False, init=False, repr=False)
    _last_reading: MeterReading | None = field(default=None, init=False, repr=False)

    def describe(self) -> dict:
        quality = (
            self._last_reading.describe()
            if self._last_reading is not None
            else {
                "complete": None,
                "coverage": None,
                "covered_duration_s": None,
                "missing_duration_s": None,
                "status": "not_finished",
            }
        )
        return {
            "kind": "nvidia_smi",
            "energy_boundary": "gpu",
            "interval_s": self.interval_s,
            "notes": (
                "GPU-board nvidia-smi power.draw, sampled every "
                f"{self.interval_s}s and integrated into Wh. Shared-device: "
                "on a box running anything besides this one model server, "
                "this OVERSTATES the run's own share. Ollama-in-Docker needs "
                "the NVIDIA Container Toolkit runtime for nvidia-smi to see "
                "the GPU at all; unsupported on macOS — use an external "
                "reading there instead."
            ),
            **quality,
        }

    async def _sample(self) -> float | None:
        # Bounded the same way `_stop_meter` bounds the whole meter (floor 2s,
        # twice the sampling interval): a wedged `nvidia-smi` must lose at
        # most one sample, never hang the loop that is supposed to fork a new
        # one every `interval_s`.
        timeout_s = max(2.0, self.interval_s * 2)
        try:
            text = await _run_command(self.command, timeout_s=timeout_s)
        except FileNotFoundError:
            if not self._warned:
                log.warning(
                    "TRET_LOCAL_ENERGY_METER=nvidia_smi but %r was not found; this run's "
                    "local energy will fall back to the per-token estimate",
                    self.command[0],
                )
                self._warned = True
            self._missing_binary = True
            return None
        except TimeoutError:
            log.warning(
                "nvidia-smi sample did not return within %.1fs; skipping this sample",
                timeout_s,
            )
            return None
        except OSError:
            log.warning("nvidia-smi sample failed to run; skipping this sample", exc_info=True)
            return None
        return _parse_power_draw(text, self.gpu_index)

    async def start(self) -> None:
        self._samples = []
        self._missing_binary = False
        self._last_reading = None
        # One synchronous probe before scheduling the background task: this is
        # what lets a missing binary be discovered (and logged) right here in
        # `start()`, rather than silently inside a task nobody is awaiting.
        watts = await self._sample()
        if self._missing_binary:
            self._task = None
            return
        # The claim begins after setup/initial probing.  Treat the initial
        # sample as an endpoint estimate rather than charging probe latency to
        # the workload interval.
        self._start_ts = time.monotonic()
        if watts is not None:
            self._samples.append((self._start_ts, watts))
        self._task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval_s)
            except asyncio.CancelledError:
                return
            watts = await self._sample()
            if watts is not None:
                self._samples.append((time.monotonic(), watts))

    async def stop(self) -> MeterReading | None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - a dying sampler must never fail the run
                log.warning("nvidia-smi sampling task raised while stopping", exc_info=True)
        if self._missing_binary:
            return None
        stop_ts = time.monotonic()
        start_ts = self._start_ts if self._start_ts is not None else stop_ts
        duration = max(0.0, stop_ts - start_ts)
        samples = list(self._samples)
        short_probe = len(samples) < 2
        used_final_probe = False
        if len(samples) < 2:
            # Too short a segment for the loop to have taken a second sample —
            # take one more now and price the whole span at that single
            # reading, rather than report zero for a run that plainly drew
            # power the entire time it ran.
            watts = await self._sample()
            if watts is not None:
                # The probe completes after the endpoint, so associate its
                # value with the endpoint itself. It estimates the endpoint
                # power without extending the measured workload duration.
                samples.append((stop_ts, watts))
                used_final_probe = True
        # Reject corrupt samples even if they entered through an injected
        # sampler or white-box integration path rather than `_parse_power_draw`.
        samples = [
            (sample_t, watts)
            for sample_t, watts in samples
            if math.isfinite(sample_t)
            and math.isfinite(watts)
            and watts >= 0
            and start_ts <= sample_t <= stop_ts
        ]
        samples.sort(key=lambda sample: sample[0])
        if not samples:
            return None

        max_gap_s = self.interval_s * MAX_SAMPLE_GAP_MULTIPLIER
        wh = 0.0
        covered_duration = 0.0

        def _cover_flat(left: float, right: float, watts: float) -> None:
            nonlocal covered_duration, wh
            span = max(0.0, right - left)
            if span <= max_gap_s:
                covered_duration += span
                wh += watts * span / 3600.0

        first_t, first_w = samples[0]
        last_t, last_w = samples[-1]
        _cover_flat(start_ts, first_t, first_w)
        for (t0, w0), (t1, w1) in zip(samples, samples[1:]):
            span = max(0.0, t1 - t0)
            if span <= max_gap_s:
                covered_duration += span
                wh += (w0 + w1) / 2.0 * span / 3600.0
        _cover_flat(last_t, stop_ts, last_w)

        covered_duration = min(duration, covered_duration)
        missing_duration = max(0.0, duration - covered_duration)
        coverage = 1.0 if duration == 0 else min(1.0, covered_duration / duration)
        complete = missing_duration <= 1e-9
        note = None
        if short_probe:
            note = (
                "fewer than 2 periodic samples were taken (a short segment); "
                "the most recent power reading was applied across the whole "
                "elapsed duration instead of integrated over time"
            )
        if used_final_probe:
            note = (note + "; " if note else "") + (
                "final power probe completed after the workload endpoint and "
                "was associated with that endpoint as an estimate"
            )
        if not complete:
            note = (
                f"sampling coverage was incomplete ({coverage:.3f}); one or more gaps "
                f"exceeded {max_gap_s:g}s and were not interpolated"
            )

        reading = MeterReading(
            wh=Decimal(str(wh)),
            samples=len(samples),
            duration_s=duration,
            kind="nvidia_smi",
            note=note,
            shared_device=True,
            energy_boundary="gpu",
            complete=complete,
            coverage=coverage,
            covered_duration_s=covered_duration,
            missing_duration_s=missing_duration,
        )
        self._last_reading = reading
        if not complete:
            log.warning(
                "nvidia-smi coverage was incomplete (%.1f%%, %.3fs missing); "
                "discarding the partial measurement and using the estimator",
                coverage * 100,
                missing_duration,
            )
            return None
        return reading


# ── optional NVML cumulative-energy counter ─────────────────────────────────
@dataclass(frozen=True)
class NvmlCounterSnapshot:
    """One validated NVML total-energy counter observation.

    NVML reports this counter in millijoules since driver reload. Callers must
    compare snapshots from the same physical device and reject a decreasing
    counter; `NvmlTotalEnergyAdapter.delta` does both checks.
    """

    device_id: str
    total_mj: int
    monotonic_s: float

    def __post_init__(self) -> None:
        if not self.device_id:
            raise ValueError("device_id must not be empty")
        if (
            not isinstance(self.total_mj, int)
            or isinstance(self.total_mj, bool)
            or self.total_mj < 0
        ):
            raise ValueError("total_mj must be a non-negative integer")
        object.__setattr__(
            self,
            "monotonic_s",
            _nonnegative_finite(self.monotonic_s, field_name="monotonic_s"),
        )


@dataclass(frozen=True)
class NvmlCounterDelta:
    """Result of comparing two NVML counter snapshots."""

    status: str
    energy_wh: Decimal | None = None
    duration_s: float | None = None
    detail: str | None = None

    @property
    def complete(self) -> bool:
        return self.status == "complete"

    def describe(self) -> dict:
        return {
            "status": self.status,
            "complete": self.complete,
            "coverage": 1.0 if self.complete else 0.0,
            "duration_s": self.duration_s,
            "detail": self.detail,
        }


class NvmlTotalEnergyAdapter:
    """Lazy, optional access to NVML's cumulative GPU energy counter.

    This adapter intentionally is not wired into settings yet. It imports
    ``pynvml`` only on the first snapshot, so installations without NVIDIA's
    optional Python binding retain the current behavior and dependencies.
    Hardware/API errors become an ``unavailable`` status rather than raising.
    """

    def __init__(self, gpu_index: int = 0, *, backend: Any | None = None) -> None:
        self.gpu_index = gpu_index
        self._backend = backend
        self._initialized = False
        self.status = "not_started"
        self.detail: str | None = None

    def _get_backend(self) -> Any:
        if self._backend is None:
            self._backend = importlib.import_module("pynvml")
        if not self._initialized:
            self._backend.nvmlInit()
            self._initialized = True
        return self._backend

    def snapshot(self) -> NvmlCounterSnapshot | None:
        """Return one counter snapshot, or ``None`` with status unavailable."""
        try:
            backend = self._get_backend()
            handle = backend.nvmlDeviceGetHandleByIndex(self.gpu_index)
            device_id = backend.nvmlDeviceGetUUID(handle)
            if isinstance(device_id, bytes):
                device_id = device_id.decode("utf-8", errors="replace")
            total_mj = backend.nvmlDeviceGetTotalEnergyConsumption(handle)
            # Timestamp after the hardware query.  Slow initialisation/query
            # work is outside the interval; a slow final query therefore
            # remains visible in the delta and is rejected by the collector's
            # symmetric claim/coverage check.
            observation_ts = time.monotonic()
            snapshot = NvmlCounterSnapshot(
                device_id=str(device_id),
                total_mj=total_mj,
                monotonic_s=observation_ts,
            )
        except Exception as exc:  # noqa: BLE001 - optional hardware must be best effort
            self.status = "unavailable"
            self.detail = f"{type(exc).__name__}: {exc}"
            return None
        self.status = "available"
        self.detail = None
        return snapshot

    @staticmethod
    def delta(
        start: NvmlCounterSnapshot | None,
        end: NvmlCounterSnapshot | None,
    ) -> NvmlCounterDelta:
        """Validate and convert a counter difference from mJ to Wh."""
        if start is None or end is None:
            return NvmlCounterDelta(
                status="unavailable", detail="both counter snapshots are required"
            )
        duration = end.monotonic_s - start.monotonic_s
        if duration < 0:
            return NvmlCounterDelta(
                status="invalid",
                duration_s=duration,
                detail="end snapshot precedes start snapshot",
            )
        if start.device_id != end.device_id:
            return NvmlCounterDelta(
                status="device_changed",
                duration_s=duration,
                detail="NVML device identity changed between snapshots",
            )
        if end.total_mj < start.total_mj:
            return NvmlCounterDelta(
                status="counter_reset",
                duration_s=duration,
                detail="NVML total-energy counter decreased",
            )
        energy_wh = Decimal(end.total_mj - start.total_mj) / Decimal("3600000")
        return NvmlCounterDelta(
            status="complete", energy_wh=energy_wh, duration_s=duration
        )

    def close(self) -> None:
        """Release this adapter's optional NVML session when one was opened."""
        if self._initialized and self._backend is not None:
            try:
                self._backend.nvmlShutdown()
            except Exception:  # noqa: BLE001 - teardown is best effort too
                log.debug("NVML shutdown failed", exc_info=True)
        self._initialized = False


class NvmlEnergyMeter:
    """One explicitly selected GPU counter, with sampled-power fallback.

    Optional ``pynvml`` binding; no package import at module load. A counter
    reset during a run is incomplete, not a reason to substitute a zero or a
    sample taken after the interval. A missing initial counter may fall back
    to the existing sampled-power method, with that choice recorded.
    """

    def __init__(self, gpu_index: int = 0, interval_s: float = 1.0, *, adapter=None):
        self.gpu_index, self.interval_s = gpu_index, interval_s
        self.adapter = adapter or NvmlTotalEnergyAdapter(gpu_index)
        self.initial = None
        self.fallback = None
        self.status = "not_started"
        self.delta_record = {}
        self.fallback_reason = None

    async def _close_adapter(self) -> None:
        """Close NVML away from the event loop, with a best-effort bound."""
        try:
            await asyncio.wait_for(asyncio.to_thread(self.adapter.close), timeout=2.0)
        except BaseException:
            log.debug("NVML adapter shutdown failed or timed out", exc_info=True)

    async def start(self):
        self.initial = await asyncio.to_thread(self.adapter.snapshot)
        if self.initial is None:
            self.fallback_reason = self.adapter.detail or self.adapter.status
            await self._close_adapter()
            self.fallback = NvidiaSmiMeter(interval_s=self.interval_s, gpu_index=self.gpu_index)
            await self.fallback.start()
            self.status = "sampled_power_fallback"
        else:
            self.status = "observing"

    async def stop(self):
        if self.fallback is not None:
            try:
                return await self.fallback.stop()
            finally:
                self.delta_record = self.fallback.describe()
        try:
            end = await asyncio.to_thread(self.adapter.snapshot)
            delta = self.adapter.delta(self.initial, end)
            self.status = delta.status
            self.delta_record = delta.describe()
            if not delta.complete:
                return None
            return MeterReading(delta.energy_wh, 2, delta.duration_s, "nvml_total_energy",
                                "Selected GPU cumulative energy counter; excludes other devices and host components.",
                                True, "gpu")
        finally:
            await self._close_adapter()

    def describe(self):
        return {"kind": "nvml_total_energy", "gpu_index": self.gpu_index,
                "device_ids": [self.initial.device_id] if self.initial else [],
                "energy_boundary": "gpu", "interval_s": self.interval_s,
                "status": self.status, **self.delta_record,
                "fallback_reason": self.fallback_reason,
                "counter_unit": "mJ", "instrument_validation": "not_supplied"}


# ── external reading ─────────────────────────────────────────────────────────
@dataclass
class ExternalReadingMeter:
    """Wraps a caller-supplied Wh figure as an `EnergyMeter`.

    `start()`/`stop()` do no sampling at all — the number was already metered
    by something outside tret (a Mac's `powermetrics`, a smart PDU, a
    cluster's own accounting) before this object existed. `shared_device` is
    left to the caller: an operator who read a whole-machine meter for a
    box also running other work may set it `True` to carry the same
    `shared_device_measurement` caveat a shared `nvidia-smi` reading does.
    """

    wh: Decimal | float
    shared_device: bool = False
    note: str | None = None
    energy_boundary: str = "node_it"

    def __post_init__(self) -> None:
        self.wh = _nonnegative_decimal(self.wh, field_name="wh")
        if self.energy_boundary not in ENERGY_BOUNDARIES:
            raise ValueError(f"unsupported energy boundary: {self.energy_boundary!r}")

    def describe(self) -> dict:
        return {
            "kind": "external",
            "energy_boundary": self.energy_boundary,
            "interval_s": None,
            "notes": "An operator-supplied reading from metering outside tret.",
            "complete": True,
            "coverage": 1.0,
            "covered_duration_s": 0.0,
            "missing_duration_s": 0.0,
            "status": "complete",
        }

    async def start(self) -> None:
        return None

    async def stop(self) -> MeterReading:
        return MeterReading(
            wh=self.wh,
            samples=1,
            duration_s=0.0,
            kind="external",
            note=self.note,
            shared_device=self.shared_device,
            energy_boundary=self.energy_boundary,
        )


# ── wiring from Settings ──────────────────────────────────────────────────────
def meter_for_settings(settings) -> "EnergyMeter | None":
    """The meter `engine/harness.py` should run for a local model segment
    under this deployment's configuration, or `None` for the default
    (unmetered — today's per-token estimate, unchanged).

    `TRET_LOCAL_ENERGY_METER`: `off` (default) | `nvidia_smi` | `nvml`.
    `ExternalReadingMeter` is deliberately not reachable from here — nothing
    in `Settings` could supply the Wh figure it wraps; a caller that has one
    already passes it straight to `Router.run`/`arun` or `tret run
    --measured-wh` instead.
    """
    kind = (getattr(settings, "local_energy_meter", "off") or "off").strip().lower()
    if kind in ("", "off"):
        return None
    if kind in {"nvidia_smi", "nvml"}:
        interval = float(getattr(settings, "local_energy_meter_interval_s", 1.0) or 1.0)
        interval = max(MIN_INTERVAL_S, interval)
        gpu_index = getattr(settings, "local_energy_meter_gpu_index", None)
        if kind == "nvml":
            return NvmlEnergyMeter(gpu_index=0 if gpu_index is None else gpu_index, interval_s=interval)
        return NvidiaSmiMeter(interval_s=interval, gpu_index=gpu_index)
    log.warning(
        "TRET_LOCAL_ENERGY_METER=%r is not recognized (off | nvidia_smi | nvml); "
        "local energy metering is disabled for this deployment",
        kind,
    )
    return None
