"""`TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST`: capture_source may leave the research
allowlist; nothing else about it (or about fetch_url) does."""
# ruff: noqa: F811  (fixtures are imported, then named as test arguments)
from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager

import httpx
import pytest

from test_web_tools import _no_leaked_overrides, ctx, db, research  # noqa: F401
from tret.config import Settings, get_settings, production_config_problems
from tret.engine import tools as tools_module
from tret.engine.tools import ToolError, capture_source
from tret.net import guard
from tret.net.fetch import FetchedPage, fetch_page
from tret.net.fetch import fetch as fetch_module
from tret.net.guard import check_url
from tret.net.policy import (
    CLASS_RESEARCH,
    MODE_OFF,
    MODE_ON,
    MODE_REPLAY,
    VERIFY_PUBLIC,
    EgressDenied,
    capture_policy,
    policy_for,
)

URL = "https://issuer.example/report.pdf"


def _s(**kw) -> Settings:
    return Settings(egress="on", egress_research="on", **kw)


@pytest.fixture()
def env(monkeypatch):
    """Set TRET_* variables and rebuild the cached settings; restore afterwards."""

    def _set(**values: str):
        for name, value in values.items():
            monkeypatch.setenv(f"TRET_{name}", value)
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture()
def resolves(monkeypatch):
    def _install(*addresses: str):
        async def fake(host: str):
            return tuple(addresses)

        monkeypatch.setattr(guard, "_resolve", fake)

    return _install


# ── the policy ───────────────────────────────────────────────────────────────
def test_flag_off_is_research_with_only_the_byte_ceiling_changed():
    s = _s(egress_research_allow_hosts="example.com")
    base = policy_for(CLASS_RESEARCH, s)
    cap = capture_policy(s)
    assert cap.max_bytes == 62_914_560 != base.max_bytes
    from dataclasses import replace

    assert replace(cap, max_bytes=base.max_bytes) == base
    assert cap.allow_hosts == frozenset({"example.com"})


def test_flag_on_drops_only_the_host_allowlist():
    s = _s(egress_research_allow_hosts="example.com", egress_capture_any_public_host=True)
    base = policy_for(CLASS_RESEARCH, s)
    cap = capture_policy(s)
    assert not cap.allow_hosts
    assert cap.verify_addresses == VERIFY_PUBLIC
    assert cap.allow_http is False and cap.standard_ports_only is True
    assert cap.mode == base.mode == MODE_ON
    assert cap.max_bytes == s.egress_capture_max_bytes


def test_defaults():
    assert Settings.model_fields["egress_capture_any_public_host"].default is False
    assert Settings.model_fields["egress_capture_max_bytes"].default == 62_914_560


@pytest.mark.parametrize("research_mode", ["off", "replay"])
def test_mode_is_inherited_not_widened(research_mode):
    s = Settings(
        egress="on", egress_research=research_mode, egress_capture_any_public_host=True
    )
    assert capture_policy(s).mode == policy_for(CLASS_RESEARCH, s).mode
    assert capture_policy(s).mode in (MODE_OFF, MODE_REPLAY)


def test_env_spelling(env):
    env(EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    assert get_settings().egress_capture_any_public_host is True
    env(EGRESS_CAPTURE_ANY_PUBLIC_HOST="1")
    assert get_settings().egress_capture_any_public_host is True


# ── the per-URL decision ─────────────────────────────────────────────────────
async def test_other_host_passes_with_capture_policy_but_not_without(env, resolves):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_RESEARCH_ALLOW_HOSTS="example.com",
        EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    resolves("93.184.216.34")
    target = await check_url("https://cdn.other.net/x.pdf", CLASS_RESEARCH, capture_policy())
    assert target.host == "cdn.other.net"
    with pytest.raises(EgressDenied) as e:
        await check_url("https://cdn.other.net/x.pdf", CLASS_RESEARCH)
    assert e.value.reason == "host_not_allowed"


@pytest.mark.parametrize("address", ["10.0.0.5", "100.64.0.1"])
async def test_private_addresses_are_still_refused(env, resolves, address):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    resolves(address)
    with pytest.raises(EgressDenied) as e:
        await check_url("https://anywhere.example/x.pdf", CLASS_RESEARCH, capture_policy())
    assert e.value.reason == "private_address"


async def test_http_is_still_refused(env, resolves):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    resolves("93.184.216.34")
    with pytest.raises(EgressDenied) as e:
        await check_url("http://anywhere.example/x.pdf", CLASS_RESEARCH, capture_policy())
    assert e.value.reason == "scheme_not_allowed"


# ── fetch_page across a redirect, real check_url ─────────────────────────────
@pytest.fixture()
def redirecting(monkeypatch, resolves):
    resolves("93.184.216.34")
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if req.url.host == "issuer.example":
            return httpx.Response(302, headers={"location": "https://cdn.example/report.pdf"})
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7\nbody"
        )

    @asynccontextmanager
    async def fake_open_client(
        egress_class, *, timeout=None, headers=None, follow_redirects=False, policy=None
    ):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers=headers, timeout=timeout
        ) as client:
            yield client

    monkeypatch.setattr(fetch_module, "open_client", fake_open_client)
    return seen


