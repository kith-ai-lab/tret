"""The default backend: none.

tret ships without a search vendor, and this is what that looks like from the
inside — a provider that exists, is registered, and says plainly that it is not
configured. The alternative (no provider object at all) pushes a None check into
every caller and turns "search is off" into an AttributeError somewhere.
"""
from __future__ import annotations

from tret.net.search.base import SearchResult, SearchUnavailable


class NullSearchProvider:
    name = "none"
    host = ""

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        raise SearchUnavailable(
            "No web search backend is configured. Set TRET_SEARCH_PROVIDER to "
            "'brave' (with TRET_SEARCH_API_KEY) or 'searxng' (with "
            "TRET_SEARXNG_BASE_URL)."
        )
