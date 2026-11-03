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
  `capture=True` (the `capture_source` tool) widens the list to the originals
  issuers publish next to a report — workbooks, ESEF packages — and in exchange
  checks that a "PDF" really is one, since the point of capture is that the stored
  bytes are the document and not an error page that happened to say so.
* **Truncation is a mode, not a fact.** Reading a page tolerates a cut-off body;
  collecting a file does not, so `fail_on_truncate` turns "too big" into an error
  instead of a short file whose hash looks like the real thing's.
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

# Originals only `capture_source` may store. Kept apart from READABLE_TYPES so a
# `fetch_url` call can never be talked into downloading a workbook or an archive.
# xhtml appears in both on purpose: reading it wants the `.html` extraction path,
# but an ESEF original should keep its real extension on disk.
CAPTURE_TYPES: dict[str, str] = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
    "application/xhtml+xml": ".xhtml",
    "application/zip": ".zip",
}
# Servers that do not know a PDF's type say octet-stream. It is accepted as a PDF
# only on the evidence of the bytes themselves.
PDF_MAGIC = b"%PDF-"
SNIFFED_TYPE = "application/octet-stream"

# The response headers kept as collection provenance. Nothing else is stored:
# cookies and auth-adjacent headers have no business in a document row.
# content-encoding is kept because a gzip response's content-length is the
# compressed size, which would otherwise read as truncation next to `meta.bytes`.
PROVENANCE_HEADERS = (
    "content-length",
    "content-encoding",
    "last-modified",
    "etag",
    "content-disposition",
)

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
    headers: tuple[tuple[str, str], ...] = ()


def _content_type(resp: httpx.Response) -> str:
    return (resp.headers.get("content-type") or "").split(";", 1)[0].strip().lower()


def _provenance_headers(resp: httpx.Response) -> tuple[tuple[str, str], ...]:
    return tuple((name, resp.headers[name]) for name in PROVENANCE_HEADERS if name in resp.headers)


def _require_pdf(body: bytearray, content_type: str) -> None:
    if body.startswith(PDF_MAGIC):
        return
    if content_type == SNIFFED_TYPE:
        raise FetchError(
            f"content-type {content_type!r} is not something tret can read, and the body "
            "does not start with %PDF- so it is not a PDF either"
        )
    raise FetchError(
        "served as application/pdf but the body does not start with %PDF- (often an HTML "
        "error page); not storing it as a PDF"
    )


async def fetch_page(
    url: str,
    *,
    max_bytes: int | None = None,
    fail_on_truncate: bool = False,
    capture: bool = False,
) -> FetchedPage:
    """Retrieve one URL as bytes. Raises EgressDenied or FetchError.

    The defaults are the research-reading behaviour `fetch_url` relies on; the
    keywords exist for `capture_source` (see the module docstring).
    """
    settings = get_settings()
    if max_bytes is None:
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
                    sniff_pdf = capture and content_type == SNIFFED_TYPE
                    extension = (
                        CAPTURE_TYPES[content_type]
                        if capture and content_type in CAPTURE_TYPES
                        else ".pdf"
                        if sniff_pdf  # provisional: confirmed against the bytes below
                        else READABLE_TYPES.get(content_type)
                    )
                    if extension is None:
                        readable = sorted(READABLE_TYPES | CAPTURE_TYPES if capture else READABLE_TYPES)
                        raise FetchError(
                            f"content-type {content_type or 'unknown'!r} is not something tret "
                            f"can read (it reads: {', '.join(readable)})"
                        )
                    declared = resp.headers.get("content-length", "")
                    if fail_on_truncate and declared.isdigit() and int(declared) > max_bytes:
                        # Saves streaming 200 MB to learn what the headers said.
                        raise FetchError(
                            f"{current} is {int(declared)} bytes, over the {max_bytes}-byte limit; "
                            "refusing to store a truncated copy"
                        )
                    # The bytes must be a PDF for capture to call them one. Decided on
                    # the head of the stream, so a mislabelled body is refused without
                    # reading the other 40 MB; `pdf_checked` waits for 5 bytes because a
                    # chunk boundary is not a promise about how many arrive first.
                    must_be_pdf = capture and (sniff_pdf or content_type == "application/pdf")
                    pdf_checked = not must_be_pdf
                    body = bytearray()
                    truncated = False
                    async for chunk in resp.aiter_bytes(CHUNK_BYTES):
                        body.extend(chunk)
                        if not pdf_checked and len(body) >= len(PDF_MAGIC):
                            _require_pdf(body, content_type)
                            pdf_checked = True
                        if fail_on_truncate and len(body) > max_bytes:
                            raise FetchError(
                                f"{current} is larger than the {max_bytes}-byte limit; refusing "
                                "to store a truncated copy"
                            )
                        if not fail_on_truncate and len(body) >= max_bytes:
                            del body[max_bytes:]
                            truncated = True
                            break
                    if not pdf_checked:
                        _require_pdf(body, content_type)
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
                        headers=_provenance_headers(resp),
                    )
            except httpx.HTTPError as e:
                raise FetchError(f"{type(e).__name__}: {e}") from e
    raise EgressDenied(
        "too_many_redirects",
        url,
        CLASS_RESEARCH,
        f"more than {MAX_REDIRECTS} redirects starting at {url}",
    )
