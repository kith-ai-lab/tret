"""`tret/services/energy_meter.py`: the meters that produce a run's own
`measured_energy_wh` instead of the per-token estimate.

No real `nvidia-smi` anywhere here — `NvidiaSmiMeter` talks to the outside
world through exactly one seam, the module-level `_run_command`, and every
test that needs one monkeypatches that seam with a canned async function
instead. Where a test needs a deterministic elapsed duration too (the
integration test, the short-run test), the module's `time` name is replaced
with a tiny fake clock — scoped to `tret.services.energy_meter` only, never
the real `time` module, so nothing about asyncio's own scheduling is
disturbed.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

import tret.services.energy_meter as energy_meter
from tret.config import Settings
from tret.services.energy_meter import (
    MIN_INTERVAL_S,
    ExternalReadingMeter,
    MeterReading,
    NvidiaSmiMeter,
    meter_for_settings,
)


class _FakeClock:
    """Stands in for the `time` module inside `energy_meter` — only
    `monotonic()` is ever called on it."""

    def __init__(self, value: float = 0.0) -> None:
        self.value = value

    def monotonic(self) -> float:
        return self.value


def _runner(*outputs):
    """An async `_run_command` stand-in: returns each of `outputs` in order
    (a string as stdout text, or the `FileNotFoundError` class to simulate a
    missing binary), repeating the last entry for any call beyond the script.
    """
    calls: list[int] = [0]

    async def _run(command: tuple[str, ...], *, timeout_s: float = 2.0) -> str:
        idx = min(calls[0], len(outputs) - 1)
        calls[0] += 1
        out = outputs[idx]
        if isinstance(out, type) and issubclass(out, BaseException):
            raise out("nvidia-smi not found")
        return out

    _run.calls = calls
    return _run


# ── parsing ───────────────────────────────────────────────────────────────────


def test_parse_power_draw_sums_every_gpu_by_default():
    text = "0, 120.50\n1, 95.25\n"
    assert energy_meter._parse_power_draw(text, gpu_index=None) == pytest.approx(215.75)


def test_parse_power_draw_can_isolate_one_gpu():
    text = "0, 120.50\n1, 95.25\n"
    assert energy_meter._parse_power_draw(text, gpu_index=1) == pytest.approx(95.25)


def test_parse_power_draw_skips_unparseable_lines():
    # A stray header the `noheader` flag failed to suppress, and a driver's
    # "[Not Supported]" cell for power draw — neither is a number.
    text = "index, power.draw [W]\n0, [Not Supported]\n1, 88.0\n"
    assert energy_meter._parse_power_draw(text, gpu_index=None) == pytest.approx(88.0)


def test_parse_power_draw_returns_none_when_nothing_parsed():
    assert energy_meter._parse_power_draw("", gpu_index=None) is None
    assert energy_meter._parse_power_draw("garbage\n", gpu_index=None) is None


def test_parse_power_draw_skips_a_negative_reading_on_one_gpu():
    # A driver quirk on one card must not subtract from the other GPUs' real
    # draw, and must not be counted as a real (zero-power) sample either.
    text = "0, -5.0\n1, 88.0\n"
    assert energy_meter._parse_power_draw(text, gpu_index=None) == pytest.approx(88.0)


def test_parse_power_draw_returns_none_when_every_reading_is_negative():
    assert energy_meter._parse_power_draw("0, -5.0\n1, -1.0\n", gpu_index=None) is None


# ── NvidiaSmiMeter: integration of a known power series ─────────────────────


async def test_integration_of_a_known_power_series_gives_expected_wh(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0)
    # A known series: 100W, 100W, 300W at t=0, 1, 2 seconds, stopped 0.9s
    # after the last sample (t=2.9) — the segment kept drawing power for
    # that 0.9s too, so it must be integrated, not left uncounted just
    # because no periodic sample happened to land there. Trapezoid between
    # samples: (100+100)/2*1 + (100+300)/2*1 = 100 + 200 = 300 watt-seconds.
    # Tail, extended flat at the last (300W) reading: 300 * 0.9 = 270
    # watt-seconds. No head extension: the first sample coincides with
    # `_start_ts`. Total 570 watt-seconds, i.e. 570 / 3600 Wh — and
    # `duration_s` (start to stop) agrees with the full integrated span.
    meter._samples = [(0.0, 100.0), (1.0, 100.0), (2.0, 300.0)]
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "time", _FakeClock(2.9))

    reading = await meter.stop()

    assert reading is not None
    assert reading.kind == "nvidia_smi"
    assert reading.samples == 3
    assert reading.duration_s == pytest.approx(2.9)
    assert reading.shared_device is True
    assert reading.note is None
    assert reading.wh == pytest.approx(Decimal("570") / Decimal("3600"))


async def test_integration_extends_flat_before_the_first_sample_too(monkeypatch):
    """The same tail treatment applies at the head: a segment that started
    before its first periodic sample landed still drew power for that
    span, priced at the first sample's own reading."""
    meter = NvidiaSmiMeter(interval_s=1.0)
    meter._samples = [(0.5, 100.0), (1.5, 100.0)]
    meter._start_ts = 0.0  # 0.5s before the first sample
    monkeypatch.setattr(energy_meter, "time", _FakeClock(1.5))  # stop exactly at the last sample

    reading = await meter.stop()

    assert reading is not None
    # Head: 100W * 0.5s = 50 watt-seconds. Trapezoid: 100W flat * 1s = 100
    # watt-seconds. No tail: stop coincides with the last sample.
    assert reading.duration_s == pytest.approx(1.5)
    assert reading.wh == pytest.approx(Decimal("150") / Decimal("3600"))


