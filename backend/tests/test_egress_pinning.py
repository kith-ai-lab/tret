"""DNS rebinding: the connection goes to the address the guard vetted.

`check_url` resolves a host and refuses unless every address is public. Before
pinning, httpx then resolved the name a second time to connect, so a hostile
resolver could answer the vetting lookup with a public address and the connecting
lookup with 127.0.0.1 or 169.254.169.254. These tests play that resolver.

No test reaches the network. Most replace the TCP dial (the one place a socket is
opened) with a recorder that also answers like the hostile DNS server would; one
runs a real local TLS server to prove the certificate is still checked against
the hostname even though the connection goes to a literal IP.
"""
from __future__ import annotations

import asyncio
import dataclasses
import datetime
import ssl

import httpcore
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tret.config import Settings
from tret.net import guard
from tret.net.fetch import FetchError, fetch_page
from tret.net.policy import CLASS_RESEARCH, EgressDenied, policy_for

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"
HOSTILE = "127.0.0.1"
METADATA = "169.254.169.254"


def _research(**kw):
    return policy_for(CLASS_RESEARCH, Settings(egress="on", egress_research="on", **kw))


@pytest.fixture(autouse=True)
def _no_ambient_proxy(monkeypatch):
    # A proxy in the developer's environment would (correctly) switch pinning off.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
        monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv(var.lower(), raising=False)
    monkeypatch.delenv("TRET_EGRESS_PROXY", raising=False)
    from tret.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Stream(httpcore.AsyncNetworkStream):
    """A server that answers one HTTP/1.1 request, recording what it was sent."""

    def __init__(self, dial_log, address, answer):
        self._log, self._address, self._answer = dial_log, address, answer
        self._request = b""
        self._reply = b""
        self.sni = None

    async def write(self, buffer, timeout=None):
        self._request += buffer
        if b"\r\n\r\n" in self._request and not self._reply:
            head = self._request.decode("latin-1")
            host = next(
                line.split(":", 1)[1].strip()
                for line in head.split("\r\n")
                if line.lower().startswith("host:")
            )
            first = head.split("\r\n", 1)[0]
            self._log.append(
                {"address": self._address, "host_header": host, "sni": self.sni, "request": first}
            )
            self._reply = self._answer(host, first)

    async def read(self, max_bytes, timeout=None):
        data, self._reply = self._reply, b""
        return data

    async def aclose(self):
        pass

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        # What httpcore passes here is the name SNI is sent under, and the name
        # the certificate is verified against.
        self.sni = server_hostname
        return self

    def get_extra_info(self, info):
        return None


def _ok(body=b"ok"):
    return (
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nConnection: close\r\n"
        + f"Content-Length: {len(body)}\r\n\r\n".encode()
        + body
    )


@pytest.fixture()
def net(monkeypatch):
    """Hostile DNS + a fake network.

    `vetting` is what the guard's lookup returns, per host. `connecting` is what a
    *second* lookup of the same name would return (the rebind): the fake dial
    follows it for any name it is handed, as the OS resolver would, and a literal
    IP is dialled as itself. `dials` is every address actually connected to.
    """
    state = type("Net", (), {})()
    state.vetting = {}
    state.connecting = {}
    state.dials = []
    state.requests = []
    state.answer = lambda host, request_line: _ok()

    async def fake_resolve(host):
        return tuple(state.vetting[host])

    async def fake_connect(self, host, port, timeout=None, local_address=None, socket_options=None):
        address = state.connecting.get(host, host)
        state.dials.append(address)
        if address in (HOSTILE, METADATA):
            # Reaching here is the vulnerability: record it, then refuse so the
            # suite never opens a real socket.
            raise httpcore.ConnectError(f"REBOUND to {address}")
        return _Stream(state.requests, address, lambda h, r: state.answer(h, r))

    from httpcore._backends.anyio import AnyIOBackend

    monkeypatch.setattr(guard, "_resolve", fake_resolve)
    monkeypatch.setattr(AnyIOBackend, "connect_tcp", fake_connect)
    return state


async def test_a_rebinding_resolver_cannot_steer_the_connection_to_loopback(net):
    """Safe answer for the vetting lookup, 127.0.0.1 for the connecting one."""
    net.vetting["rebind.example"] = [PUBLIC_A]
    net.connecting["rebind.example"] = HOSTILE  # what httpx's own lookup would have got

    page = await fetch_page("https://rebind.example/doc", policy=_research())

    assert page.body == b"ok"
    assert HOSTILE not in net.dials
    assert net.dials == [PUBLIC_A]


