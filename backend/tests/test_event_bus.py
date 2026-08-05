"""RunEventBus delivery guarantees.

The live view of a run is the product's main surface, and it is driven entirely
by this bus. Two properties are load-bearing and neither is negotiable on a long
run: a connected subscriber sees every event in order, and the terminal
`done`/`error` event always arrives. Without the terminal event the client cannot
tell "finished" from "connection dropped" — `useRunStream` treats the eventual
close as a transport error and resets accumulated text.

The bus is bounded, so the tests below deliberately use a tiny `max_backlog` and
publish well past it: the cap bounds the *replay history* for late joiners, not
what a live subscriber receives.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from bench.engine.events import RunEvent, RunEventBus


async def _drain(bus: RunEventBus, run_id: uuid.UUID, collected: list[RunEvent]) -> None:
    async for event in bus.subscribe(run_id):
        collected.append(event)


def _deltas(events: list[RunEvent]) -> list[str]:
    return [e.data["text"] for e in events if e.type == "text_delta"]


def _delta(i: int) -> RunEvent:
    return RunEvent(type="text_delta", data={"text": f"chunk-{i}"})


# ── live subscribers lose nothing ────────────────────────────────────────────
async def test_live_subscriber_receives_every_event_past_the_backlog_cap():
    """The reported bug: with max_backlog=5 and 12 deltas, the subscriber got
    the first 5 and then nothing — not the rest, and not `done`."""
    bus = RunEventBus(max_backlog=5)
    run_id = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)  # let the subscriber attach

    for i in range(12):
        await bus.publish(run_id, _delta(i))
        await asyncio.sleep(0)  # cooperative: the subscriber can keep up
    await bus.publish(run_id, RunEvent(type="done", data={"status": "completed"}))
    await asyncio.wait_for(task, timeout=2.0)

    assert _deltas(got) == [f"chunk-{i}" for i in range(12)]
    assert got[-1].type == "done"


async def test_a_long_run_delivers_thousands_of_events_in_order():
    """Production publishes one event per streamed token; 5000 is routine."""
    bus = RunEventBus(max_backlog=50)
    run_id = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)

    total = 2000
    for i in range(total):
        await bus.publish(run_id, _delta(i))
    await bus.publish(run_id, RunEvent(type="done", data={}))
    await asyncio.wait_for(task, timeout=5.0)

    assert _deltas(got) == [f"chunk-{i}" for i in range(total)]
    assert got[-1].type == "done"


async def test_a_subscriber_that_never_yields_still_gets_everything():
    """A slow SSE client must not lose events, and must not stall the engine:
    nothing here awaits the subscriber between publishes."""
    bus = RunEventBus(max_backlog=3)
    run_id = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)

    for i in range(20):
        await bus.publish(run_id, _delta(i))
    await bus.publish(run_id, RunEvent(type="error", data={"message": "provider reset"}))
    await asyncio.wait_for(task, timeout=2.0)

    assert _deltas(got) == [f"chunk-{i}" for i in range(20)]
    assert got[-1].type == "error"
    assert got[-1].data["message"] == "provider reset"


async def test_two_subscribers_each_get_the_full_stream():
    bus = RunEventBus(max_backlog=4)
    run_id = uuid.uuid4()
    first: list[RunEvent] = []
    second: list[RunEvent] = []
    tasks = [
        asyncio.create_task(_drain(bus, run_id, first)),
        asyncio.create_task(_drain(bus, run_id, second)),
    ]
    await asyncio.sleep(0)

    for i in range(10):
        await bus.publish(run_id, _delta(i))
    await bus.publish(run_id, RunEvent(type="done", data={}))
    await asyncio.wait_for(asyncio.gather(*tasks), timeout=2.0)

    expected = [f"chunk-{i}" for i in range(10)]
    assert _deltas(first) == expected
    assert _deltas(second) == expected


async def test_no_duplicates_when_subscribing_mid_run():
    """A client that attaches while events are flowing sees the backlog once and
    then the live tail — no event twice, none skipped in the handover."""
    bus = RunEventBus(max_backlog=100)
    run_id = uuid.uuid4()
    for i in range(5):
        await bus.publish(run_id, _delta(i))

    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)
    for i in range(5, 10):
        await bus.publish(run_id, _delta(i))
    await bus.publish(run_id, RunEvent(type="done", data={}))
    await asyncio.wait_for(task, timeout=2.0)

    assert _deltas(got) == [f"chunk-{i}" for i in range(10)]


# ── the terminal event is never lost ─────────────────────────────────────────
async def test_terminal_event_survives_backlog_trimming_for_a_late_joiner():
    """A client attaching after a long run finished must still be told it ended,
    even though the events it wants to replay have been trimmed away."""
    bus = RunEventBus(max_backlog=3)
    run_id = uuid.uuid4()
    for i in range(10):
        await bus.publish(run_id, _delta(i))
    await bus.publish(run_id, RunEvent(type="done", data={"status": "completed"}))
    # Keep publishing past the cap after the terminal event.
    for i in range(10, 20):
        await bus.publish(run_id, _delta(i))

    got = [event async for event in bus.subscribe(run_id)]
    assert any(e.type == "done" for e in got)
    assert got[-1].type == "done"


async def test_late_joiner_on_a_finished_run_terminates_immediately():
    bus = RunEventBus(max_backlog=10)
    run_id = uuid.uuid4()
    await bus.publish(run_id, RunEvent(type="status", data={"status": "running"}))
    await bus.publish(run_id, RunEvent(type="done", data={}))

    got = await asyncio.wait_for(
        _collect(bus, run_id), timeout=1.0
    )  # must not sit on the 30s ping timeout
    assert [e.type for e in got] == ["status", "done"]


async def _collect(bus: RunEventBus, run_id: uuid.UUID) -> list[RunEvent]:
    return [event async for event in bus.subscribe(run_id)]


async def test_a_forgotten_run_does_not_hang_a_subscriber():
    bus = RunEventBus(max_backlog=10)
    run_id = uuid.uuid4()
    await bus.publish(run_id, RunEvent(type="done", data={}))
    bus.forget(run_id)
    # Nothing known about this run at all: the generator ends rather than
    # emitting pings forever. (It has no way to learn the run is over.)
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)
    await bus.publish(run_id, RunEvent(type="done", data={}))
    await asyncio.wait_for(task, timeout=2.0)
    assert [e.type for e in got] == ["done"]


# ── state is released ────────────────────────────────────────────────────────
async def test_subscriber_state_is_released_when_the_stream_ends():
    bus = RunEventBus(max_backlog=10)
    run_id = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)
    assert bus.subscriber_count(run_id) == 1

    await bus.publish(run_id, RunEvent(type="done", data={}))
    await asyncio.wait_for(task, timeout=2.0)

    assert bus.subscriber_count(run_id) == 0
    # The last reader of a finished run releases its backlog too.
    assert bus.tracked_runs() == 0


async def test_a_cancelled_subscriber_is_unregistered():
    bus = RunEventBus(max_backlog=10)
    run_id = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, run_id, got))
    await asyncio.sleep(0)
    assert bus.subscriber_count(run_id) == 1

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert bus.subscriber_count(run_id) == 0


async def test_completed_runs_do_not_accumulate_forever():
    """A long-lived process executing many runs must not hold every run's
    events. Unwatched finished runs are released oldest-first."""
    bus = RunEventBus(max_backlog=10, max_retained_runs=4)
    run_ids = [uuid.uuid4() for _ in range(20)]
    for run_id in run_ids:
        for i in range(3):
            await bus.publish(run_id, _delta(i))
        await bus.publish(run_id, RunEvent(type="done", data={}))

    assert bus.tracked_runs() <= 4
    # The most recent finished runs are the ones kept, so a client that connects
    # just after a run ends still gets its result.
    got = [e async for e in bus.subscribe(run_ids[-1])]
    assert got[-1].type == "done"


async def test_a_run_still_being_watched_is_not_released():
    bus = RunEventBus(max_backlog=10, max_retained_runs=1)
    watched = uuid.uuid4()
    got: list[RunEvent] = []
    task = asyncio.create_task(_drain(bus, watched, got))
    await asyncio.sleep(0)
    await bus.publish(watched, _delta(0))

    for _ in range(5):  # push the retention window well past the watched run
        other = uuid.uuid4()
        await bus.publish(other, RunEvent(type="done", data={}))

    await bus.publish(watched, RunEvent(type="done", data={}))
    await asyncio.wait_for(task, timeout=2.0)
    assert _deltas(got) == ["chunk-0"]
    assert got[-1].type == "done"
