"""Multi-engine search broker via `ddgs` metasearch library.

Uses ddgs to query multiple public HTML search backends (Brave, Mojeek,
DuckDuckGo) concurrently — no API keys, no external accounts. Fuses
results via reciprocal rank fusion for deterministic, provider-diverse output.

The broker owns:
- Explicit backend selection (no randomized "auto" mode)
- Concurrent multi-provider execution
- Per-provider circuit breakers
- Reciprocal rank fusion across providers
- Provider diagnostics in response metadata

ddgs handles:
- HTML scraping + selector maintenance
- UA rotation / browser impersonation
- Query parameter encoding per engine
- Pagination and date filtering
- Redirect URL cleanup

Requires: `ddgs` (pip install ddgs)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from functools import partial
from typing import Optional
from urllib.parse import urlparse

from ddgs import DDGS

from ..models import SearchRequest, SearchResponse, SearchResult
from ..normalization import canonicalize_url
from ..outcomes import SearchOutcome, classify_outcome

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend configuration
# ---------------------------------------------------------------------------

# Primary: distinct indexes, best coverage
PRIMARY_BACKENDS = ("brave", "mojeek")

# Secondary: good fallback, DDG proxies Bing results
SECONDARY_BACKENDS = ("duckduckgo",)

# Emergency: only when primary + secondary produce < 3 results
EMERGENCY_BACKENDS = ("yahoo", "startpage")

# Reference: always included for factual/definitional queries
REFERENCE_BACKENDS = ("wikipedia",)

ALL_BACKENDS = PRIMARY_BACKENDS + SECONDARY_BACKENDS


# ---------------------------------------------------------------------------
# Circuit breaker state (per-provider health tracking)
# ---------------------------------------------------------------------------

@dataclass
class ProviderHealth:
    """Tracks health of a single search backend."""
    successes: int = 0
    failures: int = 0
    last_success: float = 0.0
    last_failure: float = 0.0
    circuit_open_until: float = 0.0  # Timestamp when circuit re-closes
    consecutive_failures: int = 0

    @property
    def is_open(self) -> bool:
        """True if circuit breaker is open (provider is down)."""
        if self.circuit_open_until <= 0:
            return False
        return time.time() < self.circuit_open_until

    def record_success(self):
        self.successes += 1
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.circuit_open_until = 0

    def record_failure(self):
        self.failures += 1
        self.last_failure = time.time()
        self.consecutive_failures += 1
        # Open circuit after 3 consecutive failures, backoff exponentially
        if self.consecutive_failures >= 3:
            backoff = min(300, 30 * (2 ** (self.consecutive_failures - 3)))
            self.circuit_open_until = time.time() + backoff
            logger.warning(
                f"Circuit breaker opened for {backoff}s "
                f"(consecutive_failures={self.consecutive_failures})"
            )

    def to_dict(self) -> dict:
        return {
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "circuit_open": self.is_open,
        }


# Module-level health state (persists across requests within the process)
_provider_health: dict[str, ProviderHealth] = defaultdict(ProviderHealth)


# ---------------------------------------------------------------------------
# Provider implementation
# ---------------------------------------------------------------------------

class HtmlMetasearchProvider:
    """Multi-engine search broker using ddgs named backends.

    No API keys. No external accounts. Fully self-contained.
    Queries Brave, Mojeek, and DuckDuckGo public HTML concurrently,
    fuses results via reciprocal rank fusion.
    """

    @property
    def name(self) -> str:
        return "html_metasearch"

    @property
    def is_available(self) -> bool:
        return True  # Always available — no keys needed

    @property
    def supports_news(self) -> bool:
        return True

    @property
    def supports_freshness(self) -> bool:
        return True

    async def search(self, request: SearchRequest) -> SearchResponse:
        """Execute multi-provider search with RRF fusion."""
        start = time.time()

        # Build query with domain filters
        query = request.query
        if request.include_domains:
            site_clauses = " OR ".join(f"site:{d}" for d in request.include_domains)
            query = f"({query}) ({site_clauses})"
        if request.exclude_domains:
            for d in request.exclude_domains:
                query += f" -site:{d}"

        timelimit = self._map_freshness(request.freshness) if request.freshness else None

        # Select backends (skip those with open circuits)
        backends = [b for b in ALL_BACKENDS if not _provider_health[b].is_open]
        if not backends:
            # All circuits open — try anyway with primary
            backends = list(PRIMARY_BACKENDS)

        # Execute concurrent searches
        search_coros = [
            self._search_backend(
                query=query,
                backend=backend,
                max_results=request.num_results,
                timelimit=timelimit,
                is_news=(request.category == "news"),
            )
            for backend in backends
        ]
        raw_results = await asyncio.gather(*search_coros)

        # Collect per-provider results and diagnostics
        provider_results: dict[str, list[dict]] = {}
        provider_diagnostics: dict[str, dict] = {}

        for backend, (results, diag) in zip(backends, raw_results):
            provider_results[backend] = results
            provider_diagnostics[backend] = diag

            # Update circuit breaker based on typed outcome
            outcome = diag.get("outcome", SearchOutcome.UPSTREAM_ERROR)
            if outcome == SearchOutcome.OK:
                _provider_health[backend].record_success()
            elif outcome in (SearchOutcome.RATE_LIMITED, SearchOutcome.BLOCKED, SearchOutcome.UPSTREAM_ERROR):
                _provider_health[backend].record_failure()
            # NO_RESULTS and PARSER_DRIFT don't trip the circuit breaker

        # Emergency fallback if insufficient results
        total_results = sum(len(r) for r in provider_results.values())
        if total_results < 3:
            for emergency_backend in EMERGENCY_BACKENDS:
                if _provider_health[emergency_backend].is_open:
                    continue
                results, diag = await self._search_backend(
                    query=query,
                    backend=emergency_backend,
                    max_results=request.num_results,
                    timelimit=timelimit,
                    is_news=(request.category == "news"),
                )
                provider_results[emergency_backend] = results
                provider_diagnostics[emergency_backend] = diag
                if results:
                    _provider_health[emergency_backend].record_success()
                    break
                else:
                    _provider_health[emergency_backend].record_failure()

        # Reciprocal rank fusion
        fused = self._reciprocal_rank_fusion(provider_results, limit=request.num_results)

        elapsed_ms = int((time.time() - start) * 1000)

        # Build normalized SearchResult list
        search_results = []
        for rank, item in enumerate(fused, start=1):
            url = item.get("url", "")
            parsed_url = urlparse(url)
            search_results.append(SearchResult(
                source_id=f"S{rank}",
                rank=rank,
                title=item.get("title", ""),
                url=url,
                canonical_url=item.get("canonical_url", url),
                domain=parsed_url.netloc.replace("www.", ""),
                snippet=(item.get("body", "") or "")[:500],
                published_at=item.get("date"),
                score=item.get("score"),
            ))

        response = SearchResponse(
            query=request.query,
            provider=self.name,
            results=search_results,
            timing_ms=elapsed_ms,
        )

        # Attach provider diagnostics as warnings (visible to caller)
        for backend, diag in provider_diagnostics.items():
            if diag["status"] != "ok":
                response.warnings.append(f"{backend}: {diag['status']}")

        return response

    async def health_check(self) -> bool:
        """Check that at least one backend responds."""
        try:
            results, _ = await self._search_backend(
                query="test", backend="brave", max_results=1
            )
            return len(results) > 0
        except Exception:
            return False

    def get_provider_health(self) -> dict[str, dict]:
        """Return current circuit breaker state for all providers."""
        return {name: health.to_dict() for name, health in _provider_health.items()}

    # ------------------------------------------------------------------
    # Internal: per-backend search execution
    # ------------------------------------------------------------------

    async def _search_backend(
        self,
        query: str,
        backend: str,
        max_results: int = 10,
        timelimit: Optional[str] = None,
        is_news: bool = False,
    ) -> tuple[list[dict], dict]:
        """Execute a single backend search in a thread executor.

        Returns (results_list, diagnostics_dict).
        """
        start = time.time()
        try:
            loop = asyncio.get_running_loop()
            results = await loop.run_in_executor(
                None,
                partial(
                    self._sync_search,
                    query=query,
                    backend=backend,
                    max_results=max_results,
                    timelimit=timelimit,
                    is_news=is_news,
                ),
            )
            elapsed = int((time.time() - start) * 1000)
            # Classify outcome (we don't have raw_body here — ddgs handles parsing)
            outcome = SearchOutcome.OK if results else SearchOutcome.NO_RESULTS
            return results, {
                "status": outcome.value,
                "outcome": outcome,
                "result_count": len(results),
                "elapsed_ms": elapsed,
            }
        except Exception as e:
            elapsed = int((time.time() - start) * 1000)
            error_type = type(e).__name__
            error_msg = str(e).lower()
            logger.warning(f"Backend {backend} failed: {error_type}: {e}")

            # Classify from error message
            if "429" in error_msg or "rate" in error_msg:
                outcome = SearchOutcome.RATE_LIMITED
            elif "403" in error_msg or "blocked" in error_msg or "captcha" in error_msg:
                outcome = SearchOutcome.BLOCKED
            elif "timeout" in error_msg or "connect" in error_msg:
                outcome = SearchOutcome.UPSTREAM_ERROR
            else:
                outcome = SearchOutcome.UPSTREAM_ERROR

            return [], {
                "status": outcome.value,
                "outcome": outcome,
                "result_count": 0,
                "elapsed_ms": elapsed,
                "error": f"{error_type}: {e}",
            }

    @staticmethod
    def _sync_search(
        query: str,
        backend: str,
        max_results: int = 10,
        timelimit: Optional[str] = None,
        is_news: bool = False,
    ) -> list[dict]:
        """Synchronous ddgs call (runs in thread executor)."""
        with DDGS(timeout=8) as ddgs:
            kwargs = {
                "backend": backend,
                "region": "us-en",
                "safesearch": "moderate",
                "max_results": max_results,
            }
            if timelimit:
                kwargs["timelimit"] = timelimit

            if is_news:
                return list(ddgs.news(query, **kwargs))
            else:
                return list(ddgs.text(query, **kwargs))

    # ------------------------------------------------------------------
    # Reciprocal Rank Fusion
    # ------------------------------------------------------------------

    def _reciprocal_rank_fusion(
        self,
        provider_results: dict[str, list[dict]],
        limit: int = 10,
        rank_constant: int = 60,
    ) -> list[dict]:
        """Fuse results from multiple providers using RRF.

        RRF score = sum over providers of 1/(k + rank_in_provider)
        where k (rank_constant) dampens the effect of position.

        Results appearing in multiple providers get boosted.
        """
        scores: dict[str, float] = defaultdict(float)
        documents: dict[str, dict] = {}
        providers_seen: dict[str, set] = defaultdict(set)

        for provider, results in provider_results.items():
            for rank, item in enumerate(results, start=1):
                url = item.get("href") or item.get("url", "")
                if not url:
                    continue

                canonical = canonicalize_url(url)
                scores[canonical] += 1.0 / (rank_constant + rank)
                providers_seen[canonical].add(provider)

                # Keep first-seen document metadata (highest-ranked provider wins)
                if canonical not in documents:
                    documents[canonical] = item

        # Sort by fused score
        ordered = sorted(scores.keys(), key=lambda u: scores[u], reverse=True)

        output = []
        for url in ordered[:limit]:
            item = dict(documents[url])
            item["url"] = item.get("href") or item.get("url", url)
            item["canonical_url"] = url
            item["score"] = round(scores[url], 6)
            item["providers"] = sorted(providers_seen[url])
            output.append(item)

        return output

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def _map_freshness(freshness: str) -> Optional[str]:
        """Map our freshness format to ddgs timelimit param."""
        mapping = {
            "24h": "d", "1d": "d",
            "7d": "w", "1w": "w",
            "30d": "m", "1m": "m",
            "1y": "y", "365d": "y",
        }
        return mapping.get(freshness)