async def test_a_redirect_to_a_cdn_succeeds_with_the_flag_on(env, redirecting):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_RESEARCH_ALLOW_HOSTS="issuer.example",
        EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    page = await fetch_page(
        URL, max_bytes=1_000_000, fail_on_truncate=True, capture=True, policy=capture_policy()
    )
    assert page.redirects == (URL,)
    assert page.url == "https://cdn.example/report.pdf"
    assert page.body.startswith(b"%PDF-")


async def test_a_redirect_off_the_allowlist_is_denied_with_the_flag_off(env, redirecting):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_RESEARCH_ALLOW_HOSTS="issuer.example")
    with pytest.raises(EgressDenied) as e:
        await fetch_page(
            URL, max_bytes=1_000_000, fail_on_truncate=True, capture=True, policy=capture_policy()
        )
    assert e.value.reason == "host_not_allowed"
    assert "cdn.example" in str(e.value)
    assert redirecting == [URL]


async def test_a_redirect_to_a_private_address_is_refused_with_the_flag_on(
    env, monkeypatch, redirecting
):
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")

    async def fake(host: str):
        return ("10.0.0.5",) if host == "cdn.example" else ("93.184.216.34",)

    monkeypatch.setattr(guard, "_resolve", fake)
    with pytest.raises(EgressDenied) as e:
        await fetch_page(
            URL, max_bytes=1_000_000, fail_on_truncate=True, capture=True, policy=capture_policy()
        )
    assert e.value.reason == "private_address"


# ── the tool ─────────────────────────────────────────────────────────────────
def _stub_fetch(monkeypatch):
    calls: list[dict] = []
    body = b"%PDF-1.7\nx"

    async def fake_fetch(target, **kw):
        calls.append(kw)
        return FetchedPage(
            url=target, requested_url=target, status_code=200, content_type="application/pdf",
            extension=".pdf", body=body, sha256=hashlib.sha256(body).hexdigest(),
            duration_ms=1, redirects=(), truncated=False, headers=(),
        )

    monkeypatch.setattr(tools_module, "fetch_page", fake_fetch)
    return calls


async def test_the_tool_passes_an_any_host_policy_when_the_flag_is_on(
    ctx, research, env, monkeypatch
):
    research(MODE_ON)
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_RESEARCH_ALLOW_HOSTS="example.com",
        EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    calls = _stub_fetch(monkeypatch)
    await capture_source(ctx, url="https://cdn.other.net/a.pdf")
    assert not calls[0]["policy"].allow_hosts
    assert calls[0]["policy"].max_bytes == 62_914_560


async def test_the_tool_keeps_the_allowlist_when_the_flag_is_off(ctx, research, env, monkeypatch):
    research(MODE_ON)
    env(EGRESS="on", EGRESS_RESEARCH="on", EGRESS_RESEARCH_ALLOW_HOSTS="example.com")
    calls = _stub_fetch(monkeypatch)
    await capture_source(ctx, url=URL)
    assert calls[0]["policy"].allow_hosts == frozenset({"example.com"})


async def test_research_off_still_refuses_with_the_flag_on(ctx, research, env, monkeypatch):
    research(MODE_OFF)
    env(EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    calls = _stub_fetch(monkeypatch)
    with pytest.raises(ToolError) as e:
        await capture_source(ctx, url=URL)
    assert "TRET_EGRESS_RESEARCH" in str(e.value)
    assert calls == []


async def test_replay_still_makes_no_request_with_the_flag_on(ctx, research, env, monkeypatch):
    research(MODE_REPLAY)
    env(EGRESS_CAPTURE_ANY_PUBLIC_HOST="true")
    calls = _stub_fetch(monkeypatch)
    with pytest.raises(ToolError) as e:
        await capture_source(ctx, url=URL)
    assert "replay mode" in str(e.value)
    assert calls == []


# ── boot note ────────────────────────────────────────────────────────────────
def test_boot_notes_the_flag_without_failing():
    base = {
        "environment": "production", "secret_key": "a-real-random-secret",
        "admin_password": "a-real-admin-password", "cookie_secure": True,
        "egress": "on", "egress_research": "on", "egress_research_allow_hosts": "example.com",
        "search_provider": "brave",
    }
    fatal, warnings = production_config_problems(
        Settings(**base, egress_capture_any_public_host=True)
    )
    assert fatal == []
    assert any("TRET_EGRESS_CAPTURE_ANY_PUBLIC_HOST" in w for w in warnings)
    _, quiet = production_config_problems(Settings(**base))
    assert not any("CAPTURE_ANY_PUBLIC_HOST" in w for w in quiet)
