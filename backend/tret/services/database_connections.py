"""Read-only Postgres / Supabase connections for a workspace.

A workspace admin connects one or more external Postgres databases (typically
Supabase); runs then get three read-only tools (`list_database_tables`,
`describe_database_table`, `query_database` in engine/tools.py). This module is
everything between the HTTP router (`api/database_connections.py`), the tools and
the network door (`tret/net/pgconnect.py`).

**Storage, no migration.** Each database is one `WorkspaceConnection` row whose
`provider` is `postgres:<slug>` (the column is plain Text: no enum, no check, no
length limit; the only constraint is UNIQUE(workspace_id, provider), which the
distinct slugs satisfy). The full DSN is Fernet-encrypted with
`services/credentials.py::get_fernet` (the same envelope the OAuth refresh tokens
use) into `encrypted_refresh_token`; non-secret display metadata lives in the
`selected_resources` JSONB. The OAuth code never sees these rows: every
OAuth path looks a row up by an exact `gdrive`/`m365` provider, and the one list
endpoint that scans a workspace's rows (`GET /api/connections`) filters to
`PROVIDER_SPECS`.

**Nothing secret leaves.** The DSN and password are never returned, logged or put
in an exception message or activity row. Driver errors are mapped to fixed
vocabulary (`pgconnect.describe_failure`) or, for Postgres server errors,
SQLSTATE plus the server's message passed through `scrub`.

**Read-only, in depth.** The role's own permissions are the real boundary (the
create-time warning says so). On top of that every call opens a fresh connection,
starts an explicit `BEGIN READ ONLY` transaction (asyncpg `transaction(readonly=
True)`), sets `statement_timeout`, `idle_in_transaction_session_timeout` and
`default_transaction_read_only` with `SET LOCAL` (transaction-scoped, so nothing
leaks into a shared pooler backend), runs ONE statement through the extended
protocol (the server rejects multi-statement strings), reads at most the row cap
through a cursor in small batches, and always rolls back. Row-returning
statements are run through a server-side wrapper that bounds every cell
(`left(col::text, N)`, bytea as a length note) so a huge value never reaches this
process. A client-side classifier additionally
refuses anything not starting SELECT/WITH/SHOW/EXPLAIN/TABLE/VALUES and any
EXPLAIN ANALYZE.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import re
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal
from urllib.parse import parse_qsl, quote, unquote, urlsplit

import asyncpg
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tret.config import get_settings
from tret.db.models import WorkspaceConnection
from tret.engine.extensions import get_extension_registry
from tret.net import pgconnect
from tret.net.policy import MODE_OFF, master_mode
from tret.services.connections import CONNECT_ACTION, USE_ACTION, record_connection_activity
from tret.services.credentials import get_fernet

log = logging.getLogger("tret.database_connections")

PROVIDER_PREFIX = "postgres:"
MAX_CONNECTIONS_PER_WORKSPACE = 5
ALLOWED_PORTS = frozenset({5432, 6543})
# Hosts for which "the server's certificate chain is not in a public trust store"
# is expected (Supabase signs with its own CA), so an unverifiable certificate
# degrades to encrypted-unverified instead of failing. Fixed on purpose: an
# operator widening `db_connect_allow_hosts` does not widen the TLS fallback.
SUPABASE_TLS_FALLBACK_SUFFIXES = ("supabase.co", "pooler.supabase.com")
# Roles refused at create/test time unless TRET_DB_CONNECT_ALLOW_PRIVILEGED_ROLES.
PRIVILEGED_ROLE_NAMES = ("postgres", "service_role", "supabase_admin")
# Consecutive tool-time connect failures after which the stored row flips to
# "error" (an authentication failure flips it at once).
MAX_CONSECUTIVE_CONNECT_FAILURES = 3
PROBE_RATE_LIMIT = 6  # create/test attempts per workspace per minute
PROBE_RATE_WINDOW_SECONDS = 60.0
NEEDS_ATTENTION = (
    "This database connection needs attention from a workspace admin (re-test it in "
    "Settings > Connections once the problem is fixed)."
)

IDLE_IN_TRANSACTION_TIMEOUT_MS = 15_000
MAX_CELL_CHARS = 2_000
MAX_OUTPUT_BYTES = 90_000  # rendered table; the engine's own result cap is ~100 KB
FETCH_BATCH_ROWS = 50
# Worst-case bytes one fetched batch may hold (cells are cut server-side to
# MAX_CELL_CHARS+1 characters, up to 4 UTF-8 bytes each); wide results fetch
# fewer rows per batch so the product stays bounded.
FETCH_BATCH_BYTES = 500_000
MAX_SQL_CHARS = 20_000
SAMPLE_ROWS = 3
TABLE_LIST_LIMIT = 500
QUERY_ACTIVITY_TARGET_CHARS = 500

# Schemas hidden from list_database_tables unless include_system is set.
SYSTEM_SCHEMAS = (
    "pg_catalog",
    "information_schema",
    "auth",
    "storage",
    "vault",
    "realtime",
    "_realtime",
    "supabase_functions",
    "supabase_migrations",
    "extensions",
    "graphql",
    "graphql_public",
    "pgsodium",
    "pgsodium_masks",
    "net",
    "pgbouncer",
)

_SLUG_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")


class DatabaseConnectionError(Exception):
    """A failure whose `.message` is safe to show a user or hand to a model.
    `kind`: policy (refused by a rule) | connect (could not connect/authenticate)
    | query (the server rejected a statement) | unavailable (no such connection,
    feature off, egress off) | gate (the workspace gate refused) | input | auth
    (the server rejected the stored credentials) | tls | limit | busy / rate (too
    many create/test attempts)."""

    def __init__(self, message: str, *, kind: str = "connect"):
        super().__init__(message)
        self.message = message
        self.kind = kind


# ── scrubbing ────────────────────────────────────────────────────────────────
_DSN_IN_TEXT_RE = re.compile(r"postgres(?:ql)?://[^\s'\"<>]+", re.IGNORECASE)


def scrub(text: str | None, *secrets: str | None) -> str:
    """`text` with every given secret (raw and percent-encoded) and anything that
    looks like a postgres URL replaced. Secrets shorter than 4 characters are
    skipped: replacing "ab" would garble every message that contains it."""
    out = text or ""
    # Longest first: the netloc contains the password, and must go whole.
    for secret in sorted((x for x in secrets if x), key=len, reverse=True):
        if len(secret) < 4:
            continue
        for variant in {secret, quote(secret, safe=""), quote(secret)}:
            if variant:
                out = out.replace(variant, "[redacted]")
    return _DSN_IN_TEXT_RE.sub("[redacted-dsn]", out)


# ── DSN parsing ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ParsedDsn:
    user: str
    password: str = field(repr=False)
    host: str
    port: int
    database: str
    sslmode: str | None = None
    netloc: str = field(default="", repr=False)  # `user:password@host:port` as written

    def secrets(self) -> tuple[str, ...]:
        return (self.password, self.netloc)


_ALLOWED_SSLMODES = ("require", "verify-ca", "verify-full")


def parse_dsn(dsn: str) -> ParsedDsn:
    """Parse a `postgres://` / `postgresql://` URL. Raises
    `DatabaseConnectionError(kind="input")`; messages never echo the input."""
    if not isinstance(dsn, str) or not dsn.strip():
        raise DatabaseConnectionError("The connection string is empty.", kind="input")
    dsn = dsn.strip()
    if len(dsn) > 2048 or any(ord(c) < 32 for c in dsn):
        raise DatabaseConnectionError("The connection string is malformed.", kind="input")
    try:
        parts = urlsplit(dsn)
        port = parts.port
        hostname = parts.hostname
    except ValueError:
        raise DatabaseConnectionError("The connection string is malformed.", kind="input") from None
    if parts.scheme.lower() not in ("postgres", "postgresql"):
        raise DatabaseConnectionError(
            "The connection string must start with postgres:// or postgresql://.", kind="input"
        )
    if parts.fragment:
        raise DatabaseConnectionError("The connection string has an unexpected fragment.", kind="input")
    sslmode = None
    if parts.query:
        try:
            params = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            raise DatabaseConnectionError("The connection string's query is malformed.", kind="input") from None
        for key, value in params:
            if key != "sslmode":
                raise DatabaseConnectionError(
                    f"Connection string option {key[:40]!r} is not supported; only sslmode is accepted.",
                    kind="input",
                )
            sslmode = value
        if sslmode is not None and sslmode not in _ALLOWED_SSLMODES:
            raise DatabaseConnectionError(
                "TLS is always required: sslmode must be require, verify-ca or verify-full (or omitted).",
                kind="input",
            )
    if not hostname or "," in (parts.netloc.rpartition("@")[2]):
        raise DatabaseConnectionError("The connection string must name exactly one host.", kind="input")
    host = hostname.lower().rstrip(".")
    is_ip = _is_ip(host)
    if not is_ip and not _HOST_RE.match(host):
        raise DatabaseConnectionError(
            "The host must be an ASCII hostname (use the punycode form for internationalised names).",
            kind="input",
        )
    user = unquote(parts.username or "")
    password = unquote(parts.password or "")
    if _has_control(user) or _has_control(password) or _has_control(unquote(hostname)):
        raise DatabaseConnectionError("The connection string is malformed.", kind="input")
    if not user:
        raise DatabaseConnectionError("The connection string must include a user.", kind="input")
    if not password:
        raise DatabaseConnectionError("The connection string must include a password.", kind="input")
    path = unquote(parts.path or "")
    database = path[1:] if path.startswith("/") else path
    if "/" in database:
        raise DatabaseConnectionError("The connection string's database name is malformed.", kind="input")
    if _has_control(database):
        raise DatabaseConnectionError("The connection string is malformed.", kind="input")
    database = database or "postgres"
    return ParsedDsn(
        user=user, password=password, host=host, port=port or 5432, database=database, sslmode=sslmode,
        netloc=parts.netloc,
    )


def _has_control(text: str) -> bool:
    """A control character (below 32, or DEL) anywhere in `text`; checked AFTER
    percent-decoding, where `%0a` has become a real newline."""
    return any(ord(c) < 32 or ord(c) == 127 for c in text)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return False


# ── host / port policy ───────────────────────────────────────────────────────
def host_matches_suffixes(host: str, suffixes) -> bool:
    """`host` equals a suffix or is a subdomain of it (label boundary), case- and
    trailing-dot-insensitive. `evilsupabase.co` does not match `supabase.co`."""
    candidate = (host or "").strip().lower().rstrip(".")
    for suffix in suffixes:
        s = (suffix or "").strip().lower().lstrip(".").rstrip(".")
        if s and s != "*" and (candidate == s or candidate.endswith("." + s)):
            return True
    return False


def check_host_policy(host: str, port: int) -> None:
    """Raise `DatabaseConnectionError(kind="policy")` naming the rule that failed.
    Runs at create time AND before every connect."""
    settings = get_settings()
    allow = list(settings.db_connect_allow_hosts or [])
    wildcard = "*" in allow
    if port not in ALLOWED_PORTS:
        raise DatabaseConnectionError(
            "Only ports 5432 (direct) and 6543 (pooler) are allowed for database connections.",
            kind="policy",
        )
    if _is_ip(host) and not wildcard:
        raise DatabaseConnectionError(
            "IP-address hosts are not allowed; use the database's hostname.", kind="policy"
        )
    if wildcard:
        return  # any PUBLIC host; public-ness is checked on the resolved addresses
    if not host_matches_suffixes(host, allow):
        raise DatabaseConnectionError(
            "That host is not allowed on this deployment. Allowed host suffixes: "
            + ", ".join(sorted(allow))
            + " (TRET_DB_CONNECT_ALLOW_HOSTS).",
            kind="policy",
        )


async def open_connection(parsed: ParsedDsn):
    """Policy check, resolve + public-address check, connect (vetted IP, TLS).
    Returns `(connection, tls_mode)`. The check runs on every call."""
    if master_mode() == MODE_OFF:
        raise DatabaseConnectionError(
            "Outbound access is disabled on this deployment (TRET_EGRESS).", kind="unavailable"
        )
    check_host_policy(parsed.host, parsed.port)
    settings = get_settings()
    wildcard = "*" in (settings.db_connect_allow_hosts or [])
    # The encrypted-but-unverified retry needs ALL of: the operator's opt-in, a
    # Supabase host, and no verify-ca/verify-full in this connection's DSN.
    allow_unverified = (
        bool(settings.db_connect_allow_unverified_tls)
        and host_matches_suffixes(parsed.host, SUPABASE_TLS_FALLBACK_SUFFIXES)
        and parsed.sslmode not in ("verify-ca", "verify-full")
    )
    try:
        return await pgconnect.open_pinned(
            host=parsed.host,
            port=parsed.port,
            user=parsed.user,
            password=parsed.password,
            database=parsed.database,
            allow_literal_ip=wildcard,
            allow_unverified_tls=allow_unverified,
            ca_pem=settings.db_connect_ca_pem,
        )
    except pgconnect.PgNetworkError as exc:
        kind = exc.kind if exc.kind in ("policy", "auth") else "connect"
        raise DatabaseConnectionError(scrub(exc.message, *parsed.secrets()), kind=kind) from None
    except asyncpg.PostgresError as exc:  # defensive: open_pinned maps these already
        raise DatabaseConnectionError(
            scrub(pgconnect.describe_failure(exc), *parsed.secrets()), kind="connect"
        ) from None


# ── encryption at rest ───────────────────────────────────────────────────────
def encrypt_dsn(dsn: str) -> bytes:
    return get_fernet().encrypt(dsn.encode())


def decrypt_dsn(blob: bytes) -> str:
    try:
        return get_fernet().decrypt(blob).decode()
    except (InvalidToken, ValueError):
        raise DatabaseConnectionError(
            "The stored connection secret can no longer be decrypted (the deployment's secret key "
            "changed). Remove this database and add it again.",
            kind="unavailable",
        ) from None


# ── statement classification ─────────────────────────────────────────────────
ALLOWED_FIRST_KEYWORDS = ("select", "with", "show", "explain", "table", "values")
_ANALYZE_RE = re.compile(r"\banaly[sz]e\b", re.IGNORECASE)


def _skip_noise(sql: str, i: int = 0, *, parens: bool = False) -> int:
    """Index of the first character at or after `i` that is not whitespace, a
    `-- line` comment or a (nested) `/* block */` comment (nor, with `parens`, an
    opening parenthesis). `len(sql)` when only noise (or an unterminated block
    comment) remains."""
    n = len(sql)
    while i < n:
        c = sql[i]
        if c.isspace() or (parens and c == "(") or c == "﻿":
            i += 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:  # unterminated comment: nothing executable follows
                return n
        else:
            break
    return i


def _strip_leading_noise(sql: str) -> str:
    """Drop leading whitespace, comments and opening parentheses
    (`(select 1) union ...`)."""
    return sql[_skip_noise(sql, 0, parens=True):]


# Options an EXPLAIN may carry in parentheses. Everything else (ANALYZE in any
# spelling, quoted or escaped, plus WAL/BUFFERS/TIMING which only matter with
# ANALYZE) is refused rather than denylisted.
_EXPLAIN_OPT = (
    r"(?:format\s+(?:text|xml|json|yaml)"
    r"|(?:verbose|costs|settings|summary)(?:\s+(?:true|false|on|off|0|1))?)"
)
_EXPLAIN_OPTIONS_RE = re.compile(
    rf"\(\s*{_EXPLAIN_OPT}(?:\s*,\s*{_EXPLAIN_OPT})*\s*\)", re.IGNORECASE
)


def statement_kind(sql: str) -> str:
    """The lower-cased first keyword of `sql` after comments and parentheses."""
    m = re.match(r"[A-Za-z]+", _strip_leading_noise(sql))
    return m.group(0).lower() if m else ""


def classify_statement(sql: str) -> tuple[bool, str]:
    """`(ok, reason)`. Accepts a first keyword of SELECT/WITH/SHOW/EXPLAIN/TABLE/
    VALUES; refuses EXPLAIN ANALYZE and any parenthesised EXPLAIN option list that
    is not on the small safe allowlist. A data-modifying CTE (`WITH x AS (DELETE
    ...) SELECT`) passes THIS check on purpose: the read-only transaction is what
    refuses it, and the tests pin that the transaction is set up first."""
    if not isinstance(sql, str) or not sql.strip():
        return False, "The statement is empty."
    if len(sql) > MAX_SQL_CHARS:
        return False, f"The statement is longer than {MAX_SQL_CHARS} characters."
    if "\x00" in sql:
        return False, "The statement contains a NUL character."
    body = _strip_leading_noise(sql)
    m = re.match(r"[A-Za-z]+", body)
    keyword = m.group(0).lower() if m else ""
    if keyword not in ALLOWED_FIRST_KEYWORDS:
        return False, (
            "Only read-only statements are allowed: the statement must start with SELECT, WITH, "
            "SHOW, EXPLAIN, TABLE or VALUES."
        )
    if keyword == "explain":
        if _ANALYZE_RE.search(sql):
            return False, "EXPLAIN ANALYZE is not allowed because it executes the statement."
        rest = body[m.end():]
        j = _skip_noise(rest)
        if rest[j:j + 1] == "(" and not _EXPLAIN_OPTIONS_RE.match(rest, j):
            return False, (
                "EXPLAIN options must be plain: only FORMAT, VERBOSE, COSTS, SETTINGS and SUMMARY "
                "with literal values are allowed."
            )
    return True, ""


def strip_terminator(sql: str) -> str:
    """`sql` without ONE trailing statement terminator (a `;` followed only by
    whitespace and comments). Walks the text so a `;` inside a string, quoted
    identifier, dollar quote or comment is never taken for it. The text is
    returned unchanged when it ends otherwise or the walk cannot be trusted
    (unterminated literal)."""
    n = len(sql)
    i = 0
    last_semi = -1  # index of the latest top-level `;` with only noise after it
    while i < n:
        c = sql[i]
        j = _skip_noise(sql, i) if (c.isspace() or sql.startswith(("--", "/*"), i)) else i
        if j != i:
            i = j
            continue
        if c == ";":
            last_semi = i
            i += 1
            continue
        last_semi = -1  # real tokens after a `;` mean it was not trailing
        if c == "'":
            backslash = i > 0 and sql[i - 1] in "eE" and (i < 2 or not (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            i += 1
            while True:
                if i >= n:
                    return sql
                if backslash and sql[i] == "\\":
                    i += 2
                    continue
                if sql[i] == "'":
                    if sql.startswith("''", i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    return sql
                if sql[i] == '"':
                    if sql.startswith('""', i):
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
        elif c == "$":
            m = re.match(r"\$(?:[A-Za-z_\x80-\uffff][A-Za-z0-9_\x80-\uffff]*)?\$", sql[i:])
            if m:
                tag = m.group(0)
                end = sql.find(tag, i + len(tag))
                if end == -1:
                    return sql
                i = end + len(tag)
            else:
                i += 1
        else:
            i += 1
    if last_semi == -1:
        return sql
    return sql[:last_semi].rstrip() + sql[last_semi + 1:] if sql[last_semi + 1:].strip() else sql[:last_semi].rstrip()


# ── identifiers ──────────────────────────────────────────────────────────────
_BARE_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]{0,62}$")


def quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def parse_table_identifier(raw: str) -> tuple[str | None, str]:
    """`table` or `schema.table`; each part bare (`[A-Za-z_][A-Za-z0-9_$]*`, folded
    to lower case like Postgres does) or double-quoted (`""` escapes a quote).
    Raises `DatabaseConnectionError(kind="input")`."""
    bad = DatabaseConnectionError(
        'Invalid table name: use table or schema.table (quote names that need it, e.g. "Mixed"."Case").',
        kind="input",
    )
    if not isinstance(raw, str) or not raw.strip() or len(raw) > 200:
        raise bad
    s = raw.strip()
    parts: list[str] = []
    i, n = 0, len(s)
    while True:
        if i >= n:
            raise bad
        if s[i] == '"':
            i += 1
            buf: list[str] = []
            while True:
                if i >= n:
                    raise bad
                if s[i] == '"':
                    if i + 1 < n and s[i + 1] == '"':
                        buf.append('"')
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(s[i])
                i += 1
            name = "".join(buf)
            if not name or len(name) > 63 or any(ord(c) < 32 for c in name):
                raise bad
            parts.append(name)
        else:
            j = i
            while j < n and s[j] != ".":
                j += 1
            token = s[i:j]
            if not _BARE_IDENT_RE.match(token):
                raise bad
            parts.append(token.lower())
            i = j
        if i >= n:
            break
        if s[i] != ".":
            raise bad
        i += 1
    if len(parts) == 1:
        return None, parts[0]
    if len(parts) == 2:
        return parts[0], parts[1]
    raise bad


# ── value serialisation ──────────────────────────────────────────────────────
def cell_text(value, type_name: str | None = None) -> tuple[str, bool]:
    """A value as display text, capped at MAX_CELL_CHARS. Returns (text, truncated)."""
    if value is None:
        return "NULL", False
    if isinstance(value, bool):
        text = "true" if value else "false"
    elif isinstance(value, (bytes, bytearray, memoryview)):
        text = f"<{len(bytes(value))} bytes>"
    elif isinstance(value, str):
        text = value
        if type_name in ("json", "jsonb"):
            try:
                text = json.dumps(json.loads(value), separators=(",", ":"), ensure_ascii=False)
            except ValueError:
                pass
    elif isinstance(value, (dict, list, tuple)):
        text = json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=_json_default)
    elif isinstance(value, (Decimal, int, float, uuid.UUID)):
        text = str(value)
    elif isinstance(value, (datetime, date, dtime)):
        text = value.isoformat()
    elif isinstance(value, timedelta):
        text = str(value)
    else:
        text = str(value)
    text = text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("\t", " ")
    if len(text) > MAX_CELL_CHARS:
        return text[:MAX_CELL_CHARS] + "…", True
    return text, False


def _json_default(value):
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<{len(bytes(value))} bytes>"
    return str(value)


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[list[str]]
    more_rows: bool = False  # the statement had more rows than the row cap
    truncated_cells: int = 0
    truncated_size: bool = False
    duration_ms: int = 0

    def render(self, *, header: str | None = None) -> str:
        out: list[str] = []
        if header:
            out.append(header)
        if not self.columns:
            out.append("(the statement returned no result set)")
            return "\n".join(out)
        lines = [" | ".join(self.columns)]
        size = len(lines[0].encode())
        shown = 0
        for row in self.rows:
            line = " | ".join(row)
            size += len(line.encode()) + 1
            if size > MAX_OUTPUT_BYTES:
                self.truncated_size = True
                break
            lines.append(line)
            shown += 1
        out.extend(lines)
        notes: list[str] = []
        if not self.rows:
            notes.append("0 rows")
        if self.more_rows:
            notes.append(f"output truncated at the row cap: more rows exist than the {shown} shown")
        if self.truncated_size:
            notes.append(f"output truncated at ~{MAX_OUTPUT_BYTES // 1000} KB: {shown} of {len(self.rows)} fetched rows shown")
        if self.truncated_cells:
            notes.append(f"{self.truncated_cells} cell(s) truncated to {MAX_CELL_CHARS} characters")
        if notes:
            out.append("[" + "; ".join(notes) + "]")
        return "\n".join(out)

    @property
    def truncated(self) -> bool:
        return self.more_rows or self.truncated_size or self.truncated_cells > 0


# ── running statements ───────────────────────────────────────────────────────
@asynccontextmanager
async def read_only_transaction(conn, *, timeout_seconds: int):
    """`BEGIN READ ONLY` + transaction-local safety settings; always rolled back.
    The three SET LOCAL statements are issued before anything the caller runs."""
    tr = conn.transaction(readonly=True)
    await tr.start()
    try:
        await conn.execute(f"SET LOCAL statement_timeout = {int(timeout_seconds) * 1000}")
        await conn.execute(f"SET LOCAL idle_in_transaction_session_timeout = {IDLE_IN_TRANSACTION_TIMEOUT_MS}")
        await conn.execute("SET LOCAL default_transaction_read_only = on")
        yield
    finally:
        try:
            await tr.rollback()
        except Exception:  # noqa: BLE001 - nothing to salvage; the connection is closed next
            log.info("database read-only transaction rollback failed", exc_info=False)


# Statement kinds whose rows are bounded on the server (see `build_bounded_sql`).
# SHOW and EXPLAIN cannot be wrapped in a subquery and return small output, so
# they take the direct path (still fetched in bounded batches).
BOUNDED_KINDS = frozenset({"select", "with", "table", "values"})


def build_bounded_sql(inner: str, types: list[str | None], limit: int) -> str:
    """The server-side size bound for a row-returning statement: every output
    column is renamed positionally (`AS q(c1, c2, ...)`, which copes with
    duplicate and unnamed columns) and cut to MAX_CELL_CHARS+1 characters by
    `left(c::text, N)`; bytea is reduced to its length. `inner` is the user's
    statement, already accepted by the server as ONE statement; the newline before
    `)` keeps a trailing `--` comment from swallowing it."""
    exprs = []
    for i, type_name in enumerate(types, start=1):
        ref = f"q.c{i}"
        if type_name == "bytea":
            exprs.append(
                f"CASE WHEN {ref} IS NULL THEN NULL "
                f"ELSE '<' || octet_length({ref})::text || ' bytes>' END"
            )
        else:
            exprs.append(f"left({ref}::text, {MAX_CELL_CHARS + 1})")
    aliases = ", ".join(f"c{i}" for i in range(1, len(types) + 1))
    return f"SELECT {', '.join(exprs)} FROM (\n{inner}\n) AS q({aliases}) LIMIT {int(limit)}"


async def _fetch_rows(cursor, types, *, max_rows: int) -> tuple[list[list[str]], bool, bool, int]:
    """Read up to `max_rows + 1` rows in small batches, serialising each batch at
    once, and stop early once the rendered size budget is spent. Returns
    `(rows, more_rows, size_budget_hit, truncated_cells)`."""
    ncols = max(1, len(types))
    batch = max(1, min(FETCH_BATCH_ROWS, FETCH_BATCH_BYTES // (ncols * (MAX_CELL_CHARS + 1) * 4)))
    rows: list[list[str]] = []
    cut_cells = 0
    total = 0
    size_hit = False
    while len(rows) < max_rows + 1:
        ask = min(batch, max_rows + 1 - len(rows))
        chunk = await cursor.fetch(ask)
        for record in chunk:
            values = list(record.values()) if hasattr(record, "values") else list(record)
            row: list[str] = []
            for value, type_name in zip(values, types):
                text, cut = cell_text(value, type_name)
                cut_cells += 1 if cut else 0
                row.append(text)
            total += sum(len(c.encode()) for c in row) + 3 * len(row)
            rows.append(row)
        if len(chunk) < ask:
            break
        if total >= MAX_OUTPUT_BYTES:
            size_hit = True
            break
    more = len(rows) > max_rows
    return rows[:max_rows], more, size_hit, cut_cells


async def run_statement(
    conn, sql: str, args: tuple = (), *, max_rows: int, secrets=(), bounded: bool = False
) -> QueryResult:
    """Prepare `sql` (one statement, extended protocol) and read up to `max_rows`
    rows through a cursor. Must be called inside `read_only_transaction`.

    With `bounded`, `sql` is first prepared exactly as written (that is what
    guarantees one statement and yields the column names and types WITHOUT
    executing it), then a wrapper that cuts every cell on the server is prepared
    and run instead; the user's statement is never executed on its own. A wrapper
    that will not prepare is an error, never a fallback to the unbounded path."""
    started = time.monotonic()
    try:
        stmt = await conn.prepare(sql)
        attrs = list(stmt.get_attributes())
        if not attrs:
            return QueryResult([], [], duration_ms=int((time.monotonic() - started) * 1000))
        names = [_frame_safe(str(getattr(a, "name", "?")), 80) for a in attrs]
        types = [getattr(getattr(a, "type", None), "name", None) for a in attrs]
        if bounded:
            wrapped = build_bounded_sql(sql, types, max_rows + 1)
            try:
                stmt = await conn.prepare(wrapped)
                ok = len(list(stmt.get_attributes())) == len(attrs)
            except Exception as exc:  # noqa: BLE001
                raise _bounding_error(exc, secrets) from None
            if not ok:
                raise _bounding_error(None, secrets)
            cursor = await stmt.cursor()
        else:
            cursor = await stmt.cursor(*args)
        rows, more, size_hit, cut_cells = await _fetch_rows(cursor, types, max_rows=max_rows)
    except DatabaseConnectionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _query_error(exc, secrets) from None
    return QueryResult(
        names, rows, more_rows=more, truncated_cells=cut_cells, truncated_size=size_hit,
        duration_ms=int((time.monotonic() - started) * 1000),
    )


def _bounding_error(exc: BaseException | None, secrets) -> DatabaseConnectionError:
    state = getattr(exc, "sqlstate", None) if exc is not None else None
    detail = f" (SQLSTATE {state})" if state else ""
    return DatabaseConnectionError(
        "This statement's result could not be size-bounded on the server" + detail + ". Select explicit "
        "columns, or cast any column of an unusual type to text, and try again.",
        kind="query",
    )


def _query_error(exc: BaseException, secrets) -> DatabaseConnectionError:
    if isinstance(exc, asyncpg.PostgresError):
        state = getattr(exc, "sqlstate", None) or "?"
        message = getattr(exc, "message", None) or str(exc)
        return DatabaseConnectionError(f"{state}: {scrub(message, *secrets)}"[:500], kind="query")
    return DatabaseConnectionError(scrub(pgconnect.describe_failure(exc), *secrets), kind="connect")


async def _close(conn) -> None:
    """Close, and make sure the socket is gone even if the close is cancelled or
    raises: `terminate()` runs in the `finally` unless the close completed."""
    closed = False
    try:
        await conn.close(timeout=5)
        closed = True
    except Exception:  # noqa: BLE001 - terminate below
        log.info("database connection close failed", exc_info=False)
    finally:
        if not closed:
            try:
                conn.terminate()
            except Exception:  # noqa: BLE001
                pass


# ── stored rows ──────────────────────────────────────────────────────────────
def slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (label or "").lower()).strip("-")[:32].strip("-")
    return slug or "db"


def provider_for(slug: str) -> str:
    return PROVIDER_PREFIX + slug


def is_database_provider(provider: str | None) -> bool:
    return bool(provider) and provider.startswith(PROVIDER_PREFIX)


def slug_of(conn: WorkspaceConnection) -> str:
    return conn.provider[len(PROVIDER_PREFIX):]


async def list_rows(db: AsyncSession, workspace_id: uuid.UUID) -> list[WorkspaceConnection]:
    rows = (
        await db.execute(
            select(WorkspaceConnection)
            .where(
                WorkspaceConnection.workspace_id == workspace_id,
                WorkspaceConnection.provider.startswith(PROVIDER_PREFIX, autoescape=True),
            )
            .order_by(WorkspaceConnection.created_at, WorkspaceConnection.provider)
        )
    ).scalars().all()
    return [r for r in rows if _SLUG_RE.match(slug_of(r))]


async def get_row(db: AsyncSession, workspace_id: uuid.UUID, slug: str) -> WorkspaceConnection | None:
    if not _SLUG_RE.match(slug or ""):
        return None
    return (
        await db.execute(
            select(WorkspaceConnection).where(
                WorkspaceConnection.workspace_id == workspace_id,
                WorkspaceConnection.provider == provider_for(slug),
            )
        )
    ).scalars().first()


def unique_slug(label: str, taken: set[str]) -> str:
    base = slugify(label)
    slug, n = base, 1
    while slug in taken:
        n += 1
        suffix = f"-{n}"
        slug = base[: 32 - len(suffix)].rstrip("-") + suffix
    return slug


def connection_view(row: WorkspaceConnection) -> dict:
    """What the API returns. Built from the non-secret metadata only."""
    meta = row.selected_resources or {}
    return {
        "slug": slug_of(row),
        "label": meta.get("label") or slug_of(row),
        "host": meta.get("host"),
        "port": meta.get("port"),
        "database": meta.get("database"),
        "user": meta.get("user"),
        "status": row.status,
        "last_tested_at": meta.get("last_tested_at"),
        "last_error": meta.get("last_error") if row.status != "active" else None,
        "warnings": list(meta.get("warnings") or []),
        "tls": meta.get("tls"),
        "created_at": row.created_at,
    }


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ── probing a connection (create + test) ─────────────────────────────────────
_ROLE_SQL = """
SELECT r.rolsuper,
       current_user::text,
       EXISTS (
         SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE c.relkind IN ('r', 'p')
           AND n.nspname <> ALL ($1::text[])
           AND left(n.nspname, 3) <> 'pg_'
           AND (has_table_privilege(c.oid, 'INSERT')
                OR has_table_privilege(c.oid, 'UPDATE')
                OR has_table_privilege(c.oid, 'DELETE'))
       ),
       r.rolbypassrls,
       r.rolcreaterole,
       r.rolcreatedb,
       EXISTS (
         SELECT 1 FROM pg_roles p
         WHERE p.rolname = ANY ($2::text[]) AND pg_has_role(current_user, p.oid, 'MEMBER')
       )