async def test_stop_clamps_a_negative_integration_to_zero(monkeypatch):
    """`_parse_power_draw` already keeps a negative *reading* out of
    `_samples` in the ordinary path — this is the belt-and-suspenders half:
    even if a negative wattage somehow reached the trapezoid (samples set
    directly here, the way every white-box test in this file does), the run
    must report zero energy, never negative, rather than fail or subtract
    from anything upstream."""
    meter = NvidiaSmiMeter(interval_s=1.0)
    meter._samples = [(0.0, -100.0), (1.0, -100.0)]
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "time", _FakeClock(1.0))

    reading = await meter.stop()

    assert reading is not None
    assert reading.wh == Decimal("0")


async def test_short_run_falls_back_to_one_more_sample_over_elapsed_time(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0, command=("fake",))
    meter._samples = [(0.0, 60.0)]  # only the one `start()` sample
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "_run_command", _runner("0, 60.0"))
    monkeypatch.setattr(energy_meter, "time", _FakeClock(0.5))

    reading = await meter.stop()

    assert reading is not None
    assert reading.samples == 2  # the seeded sample plus stop()'s extra one
    assert reading.duration_s == pytest.approx(0.5)
    # Priced at the (single, in this case constant) most recent watt figure
    # across the whole elapsed span, not integrated over time.
    assert reading.wh == pytest.approx(Decimal("60") * Decimal("0.5") / Decimal("3600"))
    assert "fewer than 2" in (reading.note or "")


async def test_zero_samples_and_a_still_failing_final_probe_yields_no_reading(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0, command=("fake",))
    meter._samples = []
    meter._start_ts = 0.0
    monkeypatch.setattr(energy_meter, "_run_command", _runner("garbage"))
    monkeypatch.setattr(energy_meter, "time", _FakeClock(1.0))

    reading = await meter.stop()

    assert reading is None


# ── NvidiaSmiMeter: missing binary ───────────────────────────────────────────


async def test_missing_binary_is_tolerated_start_logs_once_stop_returns_none(monkeypatch, caplog):
    meter = NvidiaSmiMeter(interval_s=0.2, command=("nvidia-smi",))
    monkeypatch.setattr(energy_meter, "_run_command", _runner(FileNotFoundError))

    with caplog.at_level("WARNING", logger="tret.energy_meter"):
        await meter.start()

    assert meter._missing_binary is True
    assert meter._task is None  # no background task was ever scheduled
    warnings = [r for r in caplog.records if "not found" in r.message]
    assert len(warnings) == 1  # logged exactly once, from start()

    reading = await meter.stop()
    assert reading is None


# ── NvidiaSmiMeter: a parse failure is skipped, not a bogus zero sample ──────


