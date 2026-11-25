"""Connect to the address the guard vetted, not to whatever the name resolves to next.

`guard.check_url` resolves a host and refuses unless every address is public.
httpx then connects to the *name*, which resolves it a second time. A hostile
DNS server answers the first lookup with a public address and the second with
127.0.0.1 or 169.254.169.254 (rebinding, with a TTL of zero), and the vetting
was about a different address than the one connected to.

This closes it at the one place a socket is opened: an httpcore network backend.
The request hook (`client.py`) records the addresses `check_url` vetted for the
request's host, and `connect_tcp` dials one of THOSE addresses.

Why a network backend rather than rewriting the URL to the IP:

  * httpcore takes the TLS server name (SNI and certificate hostname checking),
    the `Host` header and the connection-pool key from the request's origin,
    which stays the hostname. Nothing is rewritten, so nothing can be forgotten
    on one path: certificate validation is still `check_hostname` against the
    real name, and a redirect's `Location` joins against the real URL.
  * Rewriting to `https://203.0.113.5/` would need `extensions={"sni_hostname"}`
    plus a hand-set `Host`, and httpx would then verify the certificate against
    the IP unless every caller remembered the extension. This fails open;
    the backend fails closed (below).

Properties:

  * **Fail closed.** A host with no recorded entry is refused, not resolved, so a
    code path that reaches the socket without passing the hook cannot fall back
    to a fresh lookup.
  * **Every hop.** Redirects are separate requests and each goes through the hook
    (`check_url`: fresh resolution, fresh vetting) before it connects.
  * **No pin when nothing was vetted.** Classes with `verify_addresses=none`
    (provider, catalog, a fixed Graph endpoint) record an empty tuple and connect
    by name as before. For them the allowlist is the control, not the address.
  * **Proxied requests are not pinned.** When TRET_EGRESS_PROXY (or a proxy in
    the environment, for the schemes and hosts it covers) is in force the TCP
    connection goes to the proxy and the proxy resolves the destination
    (`build_transports`). Everything not proxied, including hosts in NO_PROXY and
    schemes with no proxy, is still pinned. The proxied path is protected by the
    deployment, not by this module: the proxy is
    the egress boundary (docs/hardening.md section 9) and must itself refuse
    private ranges. `check_url` still vets the name locally before the request is
    handed to it.
"""
from __future__ import annotations

from typing import Iterable

import httpcore
import httpx
from httpcore._backends.auto import AutoBackend


def build_transports(
    backend: "PinnedBackend", explicit_proxy: str | None
) -> tuple[httpx.AsyncBaseTransport, dict[str, httpx.AsyncBaseTransport | None]]:
    """The default (direct, pinned) transport and the proxy mounts for one client.

    Every direct connection goes through `PinnedTransport`, whatever the
    environment says. Proxies are mounted explicitly because httpx ignores
    environment proxies once it is handed a transport:

      * `TRET_EGRESS_PROXY` sends everything to that proxy (`all://`).
      * Otherwise the proxies httpx would have read from the environment
        (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`) are mounted by
        scheme and host. A host `NO_PROXY` excludes maps to `None`, which httpx
        reads as "use the default transport", so it is pinned like any other.
        `NO_PROXY` alone therefore changes nothing about pinning.
    """
    direct = PinnedTransport(backend)
    if explicit_proxy:
        return direct, {"all://": httpx.AsyncHTTPTransport(proxy=explicit_proxy)}
    from httpx._utils import get_environment_proxies

    mounts: dict[str, httpx.AsyncBaseTransport | None] = {}
    for pattern, proxy_url in get_environment_proxies().items():
        mounts[pattern] = None if proxy_url is None else httpx.AsyncHTTPTransport(proxy=proxy_url)
    return direct, mounts


class PinnedBackend(httpcore.AsyncNetworkBackend):
    """Dials only addresses the request hook recorded for the host being dialled."""

    def __init__(self, inner: httpcore.AsyncNetworkBackend | None = None) -> None:
        self._inner = inner or AutoBackend()
        # host (as httpx spells it) -> vetted addresses; () means "nothing was
        # vetted for this host, connect by name".
        self._pins: dict[str, tuple[str, ...]] = {}

    def pin(self, host: str, addresses: Iterable[str]) -> None:
        self._pins[host.lower().rstrip(".")] = tuple(addresses)

    async def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        key = host.lower().rstrip(".")
        if key not in self._pins:
            raise httpcore.ConnectError(f"refusing to connect to {host!r}: no vetted address for it")
        addresses = self._pins[key]
        if not addresses:
            return await self._inner.connect_tcp(
                host, port, timeout=timeout, local_address=local_address, socket_options=socket_options
            )
        last: Exception | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    address, port, timeout=timeout, local_address=local_address,
                    socket_options=socket_options,
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as e:
                last = e
        raise last or httpcore.ConnectError(f"no vetted address for {host!r}")

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("unix sockets are not an egress path")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class PinnedTransport(httpx.AsyncHTTPTransport):
    """The stock httpx transport with its connection pool dialling through a `PinnedBackend`.

    Everything else (TLS context, limits, HTTP/1.1, env CA bundle) is httpx's
    own default, so certificate validation is unchanged: it is still done
    against the request's hostname.
    """

    def __init__(self, backend: PinnedBackend) -> None:
        super().__init__()
        self._pool._network_backend = backend
