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

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class SearchResult:
    """One hit. Navigational only — see engine/tools.py::web_search.

    Deliberately not a document: a snippet is a vendor's summary of a page bench
    never saw, so it may not be quoted as evidence and carries no provenance. The
    URL is the useful part; `fetch_url` is what turns one into something citable.
    """

    title: str
    url: str
    snippet: str


class SearchUnavailable(RuntimeError):
    """No search backend is configured, or the configured one is unreachable."""


class SearchProvider(Protocol):
    name: str
    host: str  # the endpoint reached, for the egress audit row

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]: ...
