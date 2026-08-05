"""Research executor: coordinates parallel search and retrieval.

Orchestrates the full research workflow:
1. Plan (decompose into sub-queries)
2. Search (parallel provider queries)
3. Select (choose pages to read)
4. Read (parallel page retrieval)
5. Synthesize (claim-based answer with citations)
6. Verify (check claims against evidence)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable
from uuid import uuid4

from ..search_core.models import SearchRequest, SearchResponse
from ..search_core.service import get_search_service
from ..retrieval.reader import read_url, ReadResult
from ..retrieval.chunking import Passage
from .planner import plan_research, ResearchPlan, ModelCallFn
from .synthesizer import synthesize_research, SynthesisResult
from .verifier import verify_claims, VerificationResult

logger = logging.getLogger(__name__)


@dataclass
class ResearchConfig:
    """Configuration for a research run."""
    depth: str = "standard"  # quick | standard | deep
    max_searches: int = 6
    max_sources: int = 15
    max_pages_to_read: int = 8
    source_policy: str = "balanced"  # balanced | thorough | fast
    freshness: Optional[str] = None
    include_domains: Optional[list[str]] = None
    model_override: Optional[str] = None


@dataclass
class ResearchRun:
    """Complete result of a research execution."""
    run_id: str = field(default_factory=lambda: f"research_{uuid4().hex[:12]}")
    query: str = ""
    answer: str = ""
    claims: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    research_trace: dict = field(default_factory=dict)
    metrics: dict = field(default_factory=dict)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "answer": self.answer,
            "claims": self.claims,
            "sources": self.sources,
            "unresolved_questions": self.unresolved_questions,
            "research_trace": self.research_trace,
            "metrics": self.metrics,
            "error": self.error,
        }


async def execute_research(
    query: str,
    config: ResearchConfig,
    model_call: Optional[ModelCallFn] = None,
) -> ResearchRun:
    """Execute a full research workflow.

    Args:
        query: The research question.
        config: Research configuration.
        model_call: Async model call function for LLM steps.

    Returns:
        Complete ResearchRun with answer, claims, sources, and trace.
    """
    run = ResearchRun(query=query)
    start_time = time.time()
    total_tokens = 0
    all_passages: list[Passage] = []
    sources_read: list[ReadResult] = []

    search_service = get_search_service()

    # Register budget for this run (enforced at broker level)
    strategy = "broad" if config.depth == "deep" else "progressive"
    search_service.broker.create_budget(
        run_id=run.run_id,
        max_searches=config.max_searches,
    )

    # ---------------------------------------------------------------
    # Step 1: Plan
    # ---------------------------------------------------------------
    plan = await plan_research(
        query,
        depth=config.depth,
        model_call=model_call,
        model_override=config.model_override,
    )

    run.research_trace["plan"] = {
        "queries": plan.sub_queries,
        "reasoning": plan.reasoning,
    }

    # ---------------------------------------------------------------
    # Step 2: Search (parallel)
    # ---------------------------------------------------------------
    if not search_service.is_available:
        run.error = "No search providers configured. Set BRAVE_SEARCH_API_KEY."
        # Release budget
        search_service.broker.release_budget(run.run_id)

        run.metrics = {"elapsed_ms": int((time.time() - start_time) * 1000)}
        return run

    # Sequential search with broker admission control.
    # The broker handles: budget enforcement, coalescing, provider semaphores.
    # For "progressive" strategy: one provider at a time, escalate on poor results.
    # For "broad" (depth=deep): broker internally fans out but with semaphores.
    search_responses: list[SearchResponse] = []
    for sub_query in plan.sub_queries[:config.max_searches]:
        request = SearchRequest(
            query=sub_query,
            num_results=10,
            freshness=config.freshness,
            include_domains=config.include_domains,
        )
        try:
            resp = await search_service.search(
                request,
                run_id=run.run_id,
                strategy=strategy,
            )
            search_responses.append(resp)
        except Exception as e:
            search_responses.append(e)

    # Collect all results
    all_results = []
    search_errors = []
    for resp in search_responses:
        if isinstance(resp, Exception):
            search_errors.append(str(resp))
            continue
        if resp.ok:
            all_results.extend(resp.results)
        elif resp.error:
            search_errors.append(resp.error)

    run.research_trace["searches"] = {
        "executed": len(search_responses),
        "total_results": len(all_results),
        "errors": search_errors,
    }

    if not all_results:
        run.error = "No search results found from any query."
        run.metrics = {"elapsed_ms": int((time.time() - start_time) * 1000)}
        return run

    # ---------------------------------------------------------------
    # Step 3: Select pages to read (deduplicate, rank, limit)
    # ---------------------------------------------------------------
    seen_urls = set()
    pages_to_read = []
    for result in all_results:
        if result.url in seen_urls:
            continue
        seen_urls.add(result.url)
        pages_to_read.append(result)
        if len(pages_to_read) >= config.max_pages_to_read:
            break

    # ---------------------------------------------------------------
    # Step 4: Read pages (parallel)
    # ---------------------------------------------------------------
    read_tasks = [read_url(r.url, mode="chunks") for r in pages_to_read]
    read_results: list[ReadResult] = await asyncio.gather(*read_tasks, return_exceptions=True)

    for i, read_result in enumerate(read_results):
        if isinstance(read_result, Exception):
            continue
        if read_result.ok:
            sources_read.append(read_result)
            # Assign source IDs to passages
            source_id = f"S{i + 1}"
            for passage in read_result.passages:
                passage.passage_id = f"{source_id}-P{passage.passage_id.split('-P')[1] if '-P' in passage.passage_id else '1'}"
                passage.source_url = read_result.final_url
            all_passages.extend(read_result.passages)

    run.research_trace["pages"] = {
        "considered": len(pages_to_read),
        "read_successfully": len(sources_read),
        "total_passages": len(all_passages),
    }

    # Build sources list
    run.sources = [
        {
            "source_id": f"S{i + 1}",
            "url": sr.final_url,
            "title": sr.title,
            "domain": sr.final_url.split("//")[-1].split("/")[0] if "//" in sr.final_url else "",
        }
        for i, sr in enumerate(sources_read)
    ]

    if not all_passages:
        # No content extracted — synthesize from snippets instead
        snippet_text = "\n".join(
            f"- [{r.title}]({r.url}): {r.snippet}"
            for r in all_results[:10]
        )
        run.answer = f"Based on search results:\n\n{snippet_text}"
        run.metrics = {
            "elapsed_ms": int((time.time() - start_time) * 1000),
            "search_requests": len(search_responses),
            "pages_read": 0,
        }
        return run

    # ---------------------------------------------------------------
    # Step 5: Synthesize
    # ---------------------------------------------------------------
    synthesis: Optional[SynthesisResult] = None
    if model_call:
        synthesis = await synthesize_research(
            question=query,
            passages=all_passages,
            sources=run.sources,
            model_call=model_call,
            model_override=config.model_override,
        )
        if synthesis:
            run.answer = synthesis.answer
            run.claims = synthesis.claims
            run.unresolved_questions = synthesis.unresolved_questions
            total_tokens += synthesis.tokens_used

    # ---------------------------------------------------------------
    # Step 6: Verify (if we have claims)
    # ---------------------------------------------------------------
    if model_call and run.claims and all_passages:
        verification = await verify_claims(
            claims=run.claims,
            passages=all_passages,
            model_call=model_call,
            model_override=config.model_override,
        )
        if verification:
            run.claims = verification.verified_claims
            total_tokens += verification.tokens_used

    # ---------------------------------------------------------------
    # Release broker budget
    search_service.broker.release_budget(run.run_id)

    # Metrics
    # ---------------------------------------------------------------
    run.metrics = {
        "elapsed_ms": int((time.time() - start_time) * 1000),
        "model_tokens": total_tokens,
        "search_requests": len(search_responses),
        "pages_read": len(sources_read),
        "passages_extracted": len(all_passages),
    }

    return run
