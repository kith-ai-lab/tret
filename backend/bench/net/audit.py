"""The record of what bench talked to.

Two tiers, deliberately not one:

* **The `egress_calls` table** covers the `research` class — every search and
  every fetch, allowed or denied, with the run that caused it. That is where an
  audit question actually lands ("what did this deliverable read, and when?"),
  and it is the only class whose destination a model chose.
* **In-process counters** cover `provider` and `catalog`. A row per model call
  would double the write volume of a run to record something the run already
  persists in full (`runs.routing`, `runs.model_used`, the transcript), and the
  provider stream has no database session to write it with anyway.

Query strings are dropped, never stored. They carry API keys and search terms
that are often the private part of a question, and a host plus a path answers
the audit question without becoming a second place secrets accumulate.
"""
from __future__ import annotations

import uuid
import logging
from collections import Counter

from sqlalchemy.ext.asyncio import AsyncSession

from bench.db.engine import get_session_factory
from bench.db.models import EgressCall

log = logging.getLogger("bench.net")

DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"

# Path kept for the audit trail, but a path is attacker-influenced text that is
# rendered back to an operator; it does not get to be arbitrarily long.
MAX_PATH_CHARS = 512

_counters: Counter[tuple[str, str]] = Counter()  # (class, host) -> attempts


def note_attempt(egress_class: str, host: str) -> None:
    """Count one outbound attempt. Cheap enough for the provider hot path."""
    _counters[(egress_class, host)] += 1


def counters() -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for (egress_class, host), count in _counters.items():
        out.setdefault(egress_class, {})[host] = count
    return out


def reset_counters() -> None:
    _counters.clear()


async def record(
    db: AsyncSession,
    *,
    egress_class: str,
    method: str,
    host: str,
    path: str,
    decision: str,
    run_id: uuid.UUID | None = None,
    project_id: uuid.UUID | None = None,
    status_code: int | None = None,
    byte_count: int = 0,
    duration_ms: int = 0,
    reason: str | None = None,
) -> EgressCall:
    """Persist one research-class call. The caller commits.

    Flushed rather than left pending so the row exists before the tool result is
    handed back to the model — the engine commits after each tool call, and an
    audit row that only materialises if the *next* thing succeeds is not an audit
    row. Denials are recorded exactly like successes: a refused request is the
    most interesting line in this table.
    """
    row = EgressCall(
        run_id=run_id,
        project_id=project_id,
        egress_class=egress_class,
        method=method.upper(),
        host=(host or "")[:255],
        path=(path or "")[:MAX_PATH_CHARS],
        status_code=status_code,
        byte_count=byte_count,
        duration_ms=duration_ms,
        decision=decision,
        reason=reason,
    )
    db.add(row)
    await db.flush()
    return row


async def record_durably(**kwargs) -> None:
    """Record a call on its own session, surviving the caller's rollback.

    Denials need this. A denied fetch comes back to the model as a tool error,
    and the engine's contract for a tool error is to roll the failed call's
    partial writes out of the session (`engine/harness.py`) — which would discard
    the audit row along with them. An audit trail that a refused request can
    erase is not an audit trail, and "we refused 400 requests to 10.0.0.0/8 last
    week" is the single most valuable line this table holds.

    So the row goes through a separate short-lived session and is committed
    there. Denials are rare by construction, so the extra connection is not a hot
    path. Any failure writing it is swallowed: losing an audit row is bad, and
    turning a policy denial into a crashed run because the audit write failed is
    worse — the denial itself already reached the model and the log.
    """
    try:
        async with get_session_factory()() as session:
            await record(session, **kwargs)
            await session.commit()
    except Exception:  # noqa: BLE001 - see docstring
        log.warning("could not persist an egress audit row for %s", kwargs.get("host"), exc_info=True)
