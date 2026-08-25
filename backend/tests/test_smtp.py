"""`tret/net/smtp.py`'s `starttls()` call: it must upgrade to a *verified*
TLS connection, not smtplib's own unverified default.

`smtplib.SMTP.starttls()` with no `context` argument builds one via
`ssl._create_unverified_context()` deep inside the stdlib — no certificate
or hostname check at all, so a MITM sitting between this process and the
configured relay host can present any certificate and this would happily
authenticate (`server.login`) straight through it. `ssl.create_default_context()`
turns both checks back on, matching every other outbound connection this
codebase makes (`tret.net.client`'s httpx clients verify by default too).

`smtplib.SMTP` is monkeypatched with a fake recording exactly what
`_send_sync` did to it — no real socket, matching
`tests/test_egress_chokepoint.py`'s boundary around this one module.
"""
from __future__ import annotations

import ssl

import pytest

from tret.net import smtp as smtp_module


class FakeSmtpConnection:
    """Records constructor args and every method call; usable as the
    context manager `with smtplib.SMTP(...) as server:` expects."""

    instances: list["FakeSmtpConnection"] = []

    def __init__(self, host, port, timeout=None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.starttls_calls: list[dict] = []
        self.logins: list[tuple] = []
        self.sent_messages: list[object] = []
        FakeSmtpConnection.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def starttls(self, context=None):
        self.starttls_calls.append({"context": context})

    def login(self, username, password):
        self.logins.append((username, password))

    def send_message(self, message):
        self.sent_messages.append(message)


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeSmtpConnection.instances.clear()
    yield
    FakeSmtpConnection.instances.clear()


@pytest.fixture
def fake_smtp(monkeypatch):
    monkeypatch.setattr(smtp_module.smtplib, "SMTP", FakeSmtpConnection)
    return FakeSmtpConnection


async def test_starttls_is_called_with_a_verifying_context(fake_smtp, monkeypatch):
    monkeypatch.setattr(smtp_module, "master_is_off", lambda settings: False)
    await smtp_module.send_smtp_message(
        host="smtp.example.test",
        port=587,
        username="",
        password="",
        use_tls=True,
        from_addr="noreply@example.com",
        to_addr="person@example.com",
        subject="subject",
        body="body",
    )
    (conn,) = fake_smtp.instances
    (call,) = conn.starttls_calls
    context = call["context"]
    assert isinstance(context, ssl.SSLContext)
    # The signature of a *verifying* context, as opposed to
    # ssl._create_unverified_context()'s CERT_NONE/check_hostname=False.
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


async def test_starttls_is_skipped_entirely_when_use_tls_is_false(fake_smtp, monkeypatch):
    """Not this finding's concern, but pins the branch the fix sits in: no
    context to pass when there is no starttls call at all."""
    monkeypatch.setattr(smtp_module, "master_is_off", lambda settings: False)
    await smtp_module.send_smtp_message(
        host="smtp.example.test",
        port=25,
        username="",
        password="",
        use_tls=False,
        from_addr="noreply@example.com",
        to_addr="person@example.com",
        subject="subject",
        body="body",
    )
    (conn,) = fake_smtp.instances
    assert conn.starttls_calls == []
    assert len(conn.sent_messages) == 1
