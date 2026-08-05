"""Simple in-memory search cache with TTL.

Caches normalized query → SearchResponse for a configurable duration.
Reduces redundant outbound requests when multiple agents issue
semantically similar searches within a short window.

TTL defaults:
- News/fresh queries: 10 minutes
- General web: 60 minutes  
- Documentation/reference: 6 hours
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from .models import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)


class SearchCache:
    """LRU-ish TTL cache for search responses."""

    def __init__(self, max_entries: int = 500):
        self._store: dict[str, tuple[float, SearchResponse]] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0

    def get(self, request: SearchRequest) -> Optional[SearchResponse]:
        """Look up cached response. Returns None on miss or expiry."""
        key = self._cache_key(request)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        expires_at, response = entry
        if time.time() > expires_at:
            del self._store[key]
            self._misses += 1
            return None

        self._hits += 1
        return response

    def put(self, request: SearchRequest, response: SearchResponse):
        """Store a response with appropriate TTL."""
        if response.error:
            return  # Don't cache errors

        key = self._cache_key(request)
        ttl = self._ttl_for(request)
        expires_at = time.time() + ttl
        self._store[key] = (expires_at, response)

        # Evict oldest entries if over capacity
        if len(self._store) > self._max_entries:
            self._evict_expired()
            if len(self._store) > self._max_entries:
                # Remove oldest 10%
                sorted_keys = sorted(
                    self._store.keys(),
                    key=lambda k: self._store[k][0]
                )
                for k in sorted_keys[: len(sorted_keys) // 10]:
                    del self._store[k]

    @property
    def stats(self) -> dict:
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, self._hits + self._misses), 3),
        }

    def _cache_key(self, request: SearchRequest) -> str:
        """Deterministic key from query + relevant params."""
        parts = [
            request.query.lower().strip(),
            str(request.num_results),
            request.category or "web",
            request.freshness or "",
            request.language or "en",
        ]
        raw = "|".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @staticmethod
    def _ttl_for(request: SearchRequest) -> float:
        """Determine cache TTL based on query characteristics."""
        # News or explicit freshness → short cache
        if request.category == "news" or request.freshness in ("24h", "1d"):
            return 600  # 10 minutes

        # Very fresh queries
        if request.freshness in ("7d", "1w"):
            return 1800  # 30 minutes

        # Default web search
        return 3600  # 1 hour

    def _evict_expired(self):
        """Remove all expired entries."""
        now = time.time()
        expired = [k for k, (exp, _) in self._store.items() if now > exp]
        for k in expired:
            del self._store[k]