async def test_a_rebinding_resolver_cannot_reach_the_metadata_endpoint(net):
    net.vetting["rebind.example"] = [PUBLIC_A]
    net.connecting["rebind.example"] = METADATA

    await fetch_page("https://rebind.example/", policy=_research())

    assert net.dials == [PUBLIC_A]


async def test_the_connection_uses_the_vetted_address_and_never_the_name(net):
    net.vetting["docs.example"] = [PUBLIC_A]
    await fetch_page("https://docs.example/a", policy=_research())
    assert net.dials == [PUBLIC_A]  # not "docs.example"


async def test_sni_and_host_header_are_the_hostname_not_the_ip(net):
    net.vetting["docs.example"] = [PUBLIC_A]
    await fetch_page("https://docs.example/a", policy=_research())
    (seen,) = net.requests
    assert seen["address"] == PUBLIC_A
    assert seen["sni"] == "docs.example"  # SNI and certificate hostname check
    assert seen["host_header"] == "docs.example"
    assert seen["request"] == "GET /a HTTP/1.1"


async def test_the_next_vetted_address_is_tried_when_the_first_does_not_connect(net, monkeypatch):
    net.vetting["multi.example"] = [PUBLIC_A, PUBLIC_B]
    real = net.requests

    from httpcore._backends.anyio import AnyIOBackend

    first = AnyIOBackend.connect_tcp

    async def flaky(self, host, port, *a, **k):
        if host == PUBLIC_A:
            net.dials.append(host)
            raise httpcore.ConnectError("down")
        return await first(self, host, port, *a, **k)

    monkeypatch.setattr(AnyIOBackend, "connect_tcp", flaky)
    await fetch_page("https://multi.example/", policy=_research())
    assert net.dials == [PUBLIC_A, PUBLIC_B]
    assert real[0]["address"] == PUBLIC_B


async def test_every_redirect_hop_is_resolved_and_vetted_again(net):
    net.vetting["first.example"] = [PUBLIC_A]
    net.vetting["second.example"] = [PUBLIC_B]
    net.connecting["second.example"] = HOSTILE

    def answer(host, request_line):
        if host == "first.example":
            return (
                b"HTTP/1.1 302 Found\r\nLocation: https://second.example/next\r\n"
                b"Content-Length: 0\r\nConnection: close\r\n\r\n"
            )
        return _ok(b"landed")

    net.answer = answer
    page = await fetch_page("https://first.example/start", policy=_research())

    assert page.body == b"landed" and page.redirects == ("https://first.example/start",)
    assert net.dials == [PUBLIC_A, PUBLIC_B]  # each hop dialled its own vetted address
    assert [r["host_header"] for r in net.requests] == ["first.example", "second.example"]
    assert [r["sni"] for r in net.requests] == ["first.example", "second.example"]


