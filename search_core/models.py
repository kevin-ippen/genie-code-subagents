"""Search core data models.

All search providers normalize into these structures for a consistent
downstream interface regardless of which provider answered the query.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SearchRequest:
    """Normalized inbound search request."""
    query: str
    num_results: int = 10
    provider: str = "auto"  # auto | brave | exa | searxng
    freshness: Optional[str] = None  # e.g. "24h", "7d", "30d"
    start_date: Optional[str] = None  # ISO date
    end_date: Optional[str] = None  # ISO date
    include_domains: Optional[list[str]] = None
    exclude_domains: Optional[list[str]] = None
    category: str = "web"  # web | news | code | academic
    language: str = "en"
    fetch_content: bool = False  # If True, also fetch page content for top results


@dataclass
class SearchResult:
    """A single search result from any provider."""
    source_id: str  # Stable ID for citation (e.g. "S1", "S2")
    rank: int
    title: str
    url: str
    canonical_url: Optional[str] = None
    domain: str = ""
    snippet: str = ""
    published_at: Optional[str] = None  # ISO timestamp if available
    score: Optional[float] = None  # Provider-specific relevance score
    content: Optional[str] = None  # Fetched content if fetch_content=True
    content_hash: Optional[str] = None


@dataclass
class SearchResponse:
    """Normalized search response from any provider."""
    query: str
    provider: str
    results: list[SearchResult] = field(default_factory=list)
    retrieved_at: str = field(default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    timing_ms: int = 0
    total_results_available: Optional[int] = None  # Provider-reported total
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.results) > 0

    def to_dict(self) -> dict:
        """Serialize to JSON-safe dict."""
        return {
            "query": self.query,
            "provider": self.provider,
            "retrieved_at": self.retrieved_at,
            "results": [
                {
                    "source_id": r.source_id,
                    "rank": r.rank,
                    "title": r.title,
                    "url": r.url,
                    "canonical_url": r.canonical_url,
                    "domain": r.domain,
                    "snippet": r.snippet,
                    "published_at": r.published_at,
                    "score": r.score,
                }
                for r in self.results
            ],
            "timing_ms": self.timing_ms,
            "total_results_available": self.total_results_available,
            "warnings": self.warnings,
            "error": self.error,
        }
