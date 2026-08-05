"""Base protocol for search providers.

Every search provider (Brave, Exa, SearXNG, etc.) implements this
protocol so the SearchService can route queries without coupling
to any specific vendor.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import SearchRequest, SearchResponse


@runtime_checkable
class SearchProvider(Protocol):
    """Interface that all search providers must implement."""

    @property
    def name(self) -> str:
        """Provider name (e.g. 'brave', 'exa', 'searxng')."""
        ...

    @property
    def is_available(self) -> bool:
        """Whether this provider is configured and ready."""
        ...

    @property
    def supports_news(self) -> bool:
        """Whether this provider has a dedicated news endpoint."""
        ...

    @property
    def supports_freshness(self) -> bool:
        """Whether this provider supports time-range filtering."""
        ...

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute a search and return normalized results."""
        ...

    async def health_check(self) -> bool:
        """Quick connectivity check. Returns True if provider is reachable."""
        ...
