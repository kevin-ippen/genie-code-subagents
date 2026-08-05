"""Search core: provider-backed deterministic search.

Architecture:
- DDGSBrokerProvider: multi-engine (Brave HTML + Mojeek + DDG), no API keys
- BraveSearchProvider: optional Brave API upgrade (paid, higher rate limits)
- SearchService: routing, post-processing, caching
- SearchCache: TTL-based response cache (reduces redundant outbound requests)
- Normalization: URL canonicalization, dedup, domain diversity
"""

from .models import SearchRequest, SearchResponse, SearchResult
from .service import SearchService, get_search_service
from .cache import SearchCache

__all__ = [
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "SearchCache",
    "get_search_service",
]
