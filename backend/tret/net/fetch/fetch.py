"""Fetch one URL, under the research policy.

Every bound here exists because the URL may have been chosen by a model reading
an uploaded document:

* **Redirects are followed by hand**, up to `MAX_REDIRECTS`, and each hop is
  re-checked from scratch. A redirect is a second destination chosen by the
  first one; letting httpx follow it would mean the allowlist and the private
  address check applied only to the URL the model typed. Hop 2 onward is exactly
  where an SSRF lands.
* **The body is capped while it streams**, not after. A 4GB response must not be
  a 4GB `bytes` object first and a policy violation second.
* **Content types are an allowlist.** tret can turn HTML, text and PDF into
  something a model can read; everything else is a download, and downloading
  arbitrary bytes to disk on a model's say-so is not a capability this tool has.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

import httpx

from tret.config import get_settings
from tret.net.client import USER_AGENT, open_client
from tret.net.guard import check_url
from tret.net.policy import CLASS_RESEARCH, EgressDenied

MAX_REDIRECTS = 3

# What tret can read. The mapping is to the extension `services/documents.py`
# dispatches text extraction on, so a fetched PDF goes through the same pypdf
# path as an uploaded one rather than growing a second implementation.
READABLE_TYPES: dict[str, str] = {
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/csv": ".csv",
    "application/json": ".json",
    "application/pdf": ".pdf",
}

CHUNK_BYTES = 64 * 1024


class FetchError(RuntimeError):
    """The fetch was permitted but did not produce a readable page."""


@dataclass(frozen=True)
class FetchedPage:
    url: str  # the URL finally fetched (after redirects)
    requested_url: str
    status_code: int
    content_type: str
    extension: str
    body: bytes
    sha256: str
    duration_ms: int
    redirects: tuple[str, ...]
    truncated: bool


def _content_type(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


async def fetch_page(url: str) -> FetchedPage:
    """Retrieve one URL as bytes. Raises EgressDenied or FetchError."""
    settings = get_settings()
    max_bytes = int(settings.egress_research_max_bytes)
    timeout = float(settings.egress_research_timeout_seconds)
    started = time.monotonic()
    redirects: list[str] = []
    current = url

    async with open_client(
        CLASS_RESEARCH,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, "Accept": "text/html,text/plain,application/pdf,*/*;q=0.5"},
        follow_redirects=False,
    ) as client:
        for _hop in range(MAX_REDIRECTS + 1):
            # The client's event hook checks this too; doing it here as well is
            # what lets a denial name the hop that caused it.
            await check_url(current, CLASS_RESEARCH)
            try:
                async with client.stream("GET", current) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError(f"{resp.status_code} redirect with no Location header")
                        redirects.append(current)
                        current = str(httpx.URL(current).join(location))
                        continue
                    if resp.status_code >= 400:
                        raise FetchError(f"HTTP {resp.status_code} from {current}")
                    content_type = _content_type(resp)
                    extension = READABLE_TYPES.get(content_type)
                    if extension is None:
                        raise FetchError(
                            f"content-type {content_type or 'unknown'!r} is not something tret "
                            f"can read (it reads: {', '.join(sorted(READABLE_TYPES))})"
                        )
                    body = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                        body.extend(chunk)
                        if len(body) >= max_bytes:
                            del body[max_bytes:]
                            truncated = True
                            break
                    data = bytes(body)
                    return FetchedPage(
                        url=current,
                        requested_url=url,
                        status_code=resp.status_code,
                        content_type=content_type,
                        extension=extension,
                        body=data,
                        sha256=hashlib.sha256(data).hexdigest(),
                        duration_ms=int((time.monotonic() - started) * 1000),
                        redirects=tuple(redirects),
                        truncated=truncated,
                    )
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__}: {e}") from e
    raise EgressDenied(
        "too_many_redirects",
        url,
        CLASS_RESEARCH,
        f"more than {MAX_REDIRECTS} redirects starting at {url}",
    )
