"""The switches: what narrows what, and what a class is allowed to reach.

The properties worth defending here are all about direction. Every rule narrows;
nothing widens. The master switch narrows a class, a runtime override narrows
both, and the one exemption — `local`, so an air-gapped bench can still reach a
model server — is paid for by a *stricter* address check rather than by trust.
"""
from __future__ import annotations

import pytest

from bench.config import Settings
from bench.net import policy
from bench.net.policy import (
    CLASS_CATALOG,
    CLASS_LOCAL,
    CLASS_PROVIDER,
    CLASS_RESEARCH,
    MODE_OFF,
    MODE_ON,
    MODE_REPLAY,
    VERIFY_PRIVATE,
    effective_mode,
    host_allowed,
    narrower,
    policy_for,
)


@pytest.fixture(autouse=True)
def _no_leaked_overrides():
    policy.clear_all_runtime_overrides()
    yield
    policy.clear_all_runtime_overrides()


def _settings(**kw) -> Settings:
    return Settings(**kw)


def test_narrower_takes_the_narrowest():
    assert narrower(MODE_ON, MODE_OFF) == MODE_OFF
    assert narrower(MODE_ON, MODE_REPLAY) == MODE_REPLAY
    assert narrower(MODE_ON, MODE_ON) == MODE_ON
    # A mode nobody recognises ranks narrowest, so a typo can only ever close.
    assert narrower("nonsense", MODE_ON) == "nonsense"


def test_master_switch_narrows_every_internet_class():
    s = _settings(egress="off", egress_provider="on", egress_catalog="on", egress_research="on")
    assert effective_mode(CLASS_PROVIDER, s) == MODE_OFF
    assert effective_mode(CLASS_CATALOG, s) == MODE_OFF
    assert effective_mode(CLASS_RESEARCH, s) == MODE_OFF


def test_the_master_switch_does_not_reach_the_local_class():
    """Air-gapped means no internet, not no inference."""
    s = _settings(egress="off", egress_local="on", local_base_url="http://localhost:11434/v1")
    assert effective_mode(CLASS_LOCAL, s) == MODE_ON


def test_an_air_gapped_local_class_must_prove_the_host_is_private():
    """The exemption above is checked, not assumed — a 'local' URL on the public
    internet is refused rather than let through on the strength of its name."""
    s = _settings(egress="off", local_base_url="http://localhost:11434/v1")
    assert policy_for(CLASS_LOCAL, s).verify_addresses == VERIFY_PRIVATE
    # With the master switch on there is nothing to prove: the operator has
    # already said outbound traffic is fine.
    on = _settings(egress="on", local_base_url="http://localhost:11434/v1")
    assert policy_for(CLASS_LOCAL, on).verify_addresses != VERIFY_PRIVATE


def test_a_class_switch_narrows_on_its_own():
    s = _settings(egress="on", egress_research="off")
    assert effective_mode(CLASS_RESEARCH, s) == MODE_OFF


def test_research_is_off_by_default():
    assert effective_mode(CLASS_RESEARCH, _settings()) == MODE_OFF


def test_a_runtime_override_narrows():
    s = _settings(egress="on", egress_research="on")
    assert effective_mode(CLASS_RESEARCH, s) == MODE_ON
    policy.set_runtime_override(CLASS_RESEARCH, MODE_OFF)
    assert effective_mode(CLASS_RESEARCH, s) == MODE_OFF


def test_a_runtime_override_cannot_widen():
    """The whole reason the override is one-directional: an admin session must
    not be able to restore egress the environment took away."""
    s = _settings(egress="on", egress_research="off")
    policy.set_runtime_override(CLASS_RESEARCH, MODE_ON)
    assert effective_mode(CLASS_RESEARCH, s) == MODE_OFF


def test_clearing_an_override_returns_to_the_environment():
    s = _settings(egress="on", egress_research="on")
    policy.set_runtime_override(CLASS_RESEARCH, MODE_OFF)
    policy.clear_runtime_override(CLASS_RESEARCH)
    assert effective_mode(CLASS_RESEARCH, s) == MODE_ON


