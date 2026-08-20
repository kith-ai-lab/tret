"""The DB-free half of the egress audit trail: in-process attempt counters.

Split out of `audit.py` so that `tret.net.client` — on the pip-installable
SDK path, imported by every provider call — can count outbound attempts
without pulling in SQLAlchemy. The durable half (the `egress_calls` table,
covering the `research` class) needs a database and stays in `audit.py`,
which re-exports this module's names for existing importers.
"""
from __future__ import annotations

import logging
from collections import Counter

log = logging.getLogger("tret.net")

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
