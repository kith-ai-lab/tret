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
  `shared_device=True` — this is host-level power, not a per-process figure,
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
import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

log = logging.getLogger("tret.energy_meter")

__all__ = [
    "EnergyMeter",
    "MeterReading",
    "NvidiaSmiMeter",
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

DEFAULT_NVIDIA_SMI_COMMAND: tuple[str, ...] = (
    "nvidia-smi",
    "--query-gpu=index,power.draw",
    "--format=csv,noheader,nounits",
)


@dataclass(frozen=True)
class MeterReading:
    """What one meter produced for one `ModelSegment`'s lifetime.

    `wh` is IT-load only — the same scope `energy_accounting`'s own
    `measured_energy_wh` parameter has always meant — never PUE- or
    grid-adjusted; the caller applies those on top exactly as it would to an
    estimate. `shared_device=True` says the figure could not be attributed to
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
        if watts < 0:
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

    Always `shared_device=True`: this is whatever the whole card (or host)
    drew for the sampled interval, not a per-process figure — `nvidia-smi`
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

    def describe(self) -> dict:
        return {
            "kind": "nvidia_smi",
            "interval_s": self.interval_s,
            "notes": (
                "Host-level nvidia-smi power.draw, sampled every "
                f"{self.interval_s}s and integrated into Wh. Shared-device: "
                "on a box running anything besides this one model server, "
                "this OVERSTATES the run's own share. Ollama-in-Docker needs "
                "the NVIDIA Container Toolkit runtime for nvidia-smi to see "
                "the GPU at all; unsupported on macOS — use an external "
                "reading there instead."
            ),
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
        self._start_ts = time.monotonic()
        # One synchronous probe before scheduling the background task: this is
        # what lets a missing binary be discovered (and logged) right here in
        # `start()`, rather than silently inside a task nobody is awaiting.
        watts = await self._sample()
        if self._missing_binary:
            self._task = None
            return
        if watts is not None:
            self._samples.append((time.monotonic(), watts))
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
        if len(samples) < 2:
            # Too short a segment for the loop to have taken a second sample —
            # take one more now and price the whole span at that single
            # reading, rather than report zero for a run that plainly drew
            # power the entire time it ran.
            watts = await self._sample()
            if watts is not None:
                samples.append((time.monotonic(), watts))
            if not samples:
                return None
            wh = max(0.0, samples[-1][1] * duration / 3600.0)
            return MeterReading(
                wh=Decimal(str(wh)),
                samples=len(samples),
                duration_s=duration,
                kind="nvidia_smi",
                note=(
                    "fewer than 2 periodic samples were taken (a short segment); "
                    "the most recent power reading was applied across the whole "
                    "elapsed duration instead of integrated over time"
                ),
                shared_device=True,
            )
        # Trapezoid rule between consecutive samples, with the span before the
        # first sample and after the last extended flat at that sample's own
        # watts — rather than left uncounted — so the integrated span always
        # agrees with `duration_s` (start-to-stop), not just first-sample-to-
        # last-sample. A segment stopped 0.9s after its last periodic sample,
        # say, still drew power for that 0.9s; the last reading is the best
        # estimate available for it.
        first_t, first_w = samples[0]
        last_t, last_w = samples[-1]
        wh = first_w * max(0.0, first_t - start_ts) / 3600.0
        for (t0, w0), (t1, w1) in zip(samples, samples[1:]):
            wh += (w0 + w1) / 2.0 * (t1 - t0) / 3600.0
        wh += last_w * max(0.0, stop_ts - last_t) / 3600.0
        wh = max(0.0, wh)
        return MeterReading(
            wh=Decimal(str(wh)),
            samples=len(samples),
            duration_s=duration,
            kind="nvidia_smi",
            note=None,
            shared_device=True,
        )


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

    def describe(self) -> dict:
        return {
            "kind": "external",
            "interval_s": None,
            "notes": "An operator-supplied reading from metering outside tret.",
        }

    async def start(self) -> None:
        return None

    async def stop(self) -> MeterReading:
        return MeterReading(
            wh=Decimal(str(self.wh)),
            samples=1,
            duration_s=0.0,
            kind="external",
            note=self.note,
            shared_device=self.shared_device,
        )


# ── wiring from Settings ──────────────────────────────────────────────────────
def meter_for_settings(settings) -> "EnergyMeter | None":
    """The meter `engine/harness.py` should run for a local model segment
    under this deployment's configuration, or `None` for the default
    (unmetered — today's per-token estimate, unchanged).

    `TRET_LOCAL_ENERGY_METER`: `off` (default) | `nvidia_smi`.
    `ExternalReadingMeter` is deliberately not reachable from here — nothing
    in `Settings` could supply the Wh figure it wraps; a caller that has one
    already passes it straight to `Router.run`/`arun` or `tret run
    --measured-wh` instead.
    """
    kind = (getattr(settings, "local_energy_meter", "off") or "off").strip().lower()
    if kind in ("", "off"):
        return None
    if kind == "nvidia_smi":
        interval = float(getattr(settings, "local_energy_meter_interval_s", 1.0) or 1.0)
        interval = max(MIN_INTERVAL_S, interval)
        gpu_index = getattr(settings, "local_energy_meter_gpu_index", None)
        return NvidiaSmiMeter(interval_s=interval, gpu_index=gpu_index)
    log.warning(
        "TRET_LOCAL_ENERGY_METER=%r is not recognized (off | nvidia_smi); "
        "local energy metering is disabled for this deployment",
        kind,
    )
    return None
