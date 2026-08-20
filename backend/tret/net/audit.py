"""The record of what tret talked to.

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

The in-process counters live in `audit_base.py` — DB-free, so `tret.net.client`
can note an attempt without importing SQLAlchemy — and are re-exported here
unchanged for existing importers of this module.
"""
from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from tret.db.engine import get_session_factory
from tret.db.models import EgressCall
from tret.net.audit_base import (
    DECISION_ALLOWED,
    DECISION_DENIED,
    MAX_PATH_CHARS,
    counters,
    log,
    note_attempt,
    reset_counters,
)

__all__ = [
    "DECISION_ALLOWED",
    "DECISION_DENIED",
    "MAX_PATH_CHARS",
    "counters",
    "log",
    "note_attempt",
    "record",
    "record_durably",
    "reset_counters",
]


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
