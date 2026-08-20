"""The only place in tret that constructs an HTTP client.

Everything that leaves the process goes through here, tagged with the class it
belongs to (`tret/net/policy.py`). That is the property the whole package
exists for: "can this deployment reach the internet, and what for?" is answered
by reading one directory rather than by grepping the codebase forever.

`tests/test_egress_chokepoint.py` enforces it — an `import httpx` anywhere else
under `tret/` fails the suite. Like `tret/packs/safety.py`, that scan catches
drift and accidents, not a determined author; it is a design constraint made
executable, not a sandbox.

Redirects are **not** followed by default. A redirect is a second destination
chosen by the first one, so it needs the same check as the first — and the
research fetcher follows them by hand, counting hops (`tret/net/fetch/`). The
per-request event hook here is the backstop that makes a missed check impossible
rather than the mechanism that performs it.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx

from tret.config import get_settings
from tret.net import audit
from tret.net.guard import check_url
from tret.net.policy import ClassPolicy, EgressDenied, policy_for

log = logging.getLogger("tret.net")

# Sent on research requests so an operator on the receiving end can tell what
# this is and who to talk to. Provider calls keep their SDK's own agent string.
USER_AGENT = "tret/0.1 (+https://github.com/tret-platform/tret)"


def _hook(egress_class: str, policy: ClassPolicy):
    async def enforce(request: httpx.Request) -> None:
        target = await check_url(str(request.url), egress_class, policy)
        audit.note_attempt(egress_class, target.host)

    return enforce


def build_client(
    egress_class: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    headers: dict | None = None,
    follow_redirects: bool = False,
    policy: ClassPolicy | None = None,
) -> httpx.AsyncClient:
    """An httpx client bound to one egress class. The caller closes it.

    Prefer `open_client`. This form exists for the Anthropic SDK, which takes
    ownership of a client for the life of the provider object and so cannot be
    handed a context manager.

    The class being *off* is refused here, at construction, rather than later at
    the first request: a provider that cannot legally talk to anything should
    fail where the failure names the setting, not deep inside a stream.
    """
    pol = policy or policy_for(egress_class)
    if not pol.enabled:
        raise EgressDenied(
            "class_disabled",
            "",
            egress_class,
            f"egress class {egress_class!r} is {pol.mode} — refusing to open a client. "
            f"Set TRET_EGRESS_{egress_class.upper()}=on (and TRET_EGRESS=on) to allow it.",
        )
    proxy = (get_settings().egress_proxy or "").strip() or None
    return httpx.AsyncClient(
        timeout=timeout if timeout is not None else pol.timeout_seconds,
        headers=headers,
        follow_redirects=follow_redirects,
        proxy=proxy,
        event_hooks={"request": [_hook(egress_class, pol)]},
    )


@asynccontextmanager
async def open_client(
    egress_class: str,
    *,
    timeout: float | httpx.Timeout | None = None,
    headers: dict | None = None,
    follow_redirects: bool = False,
) -> AsyncIterator[httpx.AsyncClient]:
    client = build_client(
        egress_class, timeout=timeout, headers=headers, follow_redirects=follow_redirects
    )
    async with client:
        yield client
