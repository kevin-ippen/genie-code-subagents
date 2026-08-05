"""Search service: facade over SearchBroker for backward compatibility.

This delegates all search routing to the broker which owns:
- Provider admission (semaphores, coalescing, budgets)
- Progressive escalation (one-provider-first, fallback on poor results)
- Cache (fresh + stale-if-error)

Callers use SearchService exactly as before. The difference is internal:
no concurrent fan-out by default, and proper admission control.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .models import SearchRequest, SearchResponse
from .normalization import deduplicate_results, extract_domain
from .cache import SearchCache
from .broker import SearchBroker
from .providers.duckduckgo import HtmlMetasearchProvider
from .providers.brave import BraveSearchProvider

logger = logging.getLogger(__name__)


class SearchService:
    """Universal search service.

    Maintains backward-compatible interface. Internally delegates to
    SearchBroker for admission control and provider routing.
    """

    def __init__(self):
        # Cache (shared with broker)
        self._cache = SearchCache()

        # Broker (owns all provider interactions)
        self._broker = SearchBroker(cache=self._cache)

        # Register providers with their tiers
        # DDG HTML metasearch — always available, no API keys
        ddg = HtmlMetasearchProvider()
        self._broker.register_provider("duckduckgo", ddg, tier="secondary")

        # Brave — preferred when available (richer snippets, structured data)
        brave = BraveSearchProvider()
        if brave.is_available:
            self._broker.register_provider("brave", brave, tier="primary")
            logger.info("Brave Search registered (primary)")
        else:
            # Promote DDG to primary when Brave unavailable
            self._broker._providers["duckduckgo"].tier = "primary"

        logger.info(f"SearchService ready: {self._broker.provider_names}")

    @property
    def broker(self) -> SearchBroker:
        """Expose broker for run-budget management."""
        return self._broker

    @property
    def available_providers(self) -> list[str]:
        return self._broker.provider_names

    @property
    def is_available(self) -> bool:
        return bool(self._broker.provider_names)

    async def search(
        self,
        request: SearchRequest,
        run_id: Optional[str] = None,
        strategy: str = "progressive",
    ) -> SearchResponse:
        """Execute a search request via the broker.

        Args:
            request: Search query and parameters.
            run_id: Optional research-run ID for budget enforcement.
            strategy: "progressive" (default) or "broad" (fan-out for deep research).

        Returns:
            SearchResponse (possibly stale on provider failure).
        """
        response = await self._broker.search(request, run_id=run_id, strategy=strategy)

        if response.ok:
            # Post-processing: deduplicate and enforce diversity
            response.results = deduplicate_results(response.results)
            response.results = self._enforce_domain_diversity(response.results, max_per_domain=3)

            # Re-assign source IDs after dedup/filtering
            for i, result in enumerate(response.results, start=1):
                result.source_id = f"S{i}"
                result.rank = i
                if not result.domain:
                    result.domain = extract_domain(result.url)

        return response

    async def health(self) -> dict:
        """Quick broker state snapshot."""
        return self._broker.stats

    @staticmethod
    def _enforce_domain_diversity(results: list, max_per_domain: int = 3) -> list:
        """Limit results per domain to prevent any single site from dominating."""
        domain_counts: dict[str, int] = {}
        diverse: list = []

        for result in results:
            domain = extract_domain(result.url)
            count = domain_counts.get(domain, 0)
            if count >= max_per_domain:
                continue
            domain_counts[domain] = count + 1
            diverse.append(result)

        return diverse


# Module-level singleton (lazy init)
_service: Optional[SearchService] = None


def get_search_service() -> SearchService:
    """Get or create the search service singleton."""
    global _service
    if _service is None:
        _service = SearchService()
    return _service
