"""Run events: the engine yields these; the SSE endpoint serializes them.

An in-memory RunEventBus fans events out to any number of SSE subscribers and
replays the backlog to late joiners. Single-process by design (compose pins
workers=1); the multi-worker upgrade path is Postgres LISTEN/NOTIFY.

Delivery contract
-----------------
A live subscriber receives **every** event published after it subscribed, in
order, however long the run is. The backlog exists only so a *late* joiner can
catch up on what it missed, and it is capped — but capping it must never cost a
connected client an event, and the terminal `done`/`error` event is never
dropped, because the client's whole notion of "the run finished" depends on it.

That is why each subscriber has its own unbounded queue rather than an index
into the shared backlog: trimming the backlog cannot shift a queue. (The
previous implementation tracked an absolute index into the trimmed list, so once
a run exceeded the cap the index and the list stopped lining up and the
subscriber starved silently — including on `done`.)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

TERMINAL_TYPES = ("done", "error")

# How many finished runs keep their replay backlog. A finished run's events are
# only useful to a client that is still attaching, so retaining every run for the
# life of the process is pure leak: a long-lived process that executes thousands
# of runs would hold every text delta of all of them.
MAX_RETAINED_COMPLETED_RUNS = 32


@dataclass
class RunEvent:
    type: str  # status|routing|text_delta|tool_call|tool_result|finding_recorded|usage|done|error
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps({**self.data, 'ts': self.ts})}\n\n"


class RunEventBus:
    """Fan-out for run events, with a bounded replay backlog per run.

    `max_backlog` bounds only the *replay* history a late joiner sees. It is not
    a delivery limit: a subscriber that is already attached gets everything.
    """

    def __init__(self, max_backlog: int = 5000, max_retained_runs: int = MAX_RETAINED_COMPLETED_RUNS):
        # Events carry a per-run sequence number so a subscriber can tell what it
        # has already seen without depending on the position of anything.
        self._events: dict[uuid.UUID, list[tuple[int, RunEvent]]] = {}
        self._next_seq: dict[uuid.UUID, int] = {}
        self._subscribers: dict[uuid.UUID, set[asyncio.Queue[tuple[int, RunEvent]]]] = {}
        self._done: set[uuid.UUID] = set()
        self._completed_order: deque[uuid.UUID] = deque()
        self._max_backlog = max_backlog
        self._max_retained_runs = max_retained_runs

    async def publish(self, run_id: uuid.UUID, event: RunEvent) -> None:
        seq = self._next_seq.get(run_id, 0)
        self._next_seq[run_id] = seq + 1
        entry = (seq, event)

        backlog = self._events.setdefault(run_id, [])
        backlog.append(entry)
        self._trim(backlog)
        if event.type in TERMINAL_TYPES:
            self._done.add(run_id)
            if run_id not in self._completed_order:
                self._completed_order.append(run_id)
        # Queues are unbounded, so this never blocks and never drops: a slow SSE
        # client cannot cost the engine an event or stall the run.
        for queue in self._subscribers.get(run_id, ()):
            queue.put_nowait(entry)
        if event.type in TERMINAL_TYPES:
            self._release_old_completed_runs()

    def _release_old_completed_runs(self) -> None:
        """Forget the oldest finished runs nobody is watching."""
        while len(self._completed_order) > self._max_retained_runs:
            run_id = self._completed_order.popleft()
            if self.subscriber_count(run_id):
                # Still being read; it will be released when the reader detaches.
                continue
            self.forget(run_id)

    def _trim(self, backlog: list[tuple[int, RunEvent]]) -> None:
        """Drop the oldest replayable events, keeping terminal ones.

        A terminal event is what tells a late joiner the run is over, so it
        survives trimming even if it is the oldest thing left.
        """
        excess = len(backlog) - self._max_backlog
        if excess <= 0:
            return
        keep_from = 0
        while excess > 0 and keep_from < len(backlog):
            if backlog[keep_from][1].type not in TERMINAL_TYPES:
                excess -= 1
            keep_from += 1
        # Rebuild rather than slice, so a preserved terminal event in the
        # trimmed region is carried forward.
        head = [e for e in backlog[:keep_from] if e[1].type in TERMINAL_TYPES]
        backlog[:keep_from] = head

    async def subscribe(self, run_id: uuid.UUID) -> Any:
        """Async iterator over events: the backlog first, then everything live.

        Registering the queue *before* snapshotting the backlog is what closes
        the gap in the middle: an event published during the replay lands in the
        queue and is de-duplicated below rather than being missed.
        """
        queue: asyncio.Queue[tuple[int, RunEvent]] = asyncio.Queue()
        self._subscribers.setdefault(run_id, set()).add(queue)
        try:
            last_seq = -1
            for seq, event in list(self._events.get(run_id, [])):
                last_seq = seq
                yield event
                if event.type in TERMINAL_TYPES:
                    return
            if run_id in self._done and queue.empty():
                # The run is over and there is nothing further to deliver (it was
                # forgotten while we were attaching): do not hang the client.
                return
            while True:
                try:
                    seq, event = await asyncio.wait_for(queue.get(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield RunEvent(type="ping")
                    continue
                if seq <= last_seq:
                    continue  # already delivered from the backlog snapshot
                last_seq = seq
                yield event
                if event.type in TERMINAL_TYPES:
                    return
        finally:
            subscribers = self._subscribers.get(run_id)
            if subscribers is not None:
                subscribers.discard(queue)
                if not subscribers:
                    del self._subscribers[run_id]
                    # Nobody is listening and the run is over: the backlog has
                    # no reader left, so release it instead of holding a run's
                    # worth of events for the life of the process.
                    if run_id in self._done:
                        self.forget(run_id)

    def subscriber_count(self, run_id: uuid.UUID) -> int:
        return len(self._subscribers.get(run_id, ()))

    def tracked_runs(self) -> int:
        """How many runs the bus is holding state for. Diagnostics and tests."""
        return len(set(self._events) | set(self._subscribers) | self._done)

    def forget(self, run_id: uuid.UUID) -> None:
        self._events.pop(run_id, None)
        self._subscribers.pop(run_id, None)
        self._next_seq.pop(run_id, None)
        self._done.discard(run_id)
        if run_id in self._completed_order:
            self._completed_order.remove(run_id)


_bus: RunEventBus | None = None


def get_event_bus() -> RunEventBus:
    global _bus
    if _bus is None:
        _bus = RunEventBus()
    return _bus
