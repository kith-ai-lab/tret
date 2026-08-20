"""Brave Search API.

One GET, a token in a header, `web.results[]` back. Chosen as the shipped cloud
adapter because it is a plain REST endpoint with no SDK, which keeps the whole
backend inside the chokepoint and this file under a page.

The query leaves the deployment. That is inherent to a hosted search API and is
the reason `searxng.py` exists next to this one.
"""
from __future__ import annotations

import httpx

from tret.config import get_settings
from tret.net.client import USER_AGENT, open_client
from tret.net.policy import CLASS_RESEARCH
from tret.net.search.base import SearchResult, SearchUnavailable

ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchProvider:
    name = "brave"
    host = "api.search.brave.com"

    def __init__(self, api_key: str):
        if not api_key:
            raise SearchUnavailable(
                "TRET_SEARCH_PROVIDER=brave but TRET_SEARCH_API_KEY is empty."
            )
        self._api_key = api_key

    async def search(self, query: str, *, max_results: int) -> list[SearchResult]:
        headers = {
            "Accept": "application/json",
            "X-Subscription-Token": self._api_key,
            "User-Agent": USER_AGENT,
        }
        timeout = float(get_settings().egress_research_timeout_seconds)
        try:
            async with open_client(CLASS_RESEARCH, timeout=timeout, headers=headers) as client:
                resp = await client.get(
                    ENDPOINT, params={"q": query, "count": max(1, min(int(max_results), 20))}
                )
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            raise SearchUnavailable(f"Brave search failed: {type(e).__name__}: {e}") from e
        results = ((payload or {}).get("web") or {}).get("results") or []
        return [
            SearchResult(
                title=str(item.get("title") or "")[:300],
                url=str(item.get("url") or ""),
                snippet=str(item.get("description") or "")[:600],
            )
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]
