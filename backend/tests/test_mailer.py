"""Invite email (tret/services/mailer.py): `off` is a true no-op, `resend`
goes through `tret.net.build_client` exactly like `api/oidc.py`'s own calls
do (respx mocks the one fixed host, the same way test_oidc_login.py mocks the
IdP), and `smtp` never touches a real socket here — `tret.net.smtp.
send_smtp_message` is monkeypatched, the same boundary
tests/test_egress_chokepoint.py draws around `smtplib` itself.

`send_invite_email` never raises regardless of mode; every test here checks
that promise as much as the happy path.
"""
from __future__ import annotations

import httpx
import pytest
import respx

from tret.config import get_settings
from tret.net.smtp import SmtpError
from tret.services import mailer

TO = "person@example.com"
WORKSPACE_NAME = "Climate Co"
INVITE_URL = "https://tret.example.test/invite/abc123"
INVITED_BY = "Ada"


@pytest.fixture(autouse=True)
def _clean_settings():
    yield
    get_settings.cache_clear()


# ── off (the default) ────────────────────────────────────────────────────────
async def test_off_mode_is_a_true_noop(monkeypatch):
    monkeypatch.delenv("TRET_EMAIL_MODE", raising=False)
    get_settings.cache_clear()
    assert get_settings().email_mode == "off"

    result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)
    assert result.sent is False
    assert result.detail is not None


# ── resend ────────────────────────────────────────────────────────────────────
def _configure_resend(monkeypatch, *, api_key="re_test_key", email_from="noreply@example.com"):
    monkeypatch.setenv("TRET_EMAIL_MODE", "resend")
    if api_key is not None:
        monkeypatch.setenv("TRET_RESEND_API_KEY", api_key)
    else:
        monkeypatch.delenv("TRET_RESEND_API_KEY", raising=False)
    if email_from is not None:
        monkeypatch.setenv("TRET_EMAIL_FROM", email_from)
    else:
        monkeypatch.delenv("TRET_EMAIL_FROM", raising=False)
    get_settings.cache_clear()


async def test_resend_mode_sends_through_the_egress_chokepoint(monkeypatch):
    _configure_resend(monkeypatch)
    with respx.mock(assert_all_called=True) as respx_mock:
        route = respx_mock.post("https://api.resend.com/emails").mock(
            return_value=httpx.Response(200, json={"id": "email-1"})
        )
        result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)

    assert result.sent is True
    assert result.detail is None
    request = route.calls[0].request
    assert request.headers["authorization"] == "Bearer re_test_key"
    body = httpx_json(request)
    assert body["from"] == "noreply@example.com"
    assert body["to"] == [TO]
    assert INVITE_URL in body["text"]
    assert WORKSPACE_NAME in body["subject"]


def httpx_json(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


async def test_resend_mode_without_credentials_does_not_attempt_a_call(monkeypatch):
    _configure_resend(monkeypatch, api_key=None)
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.post("https://api.resend.com/emails").mock(
            return_value=httpx.Response(200, json={"id": "should-not-be-called"})
        )
        result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)
    assert result.sent is False
    assert result.detail is not None


async def test_resend_mode_provider_failure_does_not_raise(monkeypatch):
    _configure_resend(monkeypatch)
    with respx.mock(assert_all_called=True) as respx_mock:
        respx_mock.post("https://api.resend.com/emails").mock(
            return_value=httpx.Response(500, json={"message": "boom"})
        )
        result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)
    assert result.sent is False
    assert result.detail is not None


async def test_resend_mode_with_egress_off_does_not_raise(monkeypatch):
    _configure_resend(monkeypatch)
    monkeypatch.setenv("TRET_EGRESS", "off")
    get_settings.cache_clear()
    with respx.mock(assert_all_called=False) as respx_mock:
        respx_mock.post("https://api.resend.com/emails").mock(
            return_value=httpx.Response(200, json={"id": "should-not-be-reached"})
        )
        result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)
    assert result.sent is False
    assert result.detail is not None


# ── smtp ──────────────────────────────────────────────────────────────────────
def _configure_smtp(monkeypatch, *, host="smtp.example.test", email_from="noreply@example.com"):
    monkeypatch.setenv("TRET_EMAIL_MODE", "smtp")
    monkeypatch.setenv("TRET_SMTP_HOST", host or "")
    if email_from is not None:
        monkeypatch.setenv("TRET_EMAIL_FROM", email_from)
    else:
        monkeypatch.delenv("TRET_EMAIL_FROM", raising=False)
    get_settings.cache_clear()


async def test_smtp_mode_sends_via_tret_net_smtp(monkeypatch):
    _configure_smtp(monkeypatch)
    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(mailer, "send_smtp_message", fake_send)
    result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)

    assert result.sent is True
    assert len(calls) == 1
    assert calls[0]["to_addr"] == TO
    assert calls[0]["from_addr"] == "noreply@example.com"
    assert INVITE_URL in calls[0]["body"]


async def test_smtp_mode_without_host_does_not_attempt_a_send(monkeypatch):
    _configure_smtp(monkeypatch, host=None)
    calls = []

    async def fake_send(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(mailer, "send_smtp_message", fake_send)
    result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)

    assert result.sent is False
    assert calls == []


async def test_smtp_mode_failure_does_not_raise(monkeypatch):
    _configure_smtp(monkeypatch)

    async def fake_send(**kwargs):
        raise SmtpError("relay refused the connection")

    monkeypatch.setattr(mailer, "send_smtp_message", fake_send)
    result = await mailer.send_invite_email(TO, WORKSPACE_NAME, INVITE_URL, INVITED_BY)

    assert result.sent is False
    assert "refused" in result.detail
