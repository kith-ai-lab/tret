"""Run events: the engine yields these; the SSE endpoint serializes them.

An in-memory RunEventBus fans events out to any number of SSE subscribers and
replays the backlog to late joiners. Single-process by design (compose pins
workers=1); the multi-worker upgrade path is Postgres LISTEN/NOTIFY.
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunEvent:
    type: str  # status|routing|text_delta|tool_call|tool_result|finding_recorded|usage|done|error
    data: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_sse(self) -> str:
        return f"event: {self.type}\ndata: {json.dumps({**self.data, 'ts': self.ts})}\n\n"


class RunEventBus:
    def __init__(self, max_backlog: int = 5000):
        self._events: dict[uuid.UUID, list[RunEvent]] = {}
        self._conditions: dict[uuid.UUID, asyncio.Condition] = {}
        self._done: set[uuid.UUID] = set()
        self._max_backlog = max_backlog

    def _cond(self, run_id: uuid.UUID) -> asyncio.Condition:
        if run_id not in self._conditions:
            self._conditions[run_id] = asyncio.Condition()
        return self._conditions[run_id]

    async def publish(self, run_id: uuid.UUID, event: RunEvent) -> None:
        backlog = self._events.setdefault(run_id, [])
        backlog.append(event)
        if len(backlog) > self._max_backlog:
            del backlog[: len(backlog) - self._max_backlog]
        if event.type in ("done", "error"):
            self._done.add(run_id)
        cond = self._cond(run_id)
        async with cond:
            cond.notify_all()

    async def subscribe(self, run_id: uuid.UUID) -> Any:
        """Async iterator over events, replaying backlog first."""
        idx = 0
        while True:
            backlog = self._events.get(run_id, [])
            while idx < len(backlog):
                event = backlog[idx]
                idx += 1
                yield event
                if event.type in ("done", "error"):
                    return
            if run_id in self._done:
                return
            cond = self._cond(run_id)
            async with cond:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    yield RunEvent(type="ping")

    def forget(self, run_id: uuid.UUID) -> None:
        self._events.pop(run_id, None)
        self._conditions.pop(run_id, None)
        self._done.discard(run_id)


_bus: RunEventBus | None = None


def get_event_bus() -> RunEventBus:
    global _bus
    if _bus is None:
        _bus = RunEventBus()
    return _bus
