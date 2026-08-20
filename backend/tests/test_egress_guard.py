"""The per-URL checks — the SSRF surface.

`fetch_url` takes a URL a *model* chose, and a model's context includes uploaded
third-party documents. So this suite is written from the attacker's side: each
test is a way to make tret connect to something on its own network, and the
assertion is that it does not.

DNS is stubbed throughout. These tests are about the decision, not about the
resolver, and a test that reaches the network is a test that fails on a train.
"""
from __future__ import annotations

import pytest

from tret.config import Settings
from tret.net import guard
from tret.net.guard import check_url
from tret.net.policy import (
    CLASS_LOCAL,
    CLASS_PROVIDER,
    CLASS_RESEARCH,
    EgressDenied,
    policy_for,
)


@pytest.fixture()
def resolves(monkeypatch):
    """Point every hostname at addresses of our choosing."""

    def _install(*addresses: str):
        async def fake(host: str):
            return tuple(addresses)

        monkeypatch.setattr(guard, "_resolve", fake)

    return _install


def _research(**kw):
    return policy_for(
        CLASS_RESEARCH, Settings(egress="on", egress_research="on", **kw)
    )


async def _denied(url: str, egress_class=CLASS_RESEARCH, policy=None) -> EgressDenied:
    with pytest.raises(EgressDenied) as excinfo:
        await check_url(url, egress_class, policy or _research())
    return excinfo.value


async def test_a_public_https_url_is_allowed(resolves):
    resolves("93.184.216.34")
    target = await check_url("https://example.com/a/b?q=secret", CLASS_RESEARCH, _research())
    assert target.host == "example.com"
    assert target.path == "/a/b"
    assert "secret" not in target.path  # the query never travels into the audit row


async def test_a_hostname_resolving_to_loopback_is_refused(resolves):
    """The attack the name check alone would miss: the host looks public."""
    resolves("127.0.0.1")
    assert (await _denied("https://totally-legit.example.com/")).reason == "private_address"


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "192.168.1.10",
        "172.16.4.4",
        "169.254.169.254",  # the cloud metadata endpoint
        "::1",
        "fe80::1",
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "0.0.0.0",
    ],
)
async def test_every_non_public_address_family_is_refused(resolves, address):
    resolves(address)
    assert (await _denied("https://example.com/")).reason == "private_address"


async def test_one_private_address_among_several_is_enough_to_refuse(resolves):
    """A host with a public A record and a private AAAA record still loses:
    which one httpx picks is not something this decision should depend on."""
    resolves("93.184.216.34", "10.1.2.3")
    assert (await _denied("https://example.com/")).reason == "private_address"


async def test_localhost_is_refused_without_needing_dns(monkeypatch):
    async def explode(host):
        raise AssertionError("should have been refused before resolving")

    monkeypatch.setattr(guard, "_resolve", explode)
    assert (await _denied("https://localhost/")).reason == "private_address"
    assert (await _denied("https://db.internal/")).reason == "private_address"


async def test_plain_http_is_refused_for_research(resolves):
    resolves("93.184.216.34")
    assert (await _denied("http://example.com/")).reason == "scheme_not_allowed"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.com/", "ftp://example.com/"])
async def test_non_web_schemes_are_refused(url):
    assert (await _denied(url)).reason == "scheme_not_allowed"


async def test_credentials_in_a_url_are_refused():
    assert (await _denied("https://user:pw@example.com/")).reason == "credentials_in_url"


async def test_a_non_standard_port_is_refused_for_research(resolves):
    """Port 8080 on a public host is a service, not a web page — and the same
    request shape is how you scan one."""
    resolves("93.184.216.34")
    assert (await _denied("https://example.com:8080/")).reason == "port_not_allowed"


async def test_a_host_outside_a_configured_allowlist_is_refused(resolves):
    resolves("93.184.216.34")
    pol = _research(egress_research_allow_hosts="sec.gov")
    denied = await _denied("https://example.com/", policy=pol)
    assert denied.reason == "host_not_allowed"
    assert (await check_url("https://www.sec.gov/x", CLASS_RESEARCH, pol)).host == "www.sec.gov"


async def test_a_class_that_is_off_refuses_before_anything_else():
    pol = policy_for(CLASS_RESEARCH, Settings())  # research ships off
    assert (await _denied("https://example.com/", policy=pol)).reason == "class_disabled"


async def test_replay_mode_refuses_new_requests_with_its_own_reason():
    pol = policy_for(CLASS_RESEARCH, Settings(egress="on", egress_research="replay"))
    assert (await _denied("https://example.com/", policy=pol)).reason == "replay_only"


async def test_a_provider_host_is_allowed_without_resolving(monkeypatch):
    """The provider allowlist already pins the destination, so a DNS lookup per
    model call would buy nothing. If this starts resolving, that is a
    regression in the hot path."""

    async def explode(host):
        raise AssertionError("provider calls must not pay for a resolve")

    monkeypatch.setattr(guard, "_resolve", explode)
    pol = policy_for(CLASS_PROVIDER, Settings(egress="on"))
    assert (await check_url("https://api.anthropic.com/v1/messages", CLASS_PROVIDER, pol)).host


async def test_a_provider_host_nobody_configured_is_refused():
    pol = policy_for(CLASS_PROVIDER, Settings(egress="on"))
    with pytest.raises(EgressDenied) as excinfo:
        await check_url("https://evil.example.com/v1/messages", CLASS_PROVIDER, pol)
    assert excinfo.value.reason == "host_not_allowed"


async def test_an_air_gapped_local_server_must_be_on_a_private_address(resolves):
    settings = Settings(egress="off", local_base_url="http://ollama.example.com:11434/v1")
    pol = policy_for(CLASS_LOCAL, settings)
    resolves("93.184.216.34")
    with pytest.raises(EgressDenied) as excinfo:
        await check_url("http://ollama.example.com:11434/v1/models", CLASS_LOCAL, pol)
    assert excinfo.value.reason == "public_address"

    resolves("192.168.1.50")
    target = await check_url("http://ollama.example.com:11434/v1/models", CLASS_LOCAL, pol)
    assert target.port == 11434  # local servers live on odd ports; that is fine here
