"""The one door for outbound Postgres connections (read-only database tools).

`tests/test_egress_chokepoint.py` bans `socket`/`ssl` outside `tret/net/`, so the
code that vets a database host and opens the connection lives here;
`services/database_connections.py` owns policy (allowlist, ports), statements and
storage and calls `open_pinned` for the network part.

What `open_pinned` guarantees, on every call (never cached, never pooled):

  * the host is RESOLVED here and every address must be public (`guard._is_public`:
    private, loopback, link-local, CGNAT, unique-local IPv6, reserved, multicast
    and IPv4-mapped forms are all refused). One bad address refuses the host.
  * the connection is made to a vetted IP ADDRESS, never the hostname, so a DNS
    answer that changes between the check and the connect (rebinding) cannot
    redirect it. asyncpg verifies the certificate against whatever `host` it was
    given, which would be the IP, so `_PinnedNameContext` overrides the TLS server
    name back to the real hostname: SNI and certificate verification still use the
    name the user connected to.
  * TLS is always on (asyncpg's SSLRequest refusal is an error, never a plaintext
    fallback). Verification is `verify-full` against the system trust store plus
    certifi plus any operator CA (`ca_pem`, TRET_DB_CONNECT_CA_PEM). The
    encrypted-but-unverified retry happens ONLY when the caller allows it
    (`allow_unverified_tls`: operator opt-in, a Supabase host, no verify-ca/
    verify-full in the DSN) AND the failure is an untrusted issuer (OpenSSL
    verify codes 18/19/20/21), never a hostname mismatch, expiry or anything
    unclassifiable; the returned `tls` string says which happened ("verified" |
    "encrypted-unverified").
  * the whole connect (all addresses, both TLS passes) shares one deadline.

Errors raised here carry only fixed, scrubbed text: driver exceptions can embed
the DSN, host and user, so they are mapped to categories and never forwarded.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import ssl
from typing import Any

import asyncpg

from tret.net.guard import _is_public

log = logging.getLogger("tret.net.pgconnect")

CONNECT_TIMEOUT_SECONDS = 10.0
CONNECT_DEADLINE_SECONDS = 15.0  # total, across every address and TLS pass

TLS_VERIFIED = "verified"
TLS_UNVERIFIED = "encrypted-unverified"


class PgNetworkError(Exception):
    """A refusal or failure whose `.message` is safe to show a user or a model."""

    def __init__(self, message: str, *, kind: str = "connect"):
        super().__init__(message)
        self.message = message
        # "policy" (refused before connecting) | "connect" | "auth" (the server
        # rejected the credentials) | "tls" (certificate not trusted)
        self.kind = kind


# Seams the tests replace: the resolver and the driver's connect.
async def _resolve(host: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise PgNetworkError("cannot resolve the database host") from exc
    return tuple(dict.fromkeys(info[4][0] for info in infos))


_asyncpg_connect = asyncpg.connect


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host.strip("[]")))
    except ValueError:
        return None


async def vet_host(host: str, *, allow_literal_ip: bool) -> tuple[str, ...]:
    """Resolve `host` and return its addresses, raising `PgNetworkError(kind=
    "policy")` unless every one is public. A literal IP is refused unless
    `allow_literal_ip`, and even then must be public."""
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        raise PgNetworkError("the connection names no host", kind="policy")
    literal = _literal_ip(host)
    if literal is not None:
        if not allow_literal_ip:
            raise PgNetworkError(
                "IP-address hosts are not allowed; use the database's hostname", kind="policy"
            )
        addresses: tuple[str, ...] = (literal,)
    else:
        addresses = await _resolve(host)
    if not addresses:
        raise PgNetworkError("the database host did not resolve to any address", kind="policy")
    if any(not _is_public(a) for a in addresses):
        # The offending address is deliberately not echoed: this message reaches
        # an LLM, and "which internal address does this name map to" is recon.
        raise PgNetworkError(
            "the database host resolves to a non-public address (private, loopback, "
            "link-local or otherwise internal); refusing to connect",
            kind="policy",
        )
    return addresses


class _PinnedNameContext(ssl.SSLContext):
    """An SSLContext that handshakes under `pinned_server_hostname` whatever name
    the caller passes. asyncpg hands `server_hostname=<host it connected to>` to
    the TLS layer; we connect to an IP, so without this the certificate would be
    checked against the IP."""

    pinned_server_hostname: str | None = None

    def wrap_bio(self, incoming, outgoing, server_side=False, server_hostname=None, session=None):
        if not server_side and self.pinned_server_hostname:
            server_hostname = self.pinned_server_hostname
        return super().wrap_bio(
            incoming, outgoing, server_side=server_side, server_hostname=server_hostname, session=session
        )

    def wrap_socket(
        self, sock, server_side=False, do_handshake_on_connect=True, suppress_ragged_eofs=True,
        server_hostname=None, session=None,
    ):
        if not server_side and self.pinned_server_hostname:
            server_hostname = self.pinned_server_hostname
        return super().wrap_socket(
            sock,
            server_side=server_side,
            do_handshake_on_connect=do_handshake_on_connect,
            suppress_ragged_eofs=suppress_ragged_eofs,
            server_hostname=server_hostname,
            session=session,
        )


CA_ENV_NAME = "TRET_DB_CONNECT_CA_PEM"
# OpenSSL X509_V_ERR_* codes meaning "the issuer is not in our trust store":
# 18 depth-zero self-signed, 19 self-signed in chain, 20 unable to get local
# issuer, 21 unable to verify the first certificate. Hostname (62) and IP (64)
# mismatches, expiry (10) and everything else are NOT in this set.
_UNTRUSTED_ISSUER_CODES = frozenset({18, 19, 20, 21})


def _normalise_pem(ca_pem: str | None) -> str | None:
    text = (ca_pem or "").strip()
    if not text:
        return None
    return text.replace("\\n", "\n")  # an env value often carries literal \n


def build_tls_context(hostname: str, *, verify: bool, ca_pem: str | None = None) -> ssl.SSLContext:
    ctx = _PinnedNameContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.pinned_server_hostname = hostname
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if verify:
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.load_default_certs()
        try:  # certifi is already a dependency of httpx; covers slim images without an OS bundle
            import certifi

            ctx.load_verify_locations(certifi.where())
        except Exception:  # noqa: BLE001 - best effort, system store still loaded
            pass
        pem = _normalise_pem(ca_pem)
        if pem:
            before = ctx.cert_store_stats().get("x509", 0)
            try:
                ctx.load_verify_locations(cadata=pem)
            except (ssl.SSLError, ValueError, TypeError):
                raise _bad_ca() from None
            if ctx.cert_store_stats().get("x509", 0) <= before:
                raise _bad_ca()
    else:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _bad_ca() -> PgNetworkError:
    # Never echoes the configured value.
    return PgNetworkError(
        f"The database CA certificate configured on this deployment ({CA_ENV_NAME}) is not valid "
        "PEM; the operator must fix it.",
        kind="policy",
    )


def _cert_error(exc: BaseException | None) -> ssl.SSLCertVerificationError | None:
    seen = 0
    while exc is not None and seen < 5:
        if isinstance(exc, ssl.SSLCertVerificationError):
            return exc
        exc = exc.__cause__ or exc.__context__  # type: ignore[assignment]
        seen += 1
    return None


def _is_cert_error(exc: BaseException | None) -> bool:
    return _cert_error(exc) is not None


def is_untrusted_issuer_error(exc: BaseException | None) -> bool:
    """True only when OpenSSL's own verify code says the certificate chain does
    not lead to a trusted root. A hostname/IP mismatch, an expired certificate or
    an error that carries no verify code (cannot be told apart) is False."""
    err = _cert_error(exc)
    return err is not None and getattr(err, "verify_code", None) in _UNTRUSTED_ISSUER_CODES


def _is_auth_error(exc: BaseException | None) -> bool:
    return isinstance(
        exc, (asyncpg.InvalidPasswordError, asyncpg.InvalidAuthorizationSpecificationError)
    ) or getattr(exc, "sqlstate", None) in ("28P01", "28000")


def describe_failure(exc: BaseException) -> str:
    """A short, fixed-vocabulary description of a connect/query failure. Never
    forwards `str(exc)` for network or TLS errors (it can name the host, user or
    DSN). Postgres server errors contribute only their SQLSTATE and, for the
    authentication family, a fixed phrase."""
    if isinstance(exc, PgNetworkError):
        return exc.message
    if _is_cert_error(exc):
        return "TLS certificate verification failed"
    if isinstance(exc, ssl.SSLError):
        return "TLS handshake failed"
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "connection timed out"
    if isinstance(exc, asyncpg.InvalidPasswordError):
        return "authentication failed (check the user and password)"
    if isinstance(exc, asyncpg.InvalidAuthorizationSpecificationError):
        return "the server refused the login"
    if isinstance(exc, asyncpg.InvalidCatalogNameError):
        return "the database does not exist"
    if isinstance(exc, asyncpg.PostgresError):
        return f"the server refused the connection (SQLSTATE {getattr(exc, 'sqlstate', None) or '?'})"
    if isinstance(exc, ConnectionError):
        # asyncpg raises ConnectionError("PostgreSQL server rejected SSL upgrade"):
        # that is the "never plaintext" refusal, not an unreachable host.
        if "ssl" in str(exc).lower():
            return "the server does not support TLS, which is required"
        return "could not reach the database server"
    if isinstance(exc, OSError):
        return "could not reach the database server"
    return "could not connect to the database"


async def _connect_one(
    ip: str, port: int, hostname: str, user: str, password: str, database: str, *,
    verify: bool, ca_pem: str | None, timeout: float,
) -> Any:
    return await _asyncpg_connect(
        host=ip,
        port=port,
        user=user,
        password=password,
        database=database,
        ssl=build_tls_context(hostname, verify=verify, ca_pem=ca_pem),
        timeout=timeout,
        command_timeout=CONNECT_TIMEOUT_SECONDS * 3,
        # A fresh connection per tool call and often a transaction pooler on
        # the other end (Supabase :6543): no prepared-statement cache.
        statement_cache_size=0,
    )


async def open_pinned(
    *,
    host: str,
    port: int,
    user: str,
    password: str,
    database: str,
    allow_literal_ip: bool = False,
    allow_unverified_tls: bool = False,
    ca_pem: str | None = None,
) -> tuple[Any, str]:
    """Vet `host`, connect to one of its addresses over verified TLS (or, when
    `allow_unverified_tls`, encrypted-unverified after an untrusted-issuer
    failure) and return `(connection, tls_mode)`. Raises `PgNetworkError` with a
    scrubbed message; the caller closes the connection. All attempts share
    `CONNECT_DEADLINE_SECONDS`; an authentication failure stops immediately
    (retrying other addresses would only multiply bad logins)."""
    hostname = host.strip().lower().rstrip(".")
    addresses = await vet_host(hostname, allow_literal_ip=allow_literal_ip)
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CONNECT_DEADLINE_SECONDS
    last: BaseException | None = None
    for verify in (True, False) if allow_unverified_tls else (True,):
        untrusted = False
        for ip in addresses:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise PgNetworkError("connection timed out")
            try:
                conn = await _connect_one(
                    ip, port, hostname, user, password, database,
                    verify=verify, ca_pem=ca_pem, timeout=min(CONNECT_TIMEOUT_SECONDS, remaining),
                )
                return conn, (TLS_VERIFIED if verify else TLS_UNVERIFIED)
            except PgNetworkError:
                raise
            except Exception as exc:  # noqa: BLE001 - mapped to a scrubbed message below
                last = exc
                if _is_auth_error(exc):
                    raise PgNetworkError(describe_failure(exc), kind="auth") from None
                if verify and _is_cert_error(exc):
                    untrusted = is_untrusted_issuer_error(exc)
                    break  # same host, same certificate chain: no point trying other IPs
        if not (verify and untrusted and allow_unverified_tls):
            break
        log.info("database host presented an untrusted certificate chain; retrying encrypted-unverified")
    if last is not None and is_untrusted_issuer_error(last):
        raise PgNetworkError(
            "TLS certificate verification failed: the server's certificate is not issued by a trusted "
            f"authority. The deployment operator needs to configure the database CA certificate "
            f"({CA_ENV_NAME}).",
            kind="tls",
        )
    if last is not None and _is_cert_error(last):
        raise PgNetworkError(
            "TLS certificate verification failed: the certificate does not match the host name or is "
            "otherwise invalid.",
            kind="tls",
        )
    raise PgNetworkError(describe_failure(last) if last else "could not connect to the database")