FROM pg_roles r WHERE r.rolname = current_user
"""

_ROLE_ADVICE = (
    "The database role's own permissions are the real boundary: Tret runs every query read-only, "
    "but a dedicated read-only role is recommended."
)
_ROLE_REFUSAL = (
    "This database role is too privileged to connect: it is a superuser, bypasses row-level "
    "security, can create roles or databases, or is (or belongs to) postgres, service_role or "
    "supabase_admin. Create a dedicated read-only role and connect with that. (An operator can "
    "allow privileged roles with TRET_DB_CONNECT_ALLOW_PRIVILEGED_ROLES.)"
)


async def probe(conn, parsed: ParsedDsn, tls_mode: str) -> list[str]:
    """`select 1` and the role check, in a read-only transaction. Returns warnings
    (advisory). A privileged role is REFUSED (`kind="policy"`) unless the operator
    allowed it."""
    settings = get_settings()
    timeout = int(settings.db_query_timeout_seconds)
    warnings: list[str] = []
    async with read_only_transaction(conn, timeout_seconds=timeout):
        await run_statement(conn, "select 1", max_rows=1, secrets=parsed.secrets())
        try:
            result = await run_statement(
                conn, _ROLE_SQL, (list(SYSTEM_SCHEMAS), list(PRIVILEGED_ROLE_NAMES)),
                max_rows=1, secrets=parsed.secrets(),
            )
        except DatabaseConnectionError:
            result = QueryResult([], [])  # an unreadable catalog is not a reason to refuse

    def flag(index: int) -> bool:
        return bool(result.rows) and len(result.rows[0]) > index and result.rows[0][index] == "true"

    is_super, can_write = flag(0), flag(2)
    elevated = flag(3) or flag(4) or flag(5) or flag(6)
    privileged_name = parsed.user.split(".")[0].lower() in PRIVILEGED_ROLE_NAMES
    if is_super or elevated or privileged_name:
        if not settings.db_connect_allow_privileged_roles:
            raise DatabaseConnectionError(_ROLE_REFUSAL, kind="policy")
        warnings.append(f"The connected role is a superuser or a privileged service role. {_ROLE_ADVICE}")
    elif can_write:
        warnings.append(f"The connected role has write privileges on some tables. {_ROLE_ADVICE}")
    if tls_mode == pgconnect.TLS_UNVERIFIED:
        warnings.append(
            "The server's TLS certificate could not be verified against a trusted CA. The "
            "connection is encrypted, but the server's identity was not confirmed. Ask the "
            "operator to configure the database CA certificate (TRET_DB_CONNECT_CA_PEM)."
        )
    return warnings


async def connect_and_probe(parsed: ParsedDsn) -> tuple[list[str], str]:
    """Open, probe, close. Returns `(warnings, tls_mode)` or raises
    `DatabaseConnectionError` (scrubbed). Touches no application-database
    session: callers must not hold a transaction open around it."""
    conn, tls_mode = await open_connection(parsed)
    try:
        try:
            warnings = await probe(conn, parsed, tls_mode)
        except DatabaseConnectionError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _query_error(exc, parsed.secrets()) from None
    finally:
        await _close(conn)
    return warnings, tls_mode


# ── create/test throttling (in-process; the deployment runs one worker) ─────
_probe_inflight: set[uuid.UUID] = set()
_probe_history: dict[uuid.UUID, deque] = {}
_clock = time.monotonic  # replaced by tests


@asynccontextmanager
async def probe_slot(workspace_id: uuid.UUID):
    """At most one create/test in flight per workspace and PROBE_RATE_LIMIT
    attempts per minute, so remote connects cannot pile up on the app, or hammer
    a database with a bad password from the shared egress address. Raises
    `DatabaseConnectionError(kind="busy" | "rate")` (HTTP 429). Hold it across the
    whole operation including the final commit."""
    now = _clock()
    history = _probe_history.setdefault(workspace_id, deque())
    while history and now - history[0] > PROBE_RATE_WINDOW_SECONDS:
        history.popleft()
    if workspace_id in _probe_inflight:
        raise DatabaseConnectionError(
            "Another database connection is already being added or tested for this workspace; "
            "wait for it to finish.",
            kind="busy",
        )
    if len(history) >= PROBE_RATE_LIMIT:
        raise DatabaseConnectionError(
            f"Too many database connection attempts; at most {PROBE_RATE_LIMIT} per minute per workspace. "
            "Try again shortly.",
            kind="rate",
        )
    history.append(now)
    _probe_inflight.add(workspace_id)
    try:
        yield
    finally:
        _probe_inflight.discard(workspace_id)


# ── gates ────────────────────────────────────────────────────────────────────
async def check_gate(db: AsyncSession, workspace_id: uuid.UUID, action: str) -> None:
    gate = await get_extension_registry().check_workspace_gate(db, workspace_id, action)
    if not gate.allowed:
        raise DatabaseConnectionError(
            gate.detail or "Database connections are not included in this workspace's plan.", kind="gate"
        )


def feature_enabled() -> bool:
    return bool(get_settings().db_connections)


# ── activity ─────────────────────────────────────────────────────────────────
async def _activity(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    slug: str,
    action: str,
    *,
    actor_user_id: uuid.UUID | None = None,
    actor_run_id: uuid.UUID | None = None,
    target: str | None = None,
    nbytes: int | None = None,
    detail: str | None = None,
) -> None:
    log.info(
        "database %s slug=%s run=%s target=%r detail=%s",
        action, slug, actor_run_id, (target or "")[:QUERY_ACTIVITY_TARGET_CHARS], detail,
    )
    try:
        await record_connection_activity(
            db,
            workspace_id=workspace_id,
            provider=provider_for(slug),
            action=action,
            actor_user_id=actor_user_id,
            actor_run_id=actor_run_id,
            target=(target or None) and target[:QUERY_ACTIVITY_TARGET_CHARS],
            bytes_count=nbytes,
            detail=detail,
        )
    except Exception:  # noqa: BLE001 - the audit row must not break the read it describes
        log.warning("could not record database activity row", exc_info=False)


record_activity = _activity


# ── API operations ───────────────────────────────────────────────────────────
def _cap_check(existing) -> None:
    if len(existing) >= MAX_CONNECTIONS_PER_WORKSPACE:
        raise DatabaseConnectionError(
            f"A workspace can have at most {MAX_CONNECTIONS_PER_WORKSPACE} database connections.",
            kind="limit",
        )


async def create_connection(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID | None, label: str, dsn: str
) -> WorkspaceConnection:
    """Validate policy, connect, probe, store, COMMIT. Raises
    `DatabaseConnectionError`.

    No application-database transaction is open while the remote database is
    contacted: the reads happen first and are ended, the network work runs with
    the session idle, and the row is written in a fresh short transaction. The
    per-workspace probe slot (one at a time, rate limited) is held through the
    commit, which also makes the connection cap race-safe within the process."""
    label = (label or "").strip()
    if not label or len(label) > 80:
        raise DatabaseConnectionError("A label of 1 to 80 characters is required.", kind="input")
    parsed = parse_dsn(dsn)
    check_host_policy(parsed.host, parsed.port)
    async with probe_slot(workspace_id):
        await check_gate(db, workspace_id, CONNECT_ACTION)
        _cap_check(await list_rows(db, workspace_id))
        await db.commit()  # end the read transaction before any remote I/O
        warnings, tls_mode = await connect_and_probe(parsed)
        existing = await list_rows(db, workspace_id)  # fresh transaction
        _cap_check(existing)
        slug = unique_slug(label, {slug_of(r) for r in existing})
        row = WorkspaceConnection(
            workspace_id=workspace_id,
            provider=provider_for(slug),
            account_label=label,
            encrypted_refresh_token=encrypt_dsn(dsn.strip()),
            granted_scopes=["db:read"],
            selected_resources={
                "kind": "postgres",
                "label": label,
                "host": parsed.host,
                "port": parsed.port,
                "database": parsed.database,
                "user": parsed.user,
                "tls": tls_mode,
                "created_by": str(user_id) if user_id else None,
                "last_tested_at": _now_iso(),
                "last_error": None,
                "warnings": warnings,
            },
            status="active",
            connected_by=user_id,
        )
        db.add(row)
        await db.flush()
        await _activity(
            db, workspace_id, slug, "connect", actor_user_id=user_id,
            target=f"{parsed.user}@{parsed.host}:{parsed.port}/{parsed.database}",
        )
        await db.commit()
    return row


async def retest_connection(
    db: AsyncSession, *, workspace_id: uuid.UUID, slug: str, user_id: uuid.UUID | None
) -> WorkspaceConnection | None:
    """Re-test a stored connection and update its status/metadata, COMMITTING.
    Returns None when there is no such connection. Same transaction discipline
    as `create_connection`: nothing is open during the remote probe."""
    row = await get_row(db, workspace_id, slug)
    if row is None:
        return None
    await check_gate(db, workspace_id, USE_ACTION)
    async with probe_slot(workspace_id):
        blob = row.encrypted_refresh_token
        await db.commit()  # end the read transaction before any remote I/O
        error: DatabaseConnectionError | None = None
        warnings: list[str] = []
        tls_mode = None
        try:
            parsed = parse_dsn(decrypt_dsn(blob))
            warnings, tls_mode = await connect_and_probe(parsed)
        except DatabaseConnectionError as exc:
            error = exc
        row = await get_row(db, workspace_id, slug)  # fresh transaction
        if row is None:  # removed while we were probing
            await db.commit()
            return None
        meta = dict(row.selected_resources or {})
        if error is not None:
            row.status = "error"
            row.error_detail = error.message
            meta["last_error"] = error.message
        else:
            row.status = "active"
            row.error_detail = None
            meta.update(last_error=None, warnings=warnings, tls=tls_mode, connect_failures=0)
        meta["last_tested_at"] = _now_iso()
        row.selected_resources = meta  # reassigned: JSONB change detection is by assignment
        await _activity(
            db, workspace_id, slug_of(row), "test", actor_user_id=user_id,
            detail="ok" if row.status == "active" else (row.error_detail or "failed")[:300],
        )
        await db.commit()
    return row


# ── tool-facing operations ───────────────────────────────────────────────────
async def usable_rows(db: AsyncSession, workspace_id: uuid.UUID) -> list[WorkspaceConnection]:
    return [r for r in await list_rows(db, workspace_id) if r.status == "active"]


async def unavailable_reason(db: AsyncSession, workspace_id: uuid.UUID | None) -> str | None:
    """Why this workspace cannot use database tools right now, or None."""
    if not feature_enabled():
        return "Database connections are switched off on this deployment (TRET_DB_CONNECTIONS)."
    if workspace_id is None:
        return "This run has no workspace, so database connections are unavailable."
    if master_mode() == MODE_OFF:
        return "Outbound access is disabled on this deployment (TRET_EGRESS)."
    if not await usable_rows(db, workspace_id):
        if await list_rows(db, workspace_id):  # rows exist, none active: an admin must act
            return NEEDS_ATTENTION
        return "No active database connection is configured for this workspace."
    try:
        await check_gate(db, workspace_id, USE_ACTION)
    except DatabaseConnectionError as exc:
        return exc.message
    return None


async def resolve_for_use(
    db: AsyncSession, workspace_id: uuid.UUID | None, slug: str | None
) -> tuple[WorkspaceConnection, ParsedDsn]:
    reason = await unavailable_reason(db, workspace_id)
    if reason:
        raise DatabaseConnectionError(reason, kind="unavailable")
    rows = await usable_rows(db, workspace_id)  # type: ignore[arg-type]
    slug = (slug or "").strip().lower() or None
    if slug is None:
        if len(rows) != 1:
            raise DatabaseConnectionError(
                "This workspace has several database connections; pass connection=<slug>. Available: "
                + ", ".join(slug_of(r) for r in rows),
                kind="input",
            )
        row = rows[0]
    else:
        matches = [r for r in rows if slug_of(r) == slug]
        if not matches:
            errored = [r for r in await list_rows(db, workspace_id) if slug_of(r) == slug]
            if errored:  # stored, but stopped: do not retry until an admin re-tests it
                raise DatabaseConnectionError(NEEDS_ATTENTION, kind="unavailable")
            raise DatabaseConnectionError(
                f"No active database connection {slug[:40]!r}. Available: " + ", ".join(slug_of(r) for r in rows),
                kind="input",
            )
        row = matches[0]
    return row, parse_dsn(decrypt_dsn(row.encrypted_refresh_token))


def _frame_safe(text: str | None, limit: int = 80) -> str:
    return re.sub(r"[\s\x00-\x1f\x7f\[\]]+", " ", text or "").strip()[:limit]


def _banner(row: WorkspaceConnection, what: str) -> str:
    # Provenance only. Deliberately says nothing about what to do with the content.
    return f"[DATABASE {_frame_safe(slug_of(row))} ({_frame_safe((row.selected_resources or {}).get('label'))}) — {what}; external data]"


def _clamp_rows(max_rows: int | None) -> int:
    cap = max(1, int(get_settings().db_query_max_rows))
    if max_rows is None:
        return cap
    return max(1, min(int(max_rows), cap))


async def _mark_connect_failure(db, row: WorkspaceConnection, exc: DatabaseConnectionError) -> None:
    """Stop a stale or broken connection from being retried in a loop (and getting
    the shared egress address banned by the database): an authentication failure
    flips the row to "error" at once, other connect failures after
    MAX_CONSECUTIVE_CONNECT_FAILURES in a row. Tools then refuse until an admin
    re-tests it successfully. Committed here so the flip survives a run that later
    rolls back."""
    if exc.kind not in ("auth", "connect"):
        return
    meta = dict(row.selected_resources or {})
    failures = int(meta.get("connect_failures") or 0) + 1
    meta["connect_failures"] = failures
    if exc.kind == "auth" or failures >= MAX_CONSECUTIVE_CONNECT_FAILURES:
        row.status = "error"
        row.error_detail = meta["last_error"] = exc.message[:300]
        meta["last_tested_at"] = _now_iso()
    row.selected_resources = meta
    try:
        await db.commit()
    except Exception:  # noqa: BLE001 - the failure being reported matters more
        log.warning("could not record database connection failure", exc_info=False)
        await db.rollback()


async def _with_connection(db, workspace_id, slug, work, *, action, target, actor_run_id):
    """Resolve the connection, open it, run `work(conn, parsed)`, always close.
    A server-side refusal (kind "query") is recorded as an activity row too."""
    row, parsed = await resolve_for_use(db, workspace_id, slug)
    try:
        conn, _tls = await open_connection(parsed)
    except DatabaseConnectionError as exc:
        await _mark_connect_failure(db, row, exc)
        if exc.kind == "auth":
            raise DatabaseConnectionError(NEEDS_ATTENTION, kind="unavailable") from None
        raise
    if (row.selected_resources or {}).get("connect_failures"):
        meta = dict(row.selected_resources or {})
        meta["connect_failures"] = 0
        row.selected_resources = meta
    try:
        try:
            return row, await work(conn, parsed)
        except DatabaseConnectionError as exc:
            err = exc
        except Exception as exc:  # noqa: BLE001
            err = _query_error(exc, parsed.secrets())
    finally:
        await _close(conn)
    if err.kind == "query":
        await _activity(
            db, workspace_id, slug_of(row), action, actor_run_id=actor_run_id, target=target,
            detail=f"error: {err.message[:200]}",
        )
    raise err


async def tool_query(
    db: AsyncSession, workspace_id, slug: str | None, sql: str, *, max_rows: int | None, actor_run_id
) -> str:
    ok, reason = classify_statement(sql)
    if not ok:
        raise DatabaseConnectionError(reason, kind="input")
    statement = sql.strip()
    for _ in range(3):  # `select 1;;` and friends
        trimmed = strip_terminator(statement).strip()
        if trimmed == statement:
            break
        statement = trimmed
    bounded = statement_kind(statement) in BOUNDED_KINDS
    cap = _clamp_rows(max_rows)
    timeout = int(get_settings().db_query_timeout_seconds)
    target = re.sub(r"\s+", " ", sql).strip()

    async def work(conn, parsed):
        async with read_only_transaction(conn, timeout_seconds=timeout):
            return await run_statement(
                conn, statement, max_rows=cap, secrets=parsed.secrets(), bounded=bounded
            )

    row, result = await _with_connection(
        db, workspace_id, slug, work, action="query", target=target, actor_run_id=actor_run_id
    )
    text = result.render(
        header=_banner(row, f"read-only query result, {len(result.rows)} row(s), {result.duration_ms} ms")
    )
    await _activity(
        db, workspace_id, slug_of(row), "query", actor_run_id=actor_run_id, target=target,
        nbytes=len(text.encode()),
        detail=f"rows={len(result.rows)} duration_ms={result.duration_ms} truncated={str(result.truncated).lower()}",
    )
    return text


_LIST_SQL = """
SELECT n.nspname::text, c.relname::text, c.relkind::text, c.reltuples::bigint
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'v', 'm', 'p', 'f')
  AND n.nspname <> ALL ($1::text[])
  AND ($2::boolean OR left(n.nspname, 3) <> 'pg_')
  AND has_table_privilege(c.oid, 'SELECT')
