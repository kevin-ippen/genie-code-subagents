"""Search service: routes queries to providers and normalizes responses.

This is the single entry point for all search operations. It:
1. Selects the appropriate provider based on request params
2. Executes the search
3. Normalizes, canonicalizes, and deduplicates results
4. Applies domain diversity rules
5. Returns a deterministic SearchResponse
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from .models import SearchRequest, SearchResponse
from .normalization import deduplicate_results, extract_domain
from .providers.brave import BraveSearchProvider

logger = logging.getLogger(__name__)


class SearchService:
    """Universal search service with provider routing."""

    def __init__(self):
        # Initialize available providers
        self._providers = {}
        self._default_provider: Optional[str] = None

        # Brave (primary)
        brave = BraveSearchProvider()
        if brave.is_available:
            self._providers["brave"] = brave
            self._default_provider = "brave"
            logger.info("Brave Search provider configured")

        # Future: Exa, SearXNG, etc.
        # exa = ExaSearchProvider()
        # if exa.is_available:
        #     self._providers["exa"] = exa

        if not self._providers:
            logger.warning("No search providers configured. Set BRAVE_SEARCH_API_KEY to enable search.")

    @property
    def available_providers(self) -> list[str]:
        return list(self._providers.keys())

    @property
    def is_available(self) -> bool:
        return bool(self._providers)

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute a search request.

        Provider selection logic:
        - "auto": Use the best provider for the request type
        - Specific name: Use that provider or error
        """
        provider = self._select_provider(request)
        if not provider:
            return SearchResponse(
                query=request.query,
                provider="none",
                error=self._no_provider_error(request),
            )

        # Execute search
        response = await provider.search(request)

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
        """Check all providers."""
        results = {}
        for name, provider in self._providers.items():
            try:
                results[name] = await provider.health_check()
            except Exception:
                results[name] = False
        return results

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _select_provider(self, request: SearchRequest):
        """Select provider based on request."""
        if request.provider != "auto":
            return self._providers.get(request.provider)

        # Auto-routing logic:
        # - News queries go to providers with news support
        # - Default to primary provider
        if request.category == "news":
            for p in self._providers.values():
                if p.supports_news:
                    return p

        # Fallback to default
        if self._default_provider:
            return self._providers[self._default_provider]

        # Return first available
        return next(iter(self._providers.values()), None)

    def _no_provider_error(self, request: SearchRequest) -> str:
        if request.provider != "auto" and request.provider not in self._providers:
            available = ", ".join(self._providers.keys()) if self._providers else "none"
            return f"Provider '{request.provider}' not available. Configured: [{available}]"
        return "No search providers configured. Set BRAVE_SEARCH_API_KEY environment variable."

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
