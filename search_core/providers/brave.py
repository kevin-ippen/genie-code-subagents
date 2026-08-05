"""Brave Search API provider.

Brave provides a structured web search API with its own index.
Supports web search, news, date filtering, and domain restrictions.

API docs: https://api.search.brave.com/app
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..models import SearchRequest, SearchResponse, SearchResult

logger = logging.getLogger(__name__)

_BRAVE_API_BASE = "https://api.search.brave.com/res/v1"


class BraveSearchProvider:
    """Brave Search API adapter."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("BRAVE_SEARCH_API_KEY", "")

    @property
    def name(self) -> str:
        return "brave"

    @property
    def is_available(self) -> bool:
        return bool(self._api_key)

    @property
    def supports_news(self) -> bool:
        return True

    @property
    def supports_freshness(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute search via Brave API."""
        if not self._api_key:
            return SearchResponse(
                query=request.query,
                provider=self.name,
                error="Brave API key not configured (set BRAVE_SEARCH_API_KEY)",
            )

        endpoint = f"{_BRAVE_API_BASE}/news/search" if request.category == "news" else f"{_BRAVE_API_BASE}/web/search"

        params = {
            "q": request.query,
            "count": min(request.num_results, 20),  # Brave max is 20
            "search_lang": request.language,
            "text_decorations": "false",
        }

        # Freshness filter
        if request.freshness:
            params["freshness"] = self._map_freshness(request.freshness)

        # Extra snippets for better context
        if request.category == "web":
            params["extra_snippets"] = "true"

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }

        start = time.time()
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(endpoint, params=params, headers=headers)
                elapsed_ms = int((time.time() - start) * 1000)

                if resp.status_code == 429:
                    return SearchResponse(
                        query=request.query,
                        provider=self.name,
                        timing_ms=elapsed_ms,
                        error="Brave rate limited (429)",
                    )

                if resp.status_code != 200:
                    return SearchResponse(
                        query=request.query,
                        provider=self.name,
                        timing_ms=elapsed_ms,
                        error=f"Brave API error: {resp.status_code} {resp.text[:200]}",
                    )

                data = resp.json()
                results = self._parse_web_results(data, request) if request.category != "news" else self._parse_news_results(data, request)

                # Apply domain filters (post-filter since Brave doesn't natively support all combos)
                results = self._apply_domain_filters(results, request)

                return SearchResponse(
                    query=request.query,
                    provider=self.name,
                    results=results,
                    timing_ms=elapsed_ms,
                    total_results_available=data.get("query", {}).get("total_results"),
                )

        except httpx.TimeoutException:
            return SearchResponse(
                query=request.query,
                provider=self.name,
                timing_ms=int((time.time() - start) * 1000),
                error="Brave API timeout",
            )
        except Exception as e:
            return SearchResponse(
                query=request.query,
                provider=self.name,
                timing_ms=int((time.time() - start) * 1000),
                error=f"Brave API error: {type(e).__name__}: {e}",
            )

    async def health_check(self) -> bool:
        """Quick check that the API key works."""
        if not self._api_key:
            return False
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(
                    f"{_BRAVE_API_BASE}/web/search",
                    params={"q": "test", "count": 1},
                    headers={"X-Subscription-Token": self._api_key},
                )
                return resp.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Parsing helpers
    # ------------------------------------------------------------------

    def _parse_web_results(self, data: dict, request: SearchRequest) -> list[SearchResult]:
        """Parse Brave web search response into normalized results."""
        results = []
        web_results = data.get("web", {}).get("results", [])

        for rank, item in enumerate(web_results[:request.num_results], start=1):
            url = item.get("url", "")
            parsed_url = urlparse(url)

            # Build snippet from description + extra snippets
            snippet_parts = [item.get("description", "")]
            for extra in item.get("extra_snippets", []):
                snippet_parts.append(extra)
            snippet = " ".join(snippet_parts).strip()

            results.append(SearchResult(
                source_id=f"S{rank}",
                rank=rank,
                title=item.get("title", ""),
                url=url,
                canonical_url=item.get("canonical_url") or url,
                domain=parsed_url.netloc,
                snippet=snippet[:500],
                published_at=item.get("page_age"),
                score=None,  # Brave doesn't expose relevance scores
            ))

        return results

    def _parse_news_results(self, data: dict, request: SearchRequest) -> list[SearchResult]:
        """Parse Brave news search response."""
        results = []
        news_results = data.get("results", [])

        for rank, item in enumerate(news_results[:request.num_results], start=1):
            url = item.get("url", "")
            parsed_url = urlparse(url)

            results.append(SearchResult(
                source_id=f"S{rank}",
                rank=rank,
                title=item.get("title", ""),
                url=url,
                canonical_url=url,
                domain=parsed_url.netloc,
                snippet=item.get("description", "")[:500],
                published_at=item.get("age") or item.get("date"),
                score=None,
            ))

        return results

    def _apply_domain_filters(self, results: list[SearchResult], request: SearchRequest) -> list[SearchResult]:
        """Post-filter results by include/exclude domain lists."""
        if not request.include_domains and not request.exclude_domains:
            return results

        filtered = []
        for r in results:
            if request.include_domains:
                if not any(d in r.domain for d in request.include_domains):
                    continue
            if request.exclude_domains:
                if any(d in r.domain for d in request.exclude_domains):
                    continue
            filtered.append(r)

        return filtered

    @staticmethod
    def _map_freshness(freshness: str) -> str:
        """Map our freshness format to Brave's.

        Brave accepts: pd (past day), pw (past week), pm (past month), py (past year)
        We accept: 24h, 7d, 30d, 1y or brave-native formats.
        """
        mapping = {
            "24h": "pd", "1d": "pd",
            "7d": "pw", "1w": "pw",
            "30d": "pm", "1m": "pm",
            "1y": "py", "365d": "py",
        }
        return mapping.get(freshness, freshness)
