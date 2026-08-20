"""SearXNG: a self-hosted metasearch instance.

The reason this ships alongside a hosted API. With SearXNG inside your own
network the search *query* never leaves it either — which matters, because for
the work bench is built for the query is often the confidential part ("is
<client name> named in any enforcement action"). A hosted search API sees every
one of those.

Its base URL is operator-configured and may well be an internal address, so
these calls are still `research` class but the host lands in the research
allowlist automatically (`policy.research_allow_hosts`).
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from bench.config import get_settings
from bench.net.client import USER_AGENT, open_client
from bench.net.policy import CLASS_RESEARCH
from bench.net.search.base import SearchResult, SearchUnavailable


class SearxngSearchProvider:
    name = "searxng"
    host = "searxng"

    def __init__(self, base_url: str):
        if not base_url:
            raise SearchUnavailable(
                "BENCH_SEARCH_PROVIDER=searxng but BENCH_SEARXNG_BASE_URL is empty."
            )
        self._base_url = base_url.rstrip("/")
        self.host = urlsplit(self._base_url).hostname or "searxng"

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        settings = get_settings()
        headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
        try:
            async with open_client(
                CLASS_RESEARCH,
                timeout=float(settings.egress_research_timeout_seconds),
                headers=headers,
            ) as client:
                resp = await client.get(
                    f"{self._base_url}/search",
                    params={"q": query, "format": "json", "safesearch": 1},
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SearchUnavailable(f"SearXNG search failed: {type(e).__name__}: {e}") from e
        results = (payload or {}).get("results") or []
        out = [
            SearchResult(
                title=str(item.get("title") or "")[:300],
                url=str(item.get("url") or ""),
                snippet=str(item.get("content") or "")[:600],
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]
        return out[: max(1, int(max_results))]