async def test_a_parse_failure_is_skipped_rather_than_recorded_as_zero(monkeypatch):
    meter = NvidiaSmiMeter(interval_s=1.0, command=("fake",))
    monkeypatch.setattr(energy_meter, "_run_command", _runner("not,a,number"))

    watts = await meter._sample()

    assert watts is None
    assert meter._missing_binary is False  # a parse failure is not a missing binary


async def test_a_generic_os_error_sampling_is_also_skipped(monkeypatch):
    async def _raise(command, *, timeout_s=2.0):
        raise OSError("device busy")

    meter = NvidiaSmiMeter(interval_s=1.0, command=("fake",))
    monkeypatch.setattr(energy_meter, "_run_command", _raise)

    watts = await meter._sample()

    assert watts is None
    assert meter._missing_binary is False


# ── NvidiaSmiMeter: a wedged nvidia-smi must lose at most one sample ────────


async def test_sample_is_skipped_and_logged_when_run_command_times_out(monkeypatch, caplog):
    async def _hang(command, *, timeout_s=2.0):
        raise TimeoutError("nvidia-smi did not return in time")

    meter = NvidiaSmiMeter(interval_s=1.0, command=("fake",))
    monkeypatch.setattr(energy_meter, "_run_command", _hang)

    with caplog.at_level("WARNING", logger="tret.energy_meter"):
        watts = await meter._sample()

    assert watts is None
    assert meter._missing_binary is False  # a timeout is not a missing binary
    assert any("did not return" in r.message for r in caplog.records)


async def test_sample_passes_a_timeout_floored_at_2s_twice_the_interval(monkeypatch):
    seen: list[float] = []

    async def _spy(command, *, timeout_s=2.0):
        seen.append(timeout_s)
        return "0, 50.0"

    meter = NvidiaSmiMeter(interval_s=0.2, command=("fake",))
    monkeypatch.setattr(energy_meter, "_run_command", _spy)
    await meter._sample()
    assert seen == [2.0]  # floor, not 2 * 0.2

    seen.clear()
    meter = NvidiaSmiMeter(interval_s=5.0, command=("fake",))
    monkeypatch.setattr(energy_meter, "_run_command", _spy)
    await meter._sample()
    assert seen == [10.0]  # 2 * interval_s, above the floor


async def test_run_command_kills_a_hanging_process_on_timeout():
    """No monkeypatching here: a real, genuinely hanging subprocess (`sleep`),
    so this exercises `_run_command`'s own `wait_for` + `proc.kill()` +
    `proc.wait()`, not just the caller's handling of the exception it
    raises."""
    import time as real_time

    start = real_time.monotonic()
    with pytest.raises(TimeoutError):
        await energy_meter._run_command(("sleep", "5"), timeout_s=0.05)
    # Killed promptly rather than left to run out its full 5s.
    assert real_time.monotonic() - start < 3.0


# ── NvidiaSmiMeter: describe() ────────────────────────────────────────────────


def test_describe_reports_kind_and_interval():
    meter = NvidiaSmiMeter(interval_s=2.5, gpu_index=1)
    d = meter.describe()
    assert d["kind"] == "nvidia_smi"
    assert d["interval_s"] == 2.5
    assert "shared" in d["notes"].lower() or "overstate" in d["notes"].lower()


# ── ExternalReadingMeter ──────────────────────────────────────────────────────


async def test_external_reading_meter_wraps_the_given_figure():
    meter = ExternalReadingMeter(wh=Decimal("12.5"), shared_device=True, note="whole-rack PDU")

    assert await meter.start() is None  # no-op
    reading = await meter.stop()

    assert reading == MeterReading(
        wh=Decimal("12.5"),
        samples=1,
        duration_s=0.0,
        kind="external",
        note="whole-rack PDU",
        shared_device=True,
    )


async def test_external_reading_meter_defaults_to_not_shared():
    meter = ExternalReadingMeter(wh=3.0)
    reading = await meter.stop()
    assert reading.shared_device is False
    assert reading.wh == Decimal("3.0")


def test_external_reading_meter_describe():
    meter = ExternalReadingMeter(wh=1.0)
    d = meter.describe()
    assert d["kind"] == "external"
    assert d["interval_s"] is None


# ── meter_for_settings ────────────────────────────────────────────────────────


