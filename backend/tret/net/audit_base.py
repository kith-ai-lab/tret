"""The DB-free half of the egress audit trail: in-process attempt counters.

Split out of `audit.py` so that `tret.net.client` — on the pip-installable
SDK path, imported by every provider call — can count outbound attempts
without pulling in SQLAlchemy. The durable half (the `egress_calls` table,
covering the `research` class) needs a database and stays in `audit.py`,
which re-exports this module's names for existing importers.
"""
from __future__ import annotations

import hashlib
import logging
import re
from collections import Counter

log = logging.getLogger("tret.net")

DECISION_ALLOWED = "allowed"
DECISION_DENIED = "denied"

# Path kept for the audit trail, but a path is attacker-influenced text that is
# rendered back to an operator; it does not get to be arbitrarily long.
MAX_PATH_CHARS = 512

# Hex characters of the query's SHA-256 kept on an audit row: enough to tell two
# queries apart or to match a suspected one. It is NOT a secrecy control: a hash
# of a low-entropy query (a short search phrase, a small id) can be confirmed by
# anyone with database access who guesses it. Plain SHA-256 was chosen over a keyed
# HMAC so rows stay comparable across a key rotation and need no settings access
# here; the privacy promise is "the text is not stored", not "the text cannot be
# guessed from the row".
QUERY_HASH_CHARS = 16

# A whole whitespace-delimited token: a query may legitimately contain `)`, `'`,
# `]` or `"` (httpx leaves `)` unencoded), so stopping at any of them would leave
# the rest of the query behind. Everything from `?` or `#` to the token's end goes.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def strip_query(url: str) -> str:
    """`url` without its query string and fragment (and userinfo-free by construction
    of the callers). Both can carry exfiltrated data or secrets."""
    return url.split("#", 1)[0].split("?", 1)[0]


def redact_urls(text: str | None) -> str | None:
    """Strip query strings and fragments from every URL inside free text.

    Error messages quote URLs (httpx: "... for url 'https://h/?d=secret'"), and an
    audit `reason` or a log line is a place a query must never end up. Applied at
    the source where the message is built and again when the row is recorded.
    """
    if text is None:
        return None
    return _URL_RE.sub(lambda m: strip_query(m.group(0)), text)


def query_fingerprint(query: str | None) -> tuple[int | None, str | None]:
    """(length, sha256 prefix) of a URL query string or search query; (0, None) if empty.

    The audit row keeps these instead of the text. A fetch URL's query is where
    an injected prompt hides exfiltrated data (`attacker.example/?d=...`) and also
    where API keys live, so the text must not be stored, but its size and a stable
    fingerprint answer "how much left, and was it the same each time?".
    """
    raw = query or ""
    if not raw:
        return 0, None
    return len(raw), hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()[:QUERY_HASH_CHARS]


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
