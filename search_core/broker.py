"""SearchBroker — admission control layer between agents and providers.

Owns the 5 core policies that make search sustainable at scale:

1. Per-provider semaphore (concurrency=1)
   → No provider ever receives concurrent requests from this process.

2. Progressive escalation (one-provider-first)
   → Primary provider is tried first. Fallback only fires when results
     are insufficient (< MIN_ACCEPTABLE or low domain diversity).

3. Single-flight request coalescing
   → Identical in-flight searches share one upstream call.

4. Stale-if-error
   → When live search fails, serve stale cached results transparently.

5. Research-run budget enforcement
   → Hard cap on searches per run, enforced here not in config.

Agents never call providers directly. Every request routes through here.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from .models import SearchRequest, SearchResponse, SearchResult
from .cache import SearchCache
from .outcomes import SearchOutcome
from .normalization import deduplicate_results, extract_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

MIN_ACCEPTABLE_RESULTS = 5
MIN_DISTINCT_DOMAINS = 3

# Per-run budget defaults (can be overridden per request)
DEFAULT_MAX_SEARCHES_PER_RUN = 6
DEFAULT_MAX_SEARCHES_PER_PROVIDER = 4


@dataclass
class ProviderSlot:
    """Runtime state for a single provider behind the broker."""
    name: str
    provider: object  # SearchProvider protocol
    semaphore: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(1))
    tier: str = "primary"  # primary | secondary | emergency


@dataclass
class RunBudget:
    """Tracks search budget for a single research run."""
    run_id: str
    max_searches: int = DEFAULT_MAX_SEARCHES_PER_RUN
    searches_used: int = 0
    created_at: float = field(default_factory=time.time)

    @property
    def exhausted(self) -> bool:
        return self.searches_used >= self.max_searches

    @property
    def remaining(self) -> int:
        return max(0, self.max_searches - self.searches_used)


class SearchBroker:
    """Central admission controller for all search requests.

    Usage:
        broker = SearchBroker(cache=cache)
        broker.register_provider("brave", brave_adapter, tier="primary")
        broker.register_provider("mojeek", mojeek_adapter, tier="secondary")

        response = await broker.search(request)
    """

    def __init__(self, cache: SearchCache):
        self._cache = cache
        self._providers: dict[str, ProviderSlot] = {}
        self._inflight: dict[str, asyncio.Future] = {}
        self._budgets: dict[str, RunBudget] = {}

    # ------------------------------------------------------------------
    # Provider registration
    # ------------------------------------------------------------------

    def register_provider(self, name: str, provider, tier: str = "primary"):
        """Register a search provider with its tier."""
        self._providers[name] = ProviderSlot(
            name=name,
            provider=provider,
            tier=tier,
        )
        logger.info(f"Broker: registered provider '{name}' (tier={tier})")

    @property
    def provider_names(self) -> list[str]:
        return list(self._providers.keys())

    # ------------------------------------------------------------------
    # Budget management
    # ------------------------------------------------------------------

    def create_budget(self, run_id: str, max_searches: int = DEFAULT_MAX_SEARCHES_PER_RUN) -> RunBudget:
        """Create a search budget for a research run."""
        budget = RunBudget(run_id=run_id, max_searches=max_searches)
        self._budgets[run_id] = budget
        return budget

    def get_budget(self, run_id: str) -> Optional[RunBudget]:
        return self._budgets.get(run_id)

    def release_budget(self, run_id: str):
        """Clean up a completed run's budget."""
        self._budgets.pop(run_id, None)

    # ------------------------------------------------------------------
    # Core search (progressive escalation)
    # ------------------------------------------------------------------

    async def search(
        self,
        request: SearchRequest,
        run_id: Optional[str] = None,
        strategy: str = "progressive",  # progressive | broad
    ) -> SearchResponse:
        """Execute a search with full admission control.

        Args:
            request: The search request.
            run_id: Optional research-run ID for budget tracking.
            strategy: "progressive" (default, one-first) or "broad" (fan-out).

        Returns:
            SearchResponse with results, possibly stale.
        """
        # --- Budget check ---
        if run_id:
            budget = self._budgets.get(run_id)
            if budget and budget.exhausted:
                return SearchResponse(
                    query=request.query,
                    provider="broker",
                    error="Search budget exhausted for this research run.",
                    warnings=[f"Used {budget.searches_used}/{budget.max_searches} searches."],
                )

        # --- Cache check (fresh) ---
        cached = self._cache.get(request)
        if cached is not None and not getattr(cached, '_is_stale', False):
            return cached

        # --- Single-flight coalescing ---
        cache_key = self._request_key(request)
        if cache_key in self._inflight:
            logger.debug(f"Coalescing: joining existing request for '{request.query[:40]}'")
            return await self._inflight[cache_key]

        # --- Execute search ---
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._inflight[cache_key] = future

        try:
            if strategy == "broad":
                response = await self._search_broad(request)
            else:
                response = await self._search_progressive(request)

            # Budget accounting
            if run_id:
                budget = self._budgets.get(run_id)
                if budget:
                    budget.searches_used += 1

            # Cache successful results
            if response.ok:
                self._cache.put(request, response)

            future.set_result(response)
            return response

        except Exception as exc:
            # --- Stale-if-error fallback ---
            stale = self._cache.get_stale(request)
            if stale is not None:
                logger.info(f"Serving stale result for '{request.query[:40]}' (provider error)")
                stale.warnings = stale.warnings or []
                stale.warnings.append("Results may be outdated (live search failed).")
                future.set_result(stale)
                return stale

            future.set_exception(exc)
            raise
        finally:
            self._inflight.pop(cache_key, None)

    # ------------------------------------------------------------------
    # Progressive escalation (one-first + quality gate)
    # ------------------------------------------------------------------

    async def _search_progressive(self, request: SearchRequest) -> SearchResponse:
        """Try primary provider first. Escalate only on poor results."""

        ordered = self._providers_by_tier()

        for slot in ordered:
            # Skip providers with open circuit breakers
            if hasattr(slot.provider, 'is_circuit_open') and slot.provider.is_circuit_open:
                continue

            response = await self._call_provider(slot, request)

            if response.ok and self._quality_sufficient(response):
                response.provider = slot.name
                return response

            # Log and try next
            logger.info(
                f"Provider '{slot.name}' insufficient: "
                f"{len(response.results)} results, "
                f"escalating to next tier"
            )

        # All providers tried — return best we got (or error)
        return SearchResponse(
            query=request.query,
            provider="broker",
            error="All providers returned insufficient results.",
        )

    # ------------------------------------------------------------------
    # Broad fan-out (for depth=deep research only)
    # ------------------------------------------------------------------

    async def _search_broad(self, request: SearchRequest) -> SearchResponse:
        """Fan out to all available providers and fuse results (RRF)."""

        slots = [s for s in self._providers.values()
                 if not (hasattr(s.provider, 'is_circuit_open') and s.provider.is_circuit_open)]

        if not slots:
            return SearchResponse(query=request.query, provider="broker",
                                  error="No providers available.")

        tasks = [self._call_provider(slot, request) for slot in slots]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

        # Collect all results with provider attribution
        all_results = []
        providers_used = []
        for slot, resp in zip(slots, responses):
            if isinstance(resp, Exception):
                continue
            if resp.ok:
                providers_used.append(slot.name)
                for r in resp.results:
                    r.domain = r.domain or extract_domain(r.url)
                all_results.extend(resp.results)

        if not all_results:
            return SearchResponse(query=request.query, provider="broker",
                                  error="No results from any provider (broad).")

        # Deduplicate
        all_results = deduplicate_results(all_results)

        return SearchResponse(
            query=request.query,
            provider=",".join(providers_used),
            results=all_results[:request.num_results],
        )

    # ------------------------------------------------------------------
    # Provider call (with semaphore)
    # ------------------------------------------------------------------

    async def _call_provider(self, slot: ProviderSlot, request: SearchRequest) -> SearchResponse:
        """Call a single provider, guarded by its semaphore."""
        async with slot.semaphore:
            try:
                return await slot.provider.search(request)
            except Exception as e:
                logger.warning(f"Provider '{slot.name}' error: {e}")
                return SearchResponse(
                    query=request.query,
                    provider=slot.name,
                    error=str(e),
                )

    # ------------------------------------------------------------------
    # Quality gate
    # ------------------------------------------------------------------

    @staticmethod
    def _quality_sufficient(response: SearchResponse) -> bool:
        """Check if results meet minimum quality threshold."""
        if len(response.results) < MIN_ACCEPTABLE_RESULTS:
            return False

        domains = set(extract_domain(r.url) for r in response.results)
        if len(domains) < MIN_DISTINCT_DOMAINS:
            return False

        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _providers_by_tier(self) -> list[ProviderSlot]:
        """Return providers ordered: primary → secondary → emergency."""
        tier_order = {"primary": 0, "secondary": 1, "emergency": 2}
        return sorted(
            self._providers.values(),
            key=lambda s: tier_order.get(s.tier, 99),
        )

    @staticmethod
    def _request_key(request: SearchRequest) -> str:
        """Canonical cache key for coalescing identical requests."""
        # Normalize: lowercase, collapse whitespace, sort domain filters
        q = " ".join(request.query.lower().split())
        domains = ",".join(sorted(request.include_domains or []))
        freshness = request.freshness or ""
        category = request.category or "web"
        raw = f"{q}|{request.num_results}|{category}|{freshness}|{domains}"
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    @property
    def stats(self) -> dict:
        """Current broker state for diagnostics."""
        return {
            "providers": {
                name: {
                    "tier": slot.tier,
                    "semaphore_available": slot.semaphore._value,
                }
                for name, slot in self._providers.items()
            },
            "inflight": len(self._inflight),
            "active_budgets": len(self._budgets),
            "cache": self._cache.stats,
        }