ORDER BY 1, 2
LIMIT {limit}
"""

_RELKIND = {"r": "table", "p": "table", "v": "view", "m": "materialized view", "f": "foreign table"}


async def tool_list_tables(
    db: AsyncSession, workspace_id, slug: str | None, *, include_system: bool, actor_run_id
) -> str:
    timeout = int(get_settings().db_query_timeout_seconds)
    excluded = [] if include_system else list(SYSTEM_SCHEMAS)

    async def work(conn, parsed):
        async with read_only_transaction(conn, timeout_seconds=timeout):
            return await run_statement(
                conn, _LIST_SQL.format(limit=TABLE_LIST_LIMIT + 1), (excluded, include_system),
                max_rows=TABLE_LIST_LIMIT, secrets=parsed.secrets(),
            )

    row, result = await _with_connection(
        db, workspace_id, slug, work, action="list_tables", target=None, actor_run_id=actor_run_id
    )
    lines: list[str] = []
    for schema, name, kind, tuples in result.rows:
        rows_text = "row count unknown" if tuples.startswith("-") else f"~{tuples} rows"
        lines.append(f"{_frame_safe(schema)}.{_frame_safe(name)} | {_RELKIND.get(kind, kind)} | {rows_text}")
    if not lines:
        body = "(no tables or views visible to this role" + ("" if include_system else " outside system schemas") + ")"
    else:
        body = "\n".join(lines)
    if result.more_rows:
        body += f"\n[truncated: only the first {TABLE_LIST_LIMIT} relations are listed]"
    text = _banner(row, "tables and views; row counts are planner estimates") + "\n" + body
    await _activity(
        db, workspace_id, slug_of(row), "list_tables", actor_run_id=actor_run_id,
        target="include_system" if include_system else None, nbytes=len(text.encode()),
        detail=f"relations={len(lines)}",
    )
    return text


_RESOLVE_SQL = """
SELECT c.oid::bigint, n.nspname::text, c.relname::text, c.relkind::text
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname = $1 AND ($2::text IS NULL OR n.nspname = $2)
  AND c.relkind IN ('r', 'v', 'm', 'p', 'f')
