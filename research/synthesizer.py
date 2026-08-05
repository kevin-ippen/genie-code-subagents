"""Research synthesizer: generates claim-based answers with citations.

Takes extracted passages and produces a structured answer where
each claim is linked to supporting evidence passages.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from ..retrieval.chunking import Passage

logger = logging.getLogger(__name__)

ModelCallFn = Callable[[str, list[dict], int, Optional[str]], Awaitable[dict]]


@dataclass
class SynthesisResult:
    """Output of the synthesis step."""
    answer: str = ""
    claims: list[dict] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    tokens_used: int = 0


_SYNTHESIZER_SYSTEM = """You are a research synthesizer. Given a question and extracted passages from web sources,
produce a comprehensive answer with claim-level citations.

Rules:
- Every factual claim must cite at least one passage by ID (e.g. S1-P3)
- If passages conflict, note the disagreement and cite both sides
- If the passages don't fully answer the question, list unresolved sub-questions
- Be precise and specific — prefer data and quotes over generalizations
- Structure the answer with clear sections if the topic is complex

Return ONLY valid JSON:
{
  "answer": "Full markdown answer with inline [S1-P3] citations",
  "claims": [
    {"claim_id": "C1", "text": "specific claim", "citations": ["S1-P3", "S2-P1"]}
  ],
  "unresolved_questions": ["questions that couldn't be answered from available sources"]
}"""


async def synthesize_research(
    question: str,
    passages: list[Passage],
    sources: list[dict],
    model_call: ModelCallFn,
    model_override: Optional[str] = None,
    max_passage_chars: int = 20_000,
) -> Optional[SynthesisResult]:
    """Synthesize an answer from passages with citations.

    Args:
        question: The original research question.
        passages: Extracted passages with IDs.
        sources: Source metadata list.
        model_call: Async model call function.
        model_override: Model override.
        max_passage_chars: Budget for passage context.

    Returns:
        SynthesisResult or None on failure.
    """
    # Build evidence context (most relevant passages, within budget)
    evidence_lines = []
    char_count = 0
    for passage in passages:
        line = f"[{passage.passage_id}] ({passage.source_url})\n{passage.text}"
        if char_count + len(line) > max_passage_chars:
            break
        evidence_lines.append(line)
        char_count += len(line)

    # Build sources reference
    sources_ref = "\n".join(
        f"- {s['source_id']}: [{s.get('title', 'Untitled')}]({s['url']})"
        for s in sources
    )

    user_content = f"""QUESTION: {question}

SOURCES:
{sources_ref}

EVIDENCE PASSAGES:
{chr(10).join(evidence_lines)}

Synthesize a comprehensive answer with claim-level citations."""

    messages = [
        {"role": "system", "content": _SYNTHESIZER_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    result = await model_call("research_synthesize", messages, 2048, model_override)

    if result.get("error"):
        logger.warning(f"Synthesis model error: {result['error']}")
        # Fallback: simple concatenation
        return SynthesisResult(
            answer=_fallback_synthesis(question, passages, sources),
            tokens_used=0,
        )

    tokens = result.get("usage", {}).get("total_tokens", 0)

    # Parse JSON response
    raw = result["text"].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        parsed = json.loads(raw)
        return SynthesisResult(
            answer=parsed.get("answer", ""),
            claims=parsed.get("claims", []),
            unresolved_questions=parsed.get("unresolved_questions", []),
            tokens_used=tokens,
        )
    except json.JSONDecodeError:
        # Model returned free text — use it as the answer
        return SynthesisResult(
            answer=raw,
            tokens_used=tokens,
        )


def _fallback_synthesis(question: str, passages: list[Passage], sources: list[dict]) -> str:
    """Simple fallback when model synthesis fails."""
    lines = [f"## Research: {question}\n"]
    lines.append("Based on the following sources:\n")
    for s in sources[:5]:
        lines.append(f"- [{s.get('title', 'Source')}]({s['url']})")
    lines.append("\n### Key findings:\n")
    for p in passages[:5]:
        lines.append(f"- [{p.passage_id}] {p.text[:200]}...")
    return "\n".join(lines)
