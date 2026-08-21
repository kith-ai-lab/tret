"""The switches: what narrows what, and what a class is allowed to reach.

The properties worth defending here are all about direction. Every rule narrows;
nothing widens. The master switch narrows a class, a runtime override narrows
both, and the one exemption — `local`, so an air-gapped tret can still reach a
model server — is paid for by a *stricter* address check rather than by trust.
"""
from __future__ import annotations

import pytest

from tret.config import Settings
from tret.net import policy
from tret.net.policy import (
    CLASS_CATALOG,
    CLASS_LOCAL,
    CLASS_PROVIDER,
    CLASS_RESEARCH,
    CLASS_SEARCH,
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

    from tret.providers.openai_compat import KimiProvider, OpenRouterProvider

    pol = policy_for(CLASS_PROVIDER, _settings())
    for provider in (KimiProvider("k"), OpenRouterProvider("k")):
        host = urlsplit(provider._base_url).hostname
        assert host_allowed(host, pol), f"{host} is missing from PROVIDER_HOSTS"
    # The Anthropic SDK's default base URL is not read off an attribute here;
    # it is the one hostname tret hard-codes in both places.
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


def test_the_search_backend_no_longer_rides_the_research_allowlist():
    """This used to be `..._is_always_reachable_under_an_allowlist`: research's
    allowlist auto-added the configured search endpoint, since the operator had
    already chosen that host. Now the search backend has its own class
    (`search`, see the tests below) with its own trust rules — an
    operator-configured destination gets the same treatment `local` gets for
    TRET_LOCAL_BASE_URL, not research's SSRF checks. With the class split, the
    auto-add is not just redundant but wrong: it would make research's
    allowlist wider than what the operator actually typed into
    TRET_EGRESS_RESEARCH_ALLOW_HOSTS. A `fetch_url` call a *model* makes to that
    same host is still `research` traffic, governed by this allowlist alone,
    same as any other host — which is exactly what this test now checks."""
    s = _settings(
        egress="on",
        egress_research="on",
        egress_research_allow_hosts="example.com",
        search_provider="searxng",
        searxng_base_url="https://searx.internal.example",
    )
    assert not host_allowed("searx.internal.example", policy_for(CLASS_RESEARCH, s))


def test_a_disabled_class_refuses_to_build_a_client():
    from tret.net.client import build_client
    from tret.net.policy import EgressDenied

    with pytest.raises(EgressDenied) as excinfo:
        build_client(CLASS_RESEARCH, policy=policy_for(CLASS_RESEARCH, _settings()))
    assert "TRET_EGRESS_RESEARCH" in str(excinfo.value)


# ── the search class: the backend, not the model ─────────────────────────────
# `search` carries the web SEARCH BACKEND (SearXNG, Brave) — an
# operator-configured destination — not `fetch_url`'s model-chosen URL. It has
# no switch of its own; it rides TRET_EGRESS_RESEARCH, and its allowlist is
# exactly one host: whichever backend is actually configured.
def test_effective_mode_of_search_mirrors_research():
    """No TRET_EGRESS_SEARCH exists — the search backend is part of the
    research capability and is cut by the same switch, so the two must track
    exactly except for the replay rule below."""
    on = _settings(egress="on", egress_research="on")
    assert effective_mode(CLASS_SEARCH, on) == effective_mode(CLASS_RESEARCH, on) == MODE_ON

    off = _settings(egress="on", egress_research="off")
    assert effective_mode(CLASS_SEARCH, off) == effective_mode(CLASS_RESEARCH, off) == MODE_OFF

    master_off = _settings(egress="off", egress_research="on")
    assert effective_mode(CLASS_SEARCH, master_off) == effective_mode(CLASS_RESEARCH, master_off) == MODE_OFF


def test_replay_reads_as_off_for_search_even_though_research_stays_in_replay():
    """`replay` means "serve snapshots, fetch nothing new" for `fetch_url` —
    there is no snapshot cache for a search backend to serve from, so `replay`
    is not a meaningful state for `search` and must not be read as anything
    other than off. tret/engine/tools.py::web_search already refuses to search
    in replay mode; this is policy agreeing with that rather than contradicting
    it with a `mode` that claims replay is still in force."""
    s = _settings(egress="on", egress_research="replay")
    assert effective_mode(CLASS_RESEARCH, s) == MODE_REPLAY
    assert effective_mode(CLASS_SEARCH, s) == MODE_OFF


def test_a_replay_override_on_search_itself_also_collapses_to_off():
    """The runtime-override path, not just the env-configured one: the override
    merges into research's mode via narrower() BEFORE the replay->off collapse,
    so a `replay` override on "search" cannot leave `effective_mode` reporting
    a `replay` search class — the docstring says search can never be `replay`."""
    s = _settings(egress="on", egress_research="on")
    policy.set_runtime_override(CLASS_SEARCH, MODE_REPLAY)
    assert effective_mode(CLASS_SEARCH, s) == MODE_OFF


def test_a_runtime_override_on_search_itself_narrows_further():
    """Point 2 says search mirrors research's *switch* narrowing; this is the
    other kind of narrowing — an override set directly on "search" (not
    "research") composes on top via narrower(), same as any other class."""
    s = _settings(egress="on", egress_research="on")
    assert effective_mode(CLASS_SEARCH, s) == MODE_ON
    policy.set_runtime_override(CLASS_SEARCH, MODE_OFF)
    assert effective_mode(CLASS_SEARCH, s) == MODE_OFF
    # Narrowing "search" does not narrow "research" — they are different keys
    # in the override map, even though search's mode normally follows research.
    assert effective_mode(CLASS_RESEARCH, s) == MODE_ON


def test_search_policy_allows_only_the_configured_searxng_host():
    s = _settings(
        egress="on",
        egress_research="on",
        search_provider="searxng",
        searxng_base_url="http://searxng:8080",
    )
    pol = policy_for(CLASS_SEARCH, s)
    assert pol.mode == MODE_ON
    assert pol.allow_hosts == frozenset({"searxng"})
    assert type(pol.allow_hosts) is frozenset
    assert host_allowed("searxng", pol)
    # The allowlist is one host, not a category: a different host — even a
    # plausible-looking one — is refused. This is what distinguishes `search`
    # from `research`'s empty-allowlist-means-the-public-web.
    assert not host_allowed("evil.example.com", pol)
    assert not host_allowed("searxng.evil.example.com", pol)


def test_search_policy_allows_only_the_configured_brave_host():
    s = _settings(egress="on", egress_research="on", search_provider="brave", search_api_key="k")
    pol = policy_for(CLASS_SEARCH, s)
    assert pol.mode == MODE_ON
    assert pol.allow_hosts == frozenset({"api.search.brave.com"})


def test_search_policy_trusts_its_one_host_the_way_local_trusts_its_own():
    """Operator-configured, never model-chosen — https-only, standard-ports-
    only and address verification would all be defending against an attack
    this destination cannot be steered into (docs/hardening.md §9)."""
    from tret.net.policy import VERIFY_NONE

    s = _settings(
        egress="on",
        egress_research="on",
        search_provider="searxng",
        searxng_base_url="http://searxng:8080",
    )
    pol = policy_for(CLASS_SEARCH, s)
    assert pol.allow_http is True
    assert pol.standard_ports_only is False
    assert pol.verify_addresses == VERIFY_NONE


def test_search_mode_is_off_with_no_backend_configured():
    """host_allowed() itself is generic — empty allow_hosts reads as "any host"
    for every class, `search` included, same as `research`'s empty allowlist
    meaning the open web. What must NOT happen is a `search` class with nothing
    configured behaving like an open one, and that guarantee lives one level up:
    policy_for pins `mode` to MODE_OFF whenever no backend is configured, so
    `check_url`/`build_client` refuse with `class_disabled` before host_allowed
    is ever consulted (see test_no_search_provider_configured_means_the_class_is_off
    in test_egress_guard.py for that end-to-end check)."""
    s = _settings(egress="on", egress_research="on", search_provider="")
    pol = policy_for(CLASS_SEARCH, s)
    assert pol.mode == MODE_OFF
    assert pol.allow_hosts == frozenset()


def test_a_host_that_is_not_the_configured_backend_is_denied_under_search():
    s = _settings(
        egress="on",
        egress_research="on",
        search_provider="searxng",
        searxng_base_url="https://searx.internal.example",
    )
    pol = policy_for(CLASS_SEARCH, s)
    assert not host_allowed("api.search.brave.com", pol)
    assert not host_allowed("attacker.example.com", pol)


# ── availability follows egress ──────────────────────────────────────────────
def test_a_cloud_provider_with_a_key_is_unavailable_when_its_class_is_off(monkeypatch):
    """A key is not reachability. Offering a model the deployment cannot call
    means the router picks it and the run fails; filtering it means the router
    picks something that works."""
    from tret.config import get_settings
    from tret.providers.catalog import ProviderRegistry

    monkeypatch.setenv("TRET_EGRESS_PROVIDER", "off")
    get_settings.cache_clear()
    try:
        assert ProviderRegistry({"anthropic": "k"}).available_providers() == []
    finally:
        get_settings.cache_clear()


def test_an_air_gapped_deployment_still_routes_to_a_local_server(monkeypatch):
    from tret.config import get_settings
    from tret.providers.catalog import ProviderRegistry

    monkeypatch.setenv("TRET_EGRESS", "off")
    monkeypatch.setenv("TRET_LOCAL_BASE_URL", "http://localhost:11434/v1")
    get_settings.cache_clear()
    try:
        assert ProviderRegistry({"anthropic": "k"}).available_providers() == ["local"]
    finally:
        get_settings.cache_clear()
