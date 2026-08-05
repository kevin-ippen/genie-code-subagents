"""Search cache with stale-if-error support.

Two-tier TTL:
- Fresh TTL: Normal cache duration. get() returns result directly.
- Stale TTL: Extended window (3x fresh). get_stale() returns expired-but-usable
  results when live search fails — slightly outdated beats no results.

TTL defaults:
- News/fresh queries: 10m fresh, 1h stale
- Recent (7d): 30m fresh, 6h stale
- General web: 1h fresh, 24h stale
- Documentation: 6h fresh, 7d stale
"""

from __future__ import annotations

import hashlib
import logging
import time
from typing import Optional

from .models import SearchRequest, SearchResponse

logger = logging.getLogger(__name__)

# Stale multiplier: stale_ttl = fresh_ttl * STALE_MULTIPLIER
STALE_MULTIPLIER = 6


class SearchCache:
    """LRU-ish TTL cache with stale-if-error fallback."""

    def __init__(self, max_entries: int = 500):
        # Store: key → (fresh_expires, stale_expires, response)
        self._store: dict[str, tuple[float, float, SearchResponse]] = {}
        self._max_entries = max_entries
        self._hits = 0
        self._misses = 0
        self._stale_hits = 0

    def get(self, request: SearchRequest) -> Optional[SearchResponse]:
        """Look up cached response. Returns None on miss or fresh expiry."""
        key = self._cache_key(request)
        entry = self._store.get(key)
        if entry is None:
            self._misses += 1
            return None

        fresh_expires, _, response = entry
        if time.time() > fresh_expires:
            # Expired fresh — don't delete (stale window may still be valid)
            self._misses += 1
            return None

        self._hits += 1
        return response

    def get_stale(self, request: SearchRequest) -> Optional[SearchResponse]:
        """Return a stale (expired-fresh, within-stale-window) result.

        Called only when live search has failed. Returns None if no
        stale result available or if stale window has also expired.
        """
        key = self._cache_key(request)
        entry = self._store.get(key)
        if entry is None:
            return None

        _, stale_expires, response = entry
        now = time.time()

        if now > stale_expires:
            # Even stale window expired — truly gone
            del self._store[key]
            return None

        self._stale_hits += 1
        logger.info(f"Serving stale cache for '{request.query[:40]}'")
        return response

    def put(self, request: SearchRequest, response: SearchResponse):
        """Store a response with fresh + stale TTLs."""
        if response.error:
            return  # Don't cache errors

        key = self._cache_key(request)
        fresh_ttl = self._ttl_for(request)
        stale_ttl = fresh_ttl * STALE_MULTIPLIER
        now = time.time()

        self._store[key] = (
            now + fresh_ttl,
            now + stale_ttl,
            response,
        )

        # Evict if over capacity
        if len(self._store) > self._max_entries:
            self._evict_expired()
            if len(self._store) > self._max_entries:
                # Remove oldest 10% by stale_expires
                sorted_keys = sorted(
                    self._store.keys(),
                    key=lambda k: self._store[k][1]  # sort by stale_expires
                )
                for k in sorted_keys[: len(sorted_keys) // 10]:
                    del self._store[k]

    @property
    def stats(self) -> dict:
        total_requests = self._hits + self._misses + self._stale_hits
        return {
            "entries": len(self._store),
            "hits": self._hits,
            "stale_hits": self._stale_hits,
            "misses": self._misses,
            "hit_rate": round(self._hits / max(1, total_requests), 3),
            "stale_rate": round(self._stale_hits / max(1, total_requests), 3),
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
        """Determine fresh cache TTL based on query characteristics."""
        # News or explicit freshness → short cache
        if request.category == "news" or request.freshness in ("24h", "1d"):
            return 600  # 10 minutes (stale: 1 hour)

        # Very fresh queries
        if request.freshness in ("7d", "1w"):
            return 1800  # 30 minutes (stale: 3 hours)

        # Default web search
        return 3600  # 1 hour (stale: 6 hours)

    def _evict_expired(self):
        """Remove entries past their stale window."""
        now = time.time()
        expired = [k for k, (_, stale_exp, _) in self._store.items() if now > stale_exp]
        for k in expired:
            del self._store[k]
