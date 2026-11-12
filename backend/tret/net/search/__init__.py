"""Web search, behind one interface. Default: none configured.

`get_search_provider()` is resolved per call rather than cached, so flipping
TRET_SEARCH_PROVIDER (or narrowing egress at runtime) takes effect without a
restart and without a stale object holding an old API key.
"""
from __future__ import annotations

from tret.config import get_settings
from tret.net.search.base import (
    SEARCH_DEGRADED_MIN_ENGINES,
    SearchNotConfigured,
    SearchProvider,
    SearchResponse,
    SearchResult,
    SearchUnavailable,
    search_detailed,
)
from tret.net.search.brave import BraveSearchProvider
from tret.net.search.null import NullSearchProvider
from tret.net.search.searxng import SearxngSearchProvider

__all__ = [
    "SEARCH_DEGRADED_MIN_ENGINES",
    "SearchNotConfigured",
    "SearchResponse",
    "search_detailed",
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
    raise SearchNotConfigured(
        f"TRET_SEARCH_PROVIDER={name!r} is not a backend tret implements "
        "(brave, searxng, or empty for none)."
    )
