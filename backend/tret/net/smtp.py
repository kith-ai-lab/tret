"""The one place `smtplib` may be imported.

`smtplib` is a plain-socket protocol, not an HTTP one — there is no `httpx`
client to route through `tret.net.client`'s chokepoint at all, so the
socket-opening call itself has to live inside `tret/net/` instead, the same
way the rest of this package is the only place a connection opens
(`tests/test_egress_chokepoint.py` scans every module under `tret/` for a
bare `smtplib` import EXCEPT the ones in this directory). Callers outside
`tret/net/` — `tret/services/mailer.py`, for the self-hosted-SMTP invite-email
path — reach this module rather than importing `smtplib` themselves.

Gated by the same master `TRET_EGRESS` switch every other outbound call
obeys: an operator who has turned the deployment's egress off has turned off
outbound email too, the same reasoning `api/oidc.py`'s `_oidc_policy` gives
for its own fixed, operator-configured destination.

`smtplib` is synchronous and blocking; `services/mailer.py` is async and must
not stall the event loop on a slow or hanging relay, so the actual call runs
in a thread executor (`asyncio.to_thread`).
"""
from __future__ import annotations

import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from tret.config import get_settings
from tret.net.policy import master_is_off


class SmtpError(RuntimeError):
    """A configured SMTP send failed, timed out, or egress is off."""


def _send_sync(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    timeout: float,
) -> None:
    message = EmailMessage()
    message["From"] = from_addr
    message["To"] = to_addr
    message["Subject"] = subject
    message.set_content(body)

    with smtplib.SMTP(host, port, timeout=timeout) as server:
        if use_tls:
            # `starttls()` with no context defaults to an *unverified* SSL
            # context (`ssl._create_unverified_context()` deep inside
            # smtplib) — a relay MITM between here and `host` can present any
            # certificate and this would happily upgrade the connection to
            # it, credentials and all. `create_default_context()` turns on
            # certificate + hostname verification, the same as every `httpx`
            # call this codebase makes elsewhere (tret.net.client).
            server.starttls(context=ssl.create_default_context())
        if username:
            server.login(username, password)
        server.send_message(message)


async def send_smtp_message(
    *,
    host: str,
    port: int,
    username: str,
    password: str,
    use_tls: bool,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    timeout: float = 15.0,
) -> None:
    """Send one plain-text email over SMTP. Raises `SmtpError` on any
    failure — including egress being off — never a bare `smtplib` exception,
    so `services/mailer.py` has one exception type to catch."""
    if master_is_off(get_settings()):
        raise SmtpError("egress is off — refusing to open an SMTP connection")
    try:
        await asyncio.to_thread(
            _send_sync,
            host=host,
            port=port,
            username=username,
            password=password,
            use_tls=use_tls,
            from_addr=from_addr,
            to_addr=to_addr,
            subject=subject,
            body=body,
            timeout=timeout,
        )
    except (OSError, smtplib.SMTPException) as exc:
        raise SmtpError(str(exc)) from exc


__all__ = ["SmtpError", "send_smtp_message"]
