"""Invite email: transactional mail at launch, copy-link always available.

`TRET_EMAIL_MODE` (config.py) picks how — `off` (default, every self-hosted
deployment) | `resend` | `smtp` — and `send_invite_email` never raises and
never blocks invite creation on the outcome: `api/workspaces.py`'s
`POST /{workspace_id}/invites` always returns the invite link itself
regardless of what this module does, so a misconfigured or down mail
provider degrades to copy-link, not a 500.

**Egress.** The `resend` path is `api/oidc.py`'s pattern verbatim: never bare
`httpx`, always `tret.net.build_client`, with a `ClassPolicy` scoped to
Resend's one fixed host and gated by the master `TRET_EGRESS` switch. Like
oidc.py's `_EGRESS_CLASS = "oidc"`, `"email"` here is an audit-log label only
— not one of the five operator-facing classes `GET /api/settings/egress`
reports, because there is nothing to switch independently of "is this
deployment on the internet at all" (an operator-configured fixed destination,
never a model- or document-chosen one).

The `smtp` path has no HTTP client to route through that same chokepoint at
all — SMTP is a plain-socket protocol — so the one `smtplib` call it needs
lives in `tret.net.smtp`, the one module outside this file allowed to import
it, and is gated by the same master switch there.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from tret.config import get_settings
from tret.net import EgressDenied, build_client
from tret.net.policy import MODE_OFF, MODE_ON, VERIFY_NONE, ClassPolicy
from tret.net.smtp import SmtpError, send_smtp_message

log = logging.getLogger("tret.mailer")

_EGRESS_CLASS = "email"  # audit-log label only — see module docstring
_RESEND_URL = "https://api.resend.com/emails"
_RESEND_HOST = "api.resend.com"


@dataclass
class SendResult:
    """What `send_invite_email` always returns, whatever went wrong (or
    right): the caller — `api/workspaces.py` — surfaces `sent` so the UI can
    tell a person "we emailed it" vs. "here's the link to send yourself"."""

    sent: bool
    detail: str | None = None  # human-readable: why not, or None when sent


def _resend_policy() -> ClassPolicy:
    """A one-host allowlist scoped to Resend's fixed API endpoint — see
    api/oidc.py's `_oidc_policy` for the identical reasoning: this is an
    operator-configured destination (the mode itself, `TRET_EMAIL_MODE=resend`),
    never a model- or document-chosen one, so `VERIFY_NONE` applies exactly as
    it does there."""
    return ClassPolicy(
        name=_EGRESS_CLASS,
        mode=MODE_OFF if get_settings().egress.strip().lower() == "off" else MODE_ON,
        allow_hosts=frozenset({_RESEND_HOST}),
        allow_http=False,
        standard_ports_only=True,
        verify_addresses=VERIFY_NONE,
        max_bytes=0,
        timeout_seconds=15.0,
    )


def _plaintext_body(workspace_name: str, invite_url: str, invited_by_name: str) -> str:
    return (
        f"{invited_by_name} invited you to join {workspace_name} on tret.\n\n"
        f"Accept the invite: {invite_url}\n\n"
        "If you weren't expecting this, you can safely ignore this email."
    )


async def _send_via_resend(to: str, subject: str, text: str) -> SendResult:
    settings = get_settings()
    if not (settings.resend_api_key and settings.email_from):
        log.warning("TRET_EMAIL_MODE=resend but TRET_RESEND_API_KEY or TRET_EMAIL_FROM is unset")
        return SendResult(sent=False, detail="email sending is not configured")
    try:
        async with build_client(_EGRESS_CLASS, policy=_resend_policy(), timeout=15.0) as client:
            response = await client.post(
                _RESEND_URL,
                json={"from": settings.email_from, "to": [to], "subject": subject, "text": text},
                headers={"Authorization": f"Bearer {settings.resend_api_key}"},
            )
            response.raise_for_status()
        return SendResult(sent=True)
    except EgressDenied as exc:
        log.warning("invite email not sent: egress denied: %s", exc)
        return SendResult(sent=False, detail=str(exc))
    except httpx.HTTPError as exc:
        log.warning("invite email not sent: %s", exc)
        return SendResult(sent=False, detail=str(exc))


async def _send_via_smtp(to: str, subject: str, text: str) -> SendResult:
    settings = get_settings()
    if not (settings.smtp_host and settings.email_from):
        log.warning("TRET_EMAIL_MODE=smtp but TRET_SMTP_HOST or TRET_EMAIL_FROM is unset")
        return SendResult(sent=False, detail="email sending is not configured")
    try:
        await send_smtp_message(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_tls=settings.smtp_tls,
            from_addr=settings.email_from,
            to_addr=to,
            subject=subject,
            body=text,
        )
        return SendResult(sent=True)
    except SmtpError as exc:
        log.warning("invite email not sent: %s", exc)
        return SendResult(sent=False, detail=str(exc))


async def send_invite_email(
    to: str, workspace_name: str, invite_url: str, invited_by_name: str
) -> SendResult:
    """Send one invite email if `TRET_EMAIL_MODE` is configured for it.

    Never raises: every failure — unconfigured, egress denied, the provider
    down, a relay refusing the connection — is caught and folded into
    `SendResult(sent=False, ...)`. The invite row and its copy-link are
    already created by the time this is called (see api/workspaces.py); an
    email that fails to send must never undo that.
    """
    mode = get_settings().email_mode
    if mode == "off":
        return SendResult(sent=False, detail="email sending is not configured")
    subject = f"You've been invited to {workspace_name} on tret"
    text = _plaintext_body(workspace_name, invite_url, invited_by_name)
    try:
        if mode == "resend":
            return await _send_via_resend(to, subject, text)
        if mode == "smtp":
            return await _send_via_smtp(to, subject, text)
    except Exception:  # noqa: BLE001 - a mail failure must never fail invite creation
        log.exception("invite email send raised unexpectedly")
    return SendResult(sent=False, detail="email sending is not configured")


__all__ = ["SendResult", "send_invite_email"]