async def test_a_redirect_to_a_host_that_resolves_private_is_still_refused(net):
    net.vetting["first.example"] = [PUBLIC_A]
    net.vetting["internal.example"] = [HOSTILE]
    net.answer = lambda host, line: (
        b"HTTP/1.1 302 Found\r\nLocation: https://internal.example/\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    with pytest.raises(EgressDenied) as excinfo:
        await fetch_page("https://first.example/", policy=_research())
    assert excinfo.value.reason == "private_address"
    assert net.dials == [PUBLIC_A]


async def test_a_host_with_no_vetted_entry_is_refused_not_resolved(net):
    """Fail closed: the backend never falls back to a lookup of its own."""
    from tret.net.pinning import PinnedBackend

    backend = PinnedBackend()
    with pytest.raises(httpcore.ConnectError):
        await backend.connect_tcp("unvetted.example", 443)
    assert net.dials == []


async def test_an_explicit_egress_proxy_is_proxied_and_nothing_else_is(monkeypatch):
    """Behind TRET_EGRESS_PROXY the proxy resolves; pinning is not in the way."""
    import httpx

    from tret.config import get_settings
    from tret.net.client import build_client
    from tret.net.pinning import PinnedTransport

    monkeypatch.setenv("TRET_EGRESS_PROXY", "http://egress-proxy.internal:3128")
    get_settings.cache_clear()
    proxied = build_client(CLASS_RESEARCH, policy=_research())
    try:
        t = proxied._transport_for_url(httpx.URL("https://docs.example/"))
        assert not isinstance(t, PinnedTransport)
    finally:
        await proxied.aclose()


def _direct_is_pinned(url: str) -> bool:
    import httpx

    from tret.net.client import build_client
    from tret.net.pinning import PinnedTransport

    client = build_client(CLASS_RESEARCH, policy=_research())
    return isinstance(client._transport_for_url(httpx.URL(url)), PinnedTransport)


def test_no_proxy_alone_does_not_turn_pinning_off(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "localhost,internal.example")
    assert _direct_is_pinned("https://docs.example/")
    assert _direct_is_pinned("http://docs.example/")


def test_with_only_an_https_proxy_http_requests_go_direct_and_pinned(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    assert not _direct_is_pinned("https://docs.example/")  # the proxy resolves these
    assert _direct_is_pinned("http://docs.example/")


def test_a_no_proxy_host_is_pinned_even_when_a_proxy_is_set(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    monkeypatch.setenv("NO_PROXY", "bypass.example")
    assert _direct_is_pinned("https://bypass.example/")
    assert not _direct_is_pinned("https://other.example/")


async def test_an_idn_host_is_vetted_pinned_and_dialled_in_ascii_form(net):
    net.vetting["xn--bcher-kva.example"] = [PUBLIC_A]
    page = await fetch_page("https://b\u00fccher.example/x", policy=_research())
    assert page.body == b"ok"
    assert net.dials == [PUBLIC_A]
    assert net.requests[0]["host_header"] == "xn--bcher-kva.example"
    assert net.requests[0]["sni"] == "xn--bcher-kva.example"


# ── real TLS: the certificate is still checked against the hostname ────────────
def _certificate(tmp_path, name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / f"{name}.crt", tmp_path / f"{name}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture()
async def tls_server(tmp_path, monkeypatch):
    """A local HTTPS server whose certificate is for `served.example`, trusted by the client."""
    cert, key = _certificate(tmp_path, "served.example")
    seen = {"sni": [], "host": []}

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    context.sni_callback = lambda sock, name, ctx: seen["sni"].append(name)

    async def handle(reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        for line in head.decode().split("\r\n"):
            if line.lower().startswith("host:"):
                seen["host"].append(line.split(":", 1)[1].strip())
        writer.write(_ok(b"secure"))
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    port = server.sockets[0].getsockname()[1]
    yield port, seen
    server.close()
    await server.wait_closed()


def _loopback_policy():
    # Same research rules, except this test needs the vetted address to be loopback and the port to be ours.
    return dataclasses.replace(_research(), standard_ports_only=False)


async def test_tls_is_verified_against_the_hostname_while_connecting_to_the_ip(
    tls_server, monkeypatch
):
    port, seen = tls_server
    monkeypatch.setattr(guard, "_is_public", lambda address: True)  # let the test server stand in
    monkeypatch.setattr(guard, "_resolve", _const_resolver("served.example", "127.0.0.1"))
    _forbid_name_lookups(monkeypatch)

    page = await fetch_page(f"https://served.example:{port}/x", policy=_loopback_policy())

    assert page.body == b"secure"
    assert seen["sni"] == ["served.example"]
    assert seen["host"] == [f"served.example:{port}"]


async def test_a_certificate_for_another_name_is_rejected_even_at_the_vetted_ip(
    tls_server, monkeypatch
):
    """Connecting to an IP must not weaken certificate checking to 'any cert will do'."""
    port, _ = tls_server
    monkeypatch.setattr(guard, "_is_public", lambda address: True)
    monkeypatch.setattr(guard, "_resolve", _const_resolver("other.example", "127.0.0.1"))
    _forbid_name_lookups(monkeypatch)

    with pytest.raises(FetchError) as excinfo:
        await fetch_page(f"https://other.example:{port}/x", policy=_loopback_policy())
    assert "certificate" in str(excinfo.value).lower() or "ssl" in str(excinfo.value).lower()


def _const_resolver(host, address):
    async def resolve(name):
        assert name == host
        return (address,)

    return resolve


def _forbid_name_lookups(monkeypatch):
    """A second lookup of the hostname is exactly what must not happen."""
    import socket

    real = socket.getaddrinfo

    def guarded(host, *a, **k):
        if isinstance(host, str) and host.endswith(".example"):
            raise AssertionError(f"the name {host!r} was resolved a second time")
        return real(host, *a, **k)

    monkeypatch.setattr(socket, "getaddrinfo", guarded)
