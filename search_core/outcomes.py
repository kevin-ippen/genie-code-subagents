"""Typed search outcomes.

Critical distinction: an empty result list can mean several things.
These outcomes make the difference observable and actionable.

SearchOutcome.OK            — results extracted successfully
SearchOutcome.NO_RESULTS    — the engine genuinely returned zero matches
SearchOutcome.RATE_LIMITED   — 429 or equivalent signal
SearchOutcome.BLOCKED       — consent wall, CAPTCHA, bot challenge
SearchOutcome.PARSER_DRIFT  — HTML changed, parser returned nothing from a non-empty page
SearchOutcome.UPSTREAM_ERROR — 5xx, connection refused, DNS failure, timeout
"""

from enum import Enum


class SearchOutcome(str, Enum):
    """Typed outcome for a single provider search execution."""
    OK = "ok"
    NO_RESULTS = "no_results"
    RATE_LIMITED = "rate_limited"
    BLOCKED = "blocked"
    PARSER_DRIFT = "parser_drift"
    UPSTREAM_ERROR = "upstream_error"


# Challenge page detection heuristics (per-engine)
_CHALLENGE_SIGNALS = {
    "brave": [
        "captcha",
        "cf-browser-verification",
        "challenge-platform",
        "just a moment",
    ],
    "mojeek": [
        "blocked",
        "rate limit",
        "too many requests",
    ],
    "duckduckgo": [
        "captcha",
        "blocked",
        "something went wrong",
        "duckduckgo.com/post2.html",
    ],
    "_generic": [
        "access denied",
        "403 forbidden",
        "verify you are human",
        "one more step",
        "checking your browser",
    ],
}


def classify_outcome(
    results: list,
    status_code: int | None,
    raw_body: str | None,
    backend: str,
) -> SearchOutcome:
    """Classify search execution into a typed outcome.

    Args:
        results: Parsed result list from the adapter.
        status_code: HTTP status code (None if connection failed).
        raw_body: Raw HTML response body (for challenge detection).
        backend: Engine name (brave, mojeek, duckduckgo).

    Returns:
        SearchOutcome enum value.
    """
    # Connection-level failures
    if status_code is None:
        return SearchOutcome.UPSTREAM_ERROR

    # Rate limiting
    if status_code == 429:
        return SearchOutcome.RATE_LIMITED
    if status_code == 403 and raw_body and _is_challenge_page(raw_body, backend):
        return SearchOutcome.BLOCKED

    # Upstream 5xx
    if status_code >= 500:
        return SearchOutcome.UPSTREAM_ERROR

    # Got a 200 but no results — distinguish no-results from parser drift
    if status_code == 200 and not results:
        if raw_body and _is_challenge_page(raw_body, backend):
            return SearchOutcome.BLOCKED
        if raw_body and len(raw_body) > 2000:
            # Page has substantial content but parser extracted nothing
            return SearchOutcome.PARSER_DRIFT
        # Genuinely empty
        return SearchOutcome.NO_RESULTS

    # Success
    if results:
        return SearchOutcome.OK

    return SearchOutcome.UPSTREAM_ERROR


def _is_challenge_page(body: str, backend: str) -> bool:
    """Detect if an HTML response is a challenge/consent/CAPTCHA page."""
    body_lower = body.lower()

    # Engine-specific signals
    signals = _CHALLENGE_SIGNALS.get(backend, []) + _CHALLENGE_SIGNALS["_generic"]
    matches = sum(1 for sig in signals if sig in body_lower)

    # 2+ signals = high confidence it's a challenge
    return matches >= 2
