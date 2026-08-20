"""Per-request checks: is *this* URL one this class may reach?

Every rule here exists because of one class, `research`, where the destination
comes from a model — and the model's input includes uploaded third-party
documents, which is the canonical prompt-injection surface in this product. A
`fetch_url` that will follow `http://169.254.169.254/latest/meta-data/` is a
credential exfiltration primitive handed to whoever wrote the PDF.

So for research:

  * https only, no userinfo in the URL, no non-standard port;
  * the host must survive the allowlist (empty allowlist = the public web);
  * the host is **resolved**, and every address it resolves to must be public.
    Checking the name would be checking the wrong thing — `evil.example.com`
    resolving to `127.0.0.1` is the whole trick.

Honest limit: resolution here and connection in httpx are two separate lookups,
so a DNS entry that changes between them (rebinding) is not closed by this code.
Closing it means pinning the resolved address into the connection, which means a
custom transport and hand-managed TLS SNI. tret does not claim that. The
deployment-level control — egress restricted to a proxy, or a network policy
that cannot see RFC1918 — is the boundary; this is the part that catches the
other 99% and makes the intent legible. docs/hardening.md §9.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from tret.net.policy import (
    MODE_OFF,
    MODE_REPLAY,
    VERIFY_NONE,
    VERIFY_PRIVATE,
    VERIFY_PUBLIC,
    ClassPolicy,
    EgressDenied,
    host_allowed,
    policy_for,
)

# Hostnames that name the deployment itself. Most are caught by resolving them,
# but a name that fails to resolve should still be refused rather than reaching
# the "DNS failure" path, and `.internal`/`.local` are conventions no public
# search result should ever hand us.
_INTERNAL_SUFFIXES = (".local", ".internal", ".localdomain", ".home.arpa")
_INTERNAL_NAMES = frozenset({"localhost", "localhost.localdomain"})

_STANDARD_PORTS = {"https": 443, "http": 80}


@dataclass(frozen=True)
class Target:
    """A URL that passed the checks, with what it resolved to."""

    url: str
    scheme: str
    host: str
    port: int
    path: str  # query deliberately dropped: see audit.py
    addresses: tuple[str, ...]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
        # IPv4-mapped IPv6 (::ffff:127.0.0.1) reports as global on some
        # versions unless unwrapped, and it is a plain loopback address.
        or (isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None
            and not _is_public(str(ip.ipv4_mapped)))
    )


async def _resolve(host: str) -> tuple[str, ...]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as e:
        raise EgressDenied("dns_failure", host, "research", f"cannot resolve {host!r}: {e}") from e
    return tuple(dict.fromkeys(info[4][0] for info in infos))


async def check_url(url: str, egress_class: str, policy: ClassPolicy | None = None) -> Target:
    """Refuse or permit one URL. Raises EgressDenied; returns what it resolved to."""
    pol = policy or policy_for(egress_class)
    if pol.mode == MODE_OFF:
        raise EgressDenied(
            "class_disabled",
            url,
            egress_class,
            f"egress class {egress_class!r} is off (TRET_EGRESS / TRET_EGRESS_{egress_class.upper()})",
        )
    if pol.mode == MODE_REPLAY:
        raise EgressDenied(
            "replay_only",
            url,
            egress_class,
            f"egress class {egress_class!r} is in replay mode: only pages already "
            "snapshotted by an earlier run can be read",
        )

    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    allowed_schemes = ("https", "http") if pol.allow_http else ("https",)
    if scheme not in allowed_schemes:
        raise EgressDenied(
            "scheme_not_allowed", url, egress_class, f"scheme {scheme or '(none)'!r} is not permitted"
        )
    if parts.username or parts.password:
        raise EgressDenied(
            "credentials_in_url", url, egress_class, "URLs carrying credentials are refused"
        )
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        raise EgressDenied("no_host", url, egress_class, "the URL names no host")

    try:
        port = parts.port or _STANDARD_PORTS[scheme]
    except ValueError as e:  # a port that is not a number
        raise EgressDenied("bad_port", url, egress_class, str(e)) from e
    if pol.standard_ports_only and port != _STANDARD_PORTS[scheme]:
        # A non-standard port on the research class is a port scan or an
        # internal service, never a public web page.
        raise EgressDenied(
            "port_not_allowed", url, egress_class, f"port {port} is not permitted for web research"
        )

    if not host_allowed(host, pol):
        raise EgressDenied(
            "host_not_allowed",
            url,
            egress_class,
            f"{host} is not in the allowlist for {egress_class} "
            f"({', '.join(sorted(pol.allow_hosts))})",
        )

    if pol.verify_addresses == VERIFY_NONE:
        return Target(url, scheme, host, port, parts.path or "/", ())

    internal_name = host in _INTERNAL_NAMES or host.endswith(_INTERNAL_SUFFIXES)
    if pol.verify_addresses == VERIFY_PUBLIC and internal_name:
        # Caught before resolution so a name that does not resolve is still
        # refused as what it is, rather than as a DNS failure.
        raise EgressDenied(
            "private_address", url, egress_class, f"{host} names the deployment's own network"
        )
    if pol.verify_addresses == VERIFY_PRIVATE and internal_name:
        return Target(url, scheme, host, port, parts.path or "/", ())

    addresses = await _resolve(host)
    if pol.verify_addresses == VERIFY_PUBLIC:
        offending = [a for a in addresses if not _is_public(a)]
        if offending or not addresses:
            raise EgressDenied(
                "private_address",
                url,
                egress_class,
                f"{host} resolves to non-public address(es): {', '.join(offending) or 'none'}",
            )
    else:  # VERIFY_PRIVATE
        offending = [a for a in addresses if _is_public(a)]
        if offending or not addresses:
            raise EgressDenied(
                "public_address",
                url,
                egress_class,
                f"TRET_EGRESS=off, so the local model server must be on a private "
                f"address, but {host} resolves to {', '.join(offending) or 'nothing'}",
            )
    return Target(url, scheme, host, port, parts.path or "/", addresses)