def test_meter_for_settings_off_by_default():
    assert meter_for_settings(Settings()) is None


def test_meter_for_settings_off_explicit():
    assert meter_for_settings(Settings(local_energy_meter="off")) is None


def test_meter_for_settings_nvidia_smi_builds_the_meter():
    settings = Settings(
        local_energy_meter="nvidia_smi",
        local_energy_meter_interval_s=3.0,
        local_energy_meter_gpu_index=2,
    )
    meter = meter_for_settings(settings)
    assert isinstance(meter, NvidiaSmiMeter)
    assert meter.interval_s == 3.0
    assert meter.gpu_index == 2


def test_meter_for_settings_clamps_interval_to_the_floor():
    settings = Settings(local_energy_meter="nvidia_smi", local_energy_meter_interval_s=0.01)
    meter = meter_for_settings(settings)
    assert meter.interval_s == MIN_INTERVAL_S


def test_meter_for_settings_unrecognized_value_warns_and_disables(caplog):
    settings = Settings(local_energy_meter="powermetrics")
    with caplog.at_level("WARNING", logger="tret.energy_meter"):
        meter = meter_for_settings(settings)
    assert meter is None
    assert any("not recognized" in r.message for r in caplog.records)


def test_meter_for_settings_is_case_and_space_insensitive():
    settings = Settings(local_energy_meter="  NVIDIA_SMI  ")
    meter = meter_for_settings(settings)
    assert isinstance(meter, NvidiaSmiMeter)


# ── combine_accountings: energy_meter.note is unioned, not agreed ───────────
#
# Follow-up on the measured-energy review: `note` used to go through
# `_agreed` like every other per-segment field here, which meant two
# genuinely different (and both true) notes from two metered segments —
# e.g. one segment's own "fewer than 2 periodic samples" note alongside
# another segment's clean `None` — nulled the whole field out instead of
# keeping either. `combine_accountings` is exercised directly here, not
# through the engine: it needs only the accounting dicts, and this file
# already owns the energy-meter-shaped ones.


def _accounting_with_meter(energy_wh: float, *, note: str | None) -> dict:
    """A minimal per-segment accounting block carrying just enough for
    `combine_accountings`'s `energy_meter` union — the same shape
    `ModelSegment.accounting()` (engine/harness.py) produces, trimmed to
    what this test actually reads back."""
    return {
        "energy_wh": energy_wh,
        "energy_wh_estimated": energy_wh,
        "energy_source": "measured",
        "co2e_g": 0.0,
        "embodied_g": 0.0,
        "scopes": {"scope1_g": 0.0, "scope2_g": 0.0, "scope3_g": 0.0},
        "grid_co2e_basis": "location_based",
        "model": "local/test-model",
        "basis": "test",
        "factors": [],
        "caveats": [],
        "factor_layers": [],
        "energy_meter": {
            "kind": "nvidia_smi",
            "samples": 2,
            "duration_s": 1.0,
            "interval_s": 1.0,
            "shared_device": True,
            "note": note,
        },
    }


def test_combine_accountings_joins_distinct_notes_with_semicolons():
    from tret.services.emissions import combine_accountings

    a = _accounting_with_meter(1.0, note="fewer than 2 periodic samples were taken")
    b = _accounting_with_meter(2.0, note="a different, also-true note")

    combined = combine_accountings([a, b])

    note = combined["energy_meter"]["note"]
    assert "fewer than 2 periodic samples were taken" in note
    assert "a different, also-true note" in note
    assert note == "fewer than 2 periodic samples were taken; a different, also-true note"


def test_combine_accountings_drops_duplicate_notes_and_ignores_none():
    from tret.services.emissions import combine_accountings

    a = _accounting_with_meter(1.0, note="same note")
    b = _accounting_with_meter(2.0, note="same note")
    c = _accounting_with_meter(3.0, note=None)

    combined = combine_accountings([a, b, c])

    assert combined["energy_meter"]["note"] == "same note"


def test_combine_accountings_note_is_none_when_every_segment_is_none():
    from tret.services.emissions import combine_accountings

    a = _accounting_with_meter(1.0, note=None)
    b = _accounting_with_meter(2.0, note=None)

    combined = combine_accountings([a, b])

    assert combined["energy_meter"]["note"] is None