ORDER BY (n.nspname = 'public') DESC, n.nspname
"""
_COLUMNS_SQL = """
SELECT a.attname::text, format_type(a.atttypid, a.atttypmod), CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END
FROM pg_attribute a WHERE a.attrelid = $1::bigint::oid AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""
_PK_SQL = """
SELECT a.attname::text FROM pg_index i
JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY (i.indkey)
WHERE i.indrelid = $1::bigint::oid AND i.indisprimary ORDER BY a.attnum
"""
_FK_SQL = """
SELECT conname::text, pg_get_constraintdef(oid) FROM pg_constraint
WHERE conrelid = $1::bigint::oid AND contype = 'f' ORDER BY conname
"""


async def tool_describe_table(
    db: AsyncSession, workspace_id, slug: str | None, table: str, *, actor_run_id
) -> str:
    schema_in, name_in = parse_table_identifier(table)  # raises before any connection is opened
    timeout = int(get_settings().db_query_timeout_seconds)

    async def work(conn, parsed):
        secrets = parsed.secrets()
        async with read_only_transaction(conn, timeout_seconds=timeout):
            found = await run_statement(conn, _RESOLVE_SQL, (name_in, schema_in), max_rows=20, secrets=secrets)
            if not found.rows:
                raise DatabaseConnectionError(
                    f"No table or view named {(schema_in + '.' if schema_in else '') + name_in!r} is visible to this role. "
                    "Use list_database_tables to see what exists.",
                    kind="input",
                )
            if schema_in is None and len(found.rows) > 1 and found.rows[0][1] != "public":
                raise DatabaseConnectionError(
                    f"{name_in!r} exists in several schemas ("
                    + ", ".join(_frame_safe(r[1]) for r in found.rows)
                    + "); pass schema.table.",
                    kind="input",
                )
            oid, schema, name, kind = found.rows[0]
            cols = await run_statement(conn, _COLUMNS_SQL, (int(oid),), max_rows=500, secrets=secrets)
            pks = await run_statement(conn, _PK_SQL, (int(oid),), max_rows=50, secrets=secrets)
            fks = await run_statement(conn, _FK_SQL, (int(oid),), max_rows=50, secrets=secrets)
            sample: QueryResult | None = None
            sample_error = None
            try:
                sample = await run_statement(
                    conn,
                    f"SELECT * FROM {quote_ident(schema)}.{quote_ident(name)} LIMIT {SAMPLE_ROWS}",
                    max_rows=SAMPLE_ROWS, secrets=secrets, bounded=True,
                )
            except DatabaseConnectionError as exc:
                sample_error = exc.message
        return schema, name, kind, cols, pks, fks, sample, sample_error

    row, (schema, name, kind, cols, pks, fks, sample, sample_error) = await _with_connection(
        db, workspace_id, slug, work, action="describe", target=table[:200], actor_run_id=actor_run_id
    )
    out = [_banner(row, f"structure of {_frame_safe(schema)}.{_frame_safe(name)} ({_RELKIND.get(kind, kind)})")]
    out.append("columns (name | type | nullable):")
    out.extend("  " + " | ".join(_frame_safe(c, 120) for c in r) for r in cols.rows)
    out.append("primary key: " + (", ".join(_frame_safe(r[0]) for r in pks.rows) if pks.rows else "(none)"))
    if fks.rows:
        out.append("foreign keys:")
        out.extend(f"  {_frame_safe(r[0])}: {_frame_safe(r[1], 400)}" for r in fks.rows)
    else:
        out.append("foreign keys: (none)")
    if sample_error:
        out.append(f"sample rows: unavailable ({sample_error})")
    elif sample is not None:
        out.append(f"sample rows (up to {SAMPLE_ROWS}):")
        out.append(sample.render())
    text = "\n".join(out)
    await _activity(
        db, workspace_id, slug_of(row), "describe", actor_run_id=actor_run_id,
        target=f"{schema}.{name}", nbytes=len(text.encode()), detail=f"columns={len(cols.rows)}",
    )
    return text
