"""Research verifier: validates claims against cited passages.

Checks each claim from the synthesizer to ensure it is actually
supported by the cited passages. Marks unsupported claims and
identifies gaps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

from ..retrieval.chunking import Passage

from .utils import extract_json

logger = logging.getLogger(__name__)

ModelCallFn = Callable[[str, list[dict], int, Optional[str]], Awaitable[dict]]


@dataclass
class VerificationResult:
    """Output of the verification step."""
    verified_claims: list[dict] = field(default_factory=list)
    tokens_used: int = 0


_VERIFIER_SYSTEM = """You are a citation verifier. For each claim, check if the cited passages
actually support it.

For each claim, determine:
- "supported": The cited passages directly state or strongly imply this claim
- "partially_supported": The passages are related but don't fully confirm the claim
- "unsupported": The cited passages don't actually support this claim
- "contradicted": The passages say the opposite

Return ONLY valid JSON:
{
  "verified": [
    {"claim_id": "C1", "support": "supported|partially_supported|unsupported|contradicted", "note": "brief explanation"}
  ]
}"""


async def verify_claims(
    claims: list[dict],
    passages: list[Passage],
    model_call: ModelCallFn,
    model_override: Optional[str] = None,
) -> Optional[VerificationResult]:
    """Verify each claim against its cited passages.

    Args:
        claims: List of claim dicts from synthesis (claim_id, text, citations).
        passages: All extracted passages.
        model_call: Async model call function.
        model_override: Model override.

    Returns:
        VerificationResult with updated claims, or None on failure.
    """
    if not claims:
        return VerificationResult(verified_claims=claims)

    # Build passage lookup
    passage_map = {p.passage_id: p for p in passages}

    # Build verification context
    verification_items = []
    for claim in claims:
        cited_passages = []
        for citation_id in claim.get("citations", []):
            if citation_id in passage_map:
                p = passage_map[citation_id]
                cited_passages.append(f"  [{p.passage_id}]: {p.text[:300]}")

        verification_items.append({
            "claim_id": claim.get("claim_id", ""),
            "claim_text": claim.get("text", ""),
            "cited_evidence": "\n".join(cited_passages) if cited_passages else "[no passages found for cited IDs]",
        })

    user_content = "Verify these claims against their cited evidence:\n\n"
    for item in verification_items:
        user_content += f"CLAIM {item['claim_id']}: {item['claim_text']}\n"
        user_content += f"EVIDENCE:\n{item['cited_evidence']}\n\n"

    messages = [
        {"role": "system", "content": _VERIFIER_SYSTEM},
        {"role": "user", "content": user_content},
    ]

    result = await model_call("research_verifier", messages, 1024, model_override)

    if result.get("error"):
        logger.warning(f"Verifier error: {result['error']}")
        # Return claims unchanged
        return VerificationResult(verified_claims=claims)

    tokens = result.get("usage", {}).get("total_tokens", 0)

    # Parse verification results
    raw = result["text"].strip()
    if raw.startswith("```"):
        lines = raw.split("\n")
        raw = "\n".join(lines[1:-1] if lines[-1].startswith("```") else lines[1:])

    try:
        parsed = json.loads(raw)
        verification_map = {
            v["claim_id"]: v for v in parsed.get("verified", [])
        }

        # Merge verification into claims
        updated_claims = []
        for claim in claims:
            cid = claim.get("claim_id", "")
            if cid in verification_map:
                claim["support"] = verification_map[cid].get("support", "unknown")
                claim["verification_note"] = verification_map[cid].get("note", "")
            else:
                claim["support"] = "unverified"
            updated_claims.append(claim)

        return VerificationResult(
            verified_claims=updated_claims,
            tokens_used=tokens,
        )

    except json.JSONDecodeError:
        logger.warning("Verifier returned non-JSON")
        return VerificationResult(verified_claims=claims, tokens_used=tokens)
