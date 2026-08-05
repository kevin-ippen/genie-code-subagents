"""Research planner: decomposes questions into search sub-queries.

The planner takes a research question and generates 3-6 distinct
search queries that together cover the information needed to answer
it comprehensively.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class ResearchPlan:
    """Output of the planning step."""
    original_question: str
    sub_queries: list[str] = field(default_factory=list)
    reasoning: str = ""
    depth: str = "standard"  # quick | standard | deep
    follow_up_needed: bool = False


# Type alias for model call function
ModelCallFn = Callable[[str, list[dict], int, Optional[str]], Awaitable[dict]]

_PLANNER_SYSTEM = """You are a research query planner. Given a question, decompose it into
3-6 distinct web search queries that together will gather the information needed
for a comprehensive answer.

Rules:
- Each query should target a different aspect or angle
- Use specific, searchable terms (not full sentences as questions)
- Include queries for recent/current information when the question implies timeliness
- Include at least one query that challenges the premise (for balanced research)
- For technical topics, include both overview and specific-detail queries

Return ONLY valid JSON:
{"queries": ["query1", "query2", ...], "reasoning": "brief explanation of decomposition strategy"}"""


async def plan_research(
    question: str,
    *,
    depth: str = "standard",
    model_call: Optional[ModelCallFn] = None,
    model_override: Optional[str] = None,
) -> ResearchPlan:
    """Decompose a research question into search sub-queries.

    Args:
        question: The research question to decompose.
        depth: Research depth (affects number of queries).
        model_call: Async function to call a model (role, messages, max_tokens, override).
        model_override: Model override for the planner.

    Returns:
        ResearchPlan with sub-queries.
    """
    num_queries = {"quick": 3, "standard": 5, "deep": 6}.get(depth, 5)

    # If no model available, use heuristic decomposition
    if model_call is None:
        return _heuristic_plan(question, num_queries, depth)

    messages = [
        {"role": "system", "content": _PLANNER_SYSTEM},
        {"role": "user", "content": f"Question: {question}\n\nGenerate {num_queries} search queries."},
    ]

    result = await model_call("research_planner", messages, 512, model_override)

    if result.get("error"):
        logger.warning(f"Planner model error, using heuristic: {result['error']}")
        return _heuristic_plan(question, num_queries, depth)

    # Parse response
    raw = result["text"].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        parsed = json.loads(raw)
        queries = parsed.get("queries", [])
        reasoning = parsed.get("reasoning", "")

        if not queries:
            return _heuristic_plan(question, num_queries, depth)

        return ResearchPlan(
            original_question=question,
            sub_queries=queries[:num_queries],
            reasoning=reasoning,
            depth=depth,
        )
    except json.JSONDecodeError:
        logger.warning("Planner returned non-JSON, using heuristic")
        return _heuristic_plan(question, num_queries, depth)


def _heuristic_plan(question: str, num_queries: int, depth: str) -> ResearchPlan:
    """Simple fallback: generate queries without a model call."""
    queries = [question]  # Original question as first query

    # Add a more specific variant
    if len(question.split()) > 5:
        # Use first ~5 significant words as a tighter query
        words = [w for w in question.split() if len(w) > 3]
        queries.append(" ".join(words[:5]))

    # Add a "how" variant
    if not question.lower().startswith("how"):
        queries.append(f"how {question.lower().rstrip('?')}")

    # Add a "latest" / current variant
    queries.append(f"{question} 2026")

    # Pad if needed
    while len(queries) < num_queries:
        queries.append(f"{question} explained")
        break

    return ResearchPlan(
        original_question=question,
        sub_queries=queries[:num_queries],
        reasoning="heuristic decomposition (no model available)",
        depth=depth,
    )
