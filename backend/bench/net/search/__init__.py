"""Web search, behind one interface. Default: none configured.

`get_search_provider()` is resolved per call rather than cached, so flipping
BENCH_SEARCH_PROVIDER (or narrowing egress at runtime) takes effect without a
restart and without a stale object holding an old API key.
"""
from __future__ import annotations

from bench.config import get_settings
from bench.net.search.base import SearchProvider, SearchResult, SearchUnavailable
from bench.net.search.brave import BraveSearchProvider
from bench.net.search.null import NullSearchProvider
from bench.net.search.searxng import SearxngSearchProvider

__all__ = [
    "SearchProvider",
    "SearchResult",
    "SearchUnavailable",
    "get_search_provider",
    "search_backend_name",
]


def search_backend_name(settings=None) -> str:
    return ((settings or get_settings()).search_provider or "").strip().lower()


def get_search_provider(settings=None) -> SearchProvider:
    """The configured backend, or the null one. Raises SearchUnavailable for a
    name nobody implements — a typo'd backend must not read as "search is off"."""
    s = settings or get_settings()
    name = search_backend_name(s)
    if not name or name == "none":
        return NullSearchProvider()
    if name == "brave":
        return BraveSearchProvider(s.search_api_key)
    if name == "searxng":
        return SearxngSearchProvider(s.searxng_base_url)
    raise SearchUnavailable(
        f"BENCH_SEARCH_PROVIDER={name!r} is not a backend bench implements "
        "(brave, searxng, or empty for none)."
    )
