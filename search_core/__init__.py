"""Search core: provider-backed deterministic web search."""

from .models import SearchRequest, SearchResponse, SearchResult
from .service import SearchService, get_search_service

__all__ = [
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SearchService",
    "get_search_service",
]