def test_replay_is_a_research_only_mode():
    """Nothing to replay for a model call, and reading it as `on` would widen."""
    s = _settings(egress="on", egress_provider="replay", egress_research="replay")
    assert effective_mode(CLASS_PROVIDER, s) == MODE_OFF
    assert effective_mode(CLASS_RESEARCH, s) == MODE_REPLAY


def test_every_provider_base_url_is_in_the_provider_allowlist():
    """PROVIDER_HOSTS is a literal (providers import policy, not the reverse), so
    this is what stops it drifting from the URLs the providers actually use."""
    from urllib.parse import urlsplit

    from bench.providers.openai_compat import KimiProvider, OpenRouterProvider

    pol = policy_for(CLASS_PROVIDER, _settings())
    for provider in (KimiProvider("k"), OpenRouterProvider("k")):
        host = urlsplit(provider._base_url).hostname
        assert host_allowed(host, pol), f"{host} is missing from PROVIDER_HOSTS"
    # The Anthropic SDK's default base URL is not read off an attribute here;
    # it is the one hostname bench hard-codes in both places.
    assert host_allowed("api.anthropic.com", pol)


def test_an_empty_research_allowlist_means_the_public_web_not_nothing():
    """Stated plainly rather than pretended otherwise: a general web search
    cannot run against an allowlist, so empty has to mean 'any public host'."""
    pol = policy_for(CLASS_RESEARCH, _settings(egress="on", egress_research="on"))
    assert pol.allow_hosts == frozenset()
    assert host_allowed("anything.example.com", pol)


def test_a_configured_research_allowlist_matches_subdomains_only():
    s = _settings(egress="on", egress_research="on", egress_research_allow_hosts="example.com, sec.gov")
    pol = policy_for(CLASS_RESEARCH, s)
    assert host_allowed("example.com", pol)
    assert host_allowed("www.example.com", pol)
    assert not host_allowed("notexample.com", pol)
    assert not host_allowed("example.com.evil.net", pol)


def test_the_search_backend_is_always_reachable_under_an_allowlist():
    """An operator who names a search backend has already chosen that host;
    making them list it twice only produces a confusing outage."""
    s = _settings(
        egress="on",
        egress_research="on",
        egress_research_allow_hosts="example.com",
        search_provider="searxng",
        searxng_base_url="https://searx.internal.example",
    )
    assert host_allowed("searx.internal.example", policy_for(CLASS_RESEARCH, s))


def test_a_disabled_class_refuses_to_build_a_client():
    from bench.net.client import build_client
    from bench.net.policy import EgressDenied

    with pytest.raises(EgressDenied) as excinfo:
        build_client(CLASS_RESEARCH, policy=policy_for(CLASS_RESEARCH, _settings()))
    assert "BENCH_EGRESS_RESEARCH" in str(excinfo.value)


# ── availability follows egress ──────────────────────────────────────────────
def test_a_cloud_provider_with_a_key_is_unavailable_when_its_class_is_off(monkeypatch):
    """A key is not reachability. Offering a model the deployment cannot call
    means the router picks it and the run fails; filtering it means the router
    picks something that works."""
    from bench.config import get_settings
    from bench.providers.catalog import ProviderRegistry

    monkeypatch.setenv("BENCH_EGRESS_PROVIDER", "off")
    get_settings.cache_clear()
    try:
        assert ProviderRegistry({"anthropic": "k"}).available_providers() == []
    finally:
        get_settings.cache_clear()


def test_an_air_gapped_deployment_still_routes_to_a_local_server(monkeypatch):
    from bench.config import get_settings
    from bench.providers.catalog import ProviderRegistry

    monkeypatch.setenv("BENCH_EGRESS", "off")
    monkeypatch.setenv("BENCH_LOCAL_BASE_URL", "http://localhost:11434/v1")
    get_settings.cache_clear()
    try:
        assert ProviderRegistry({"anthropic": "k"}).available_providers() == ["local"]
    finally:
        get_settings.cache_clear()
