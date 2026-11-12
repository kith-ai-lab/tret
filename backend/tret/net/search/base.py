"""The search-provider seam.

Search backends differ in every way that does not matter here (auth header,
result key names, ranking) and not at all in the way that does: a query goes
out, titles and URLs come back. Keeping that behind one protocol means the tool
in `engine/tools.py` never learns which vendor is configured, and swapping Brave
for a self-hosted SearXNG is a settings change rather than a code change.

`SearchUnavailable` is a first-class outcome, not an error path. A deployment
with no search backend is the *default* deployment, and a harness that lists
`web_search` should still run there — the model is told the tool is unconfigured
and gets on with the work it can do.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class SearchResult:
    """One hit. Navigational only — see engine/tools.py::web_search.

    Deliberately not a document: a snippet is a vendor's summary of a page tret
    never saw, so it may not be quoted as evidence and carries no provenance. The
    URL is the useful part; `fetch_url` is what turns one into something citable.
    """

    title: str
    url: str
    snippet: str


class SearchUnavailable(RuntimeError):
    """No search backend is configured, or the configured one is unreachable."""


class SearchNotConfigured(SearchUnavailable):
    """No usable backend is set up (none chosen, a typo'd name, a missing key or
    URL). An operator's choice, not an outage: audited apart from a backend that
    is configured and failing."""


class SearchProvider(Protocol):
    name: str
    host: str  # the endpoint reached, for the egress audit row

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...


# Zero results while at least this many of the backend's engines failed to
# answer is read as an outage, not an honest empty answer. One flaky engine is
# normal for a metasearch backend; two is not.
SEARCH_DEGRADED_MIN_ENGINES = 2

_UNRESPONSIVE_MAX_ENTRIES = 12
_UNRESPONSIVE_NAME_CHARS = 40
_UNRESPONSIVE_REASON_CHARS = 80
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


@dataclass(frozen=True)
class SearchResponse:
    """What a backend answered: its hits, and the (engine, reason) pairs of the
    engines it says did not respond. `unresponsive` is empty for a backend that
    does not report that."""

    results: list[SearchResult]
    unresponsive: list[tuple[str, str]] = field(default_factory=list)


def _clean(value: Any, limit: int) -> str:
    if value is None or isinstance(value, (dict, list, tuple, set)):
        return ""
    text = "".join(ch for ch in str(value) if unicodedata.category(ch) != "Cf")
    return re.sub(r"\s+", " ", _CONTROL_RE.sub(" ", text)).strip()[:limit]


def parse_unresponsive(raw: Any) -> list[tuple[str, str]]:
    """SearXNG's `unresponsive_engines` as clean (engine, reason) pairs. It emits
    a list of `[engine, reason]` pairs; anything else it might send is skipped
    rather than raised on, since this is read on the failure path."""
    if not isinstance(raw, (list, tuple)):
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        name = reason = ""
        if isinstance(item, str):
            name = item
        elif isinstance(item, (list, tuple)) and 1 <= len(item) <= 2:
            name = _clean(item[0], _UNRESPONSIVE_NAME_CHARS)
            reason = _clean(item[1], _UNRESPONSIVE_REASON_CHARS) if len(item) == 2 else ""
        elif isinstance(item, dict):
            name = item.get("engine") or item.get("name") or ""
            reason = _clean(item.get("reason") or item.get("error") or item.get("message"), _UNRESPONSIVE_REASON_CHARS)
        name = _clean(name, _UNRESPONSIVE_NAME_CHARS)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append((name, reason))
        if len(out) >= _UNRESPONSIVE_MAX_ENTRIES:
            break
    return out


async def search_detailed(provider: Any, query: str, *, max_results: int) -> SearchResponse:
    """`provider.search_detailed` when it has one, else its plain `search()` with
    nothing reported as unresponsive (a fake or third-party provider)."""
    detailed = getattr(provider, "search_detailed", None)
    if detailed is not None:
        return await detailed(query, max_results=max_results)
    return SearchResponse(results=await provider.search(query, max_results=max_results))
