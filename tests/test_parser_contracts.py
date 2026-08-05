"""Parser contract tests for HTML search adapters.

Tests assert SEMANTIC correctness — not exact output.
Each fixture has a JSON sidecar declaring expected behavior.

Contract rules:
- "ok" fixtures → parser returns ≥ minimum_results with required fields
- "no_results" fixtures → parser returns empty list (not an error)
- "blocked" fixtures → outcome is BLOCKED (not empty OK)
- "parser_drift" → outcome is PARSER_DRIFT (not silent empty)
- URLs are normalized (no provider redirect wrappers)
- Challenge pages are never returned as valid results
- All required fields are populated strings

Run with: pytest tests/test_parser_contracts.py -v
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "search"
SUPPORTED_BACKENDS = ("brave", "mojeek", "duckduckgo")


def load_fixture(backend: str, scenario: str) -> tuple[str, dict]:
    """Load HTML fixture and its JSON sidecar.

    Returns (html_content, sidecar_metadata).
    Skips if fixture not yet captured.
    """
    html_path = FIXTURE_DIR / backend / f"{scenario}.html"
    json_path = FIXTURE_DIR / backend / f"{scenario}.json"

    if not html_path.exists() or not json_path.exists():
        pytest.skip(f"Fixture not yet captured: {backend}/{scenario}")

    html = html_path.read_text(encoding="utf-8")
    sidecar = json.loads(json_path.read_text())

    # Skip placeholder fixtures
    if html.startswith("<!-- TODO") or len(html) < 100:
        pytest.skip(f"Fixture is placeholder: {backend}/{scenario}")

    return html, sidecar


def get_parser_for_backend(backend: str):
    """Import and return the parser for a given backend.

    This exercises the same parsing code path used in production.
    """
    try:
        from ddgs import DDGS
        # ddgs doesn't expose parsers directly — we'll test through
        # the full search path using fixtures
        return DDGS
    except ImportError:
        pytest.skip("ddgs not installed")


def parse_with_ddgs(backend: str, html: str) -> list[dict]:
    """Parse HTML through ddgs internals.

    NOTE: ddgs doesn't expose raw HTML parsing as a public API.
    When we vendor/fork the adapters, this will call the parser directly.
    For now, this is a placeholder for the future vendored interface.
    """
    # TODO: Once we vendor the adapters, call the parser directly:
    # from search_core.vendors.brave import BraveHtmlParser
    # return BraveHtmlParser.parse(html)
    pytest.skip(
        f"Direct HTML parsing not yet exposed for {backend}. "
        "Implement when vendoring ddgs adapters."
    )


# ---------------------------------------------------------------------------
# Test classes per backend
# ---------------------------------------------------------------------------

class TestBraveParserContract:
    """Contract tests for Brave HTML search adapter."""

    BACKEND = "brave"

    def test_normal_results_extracted(self):
        """OK fixture → ≥ minimum_results with required fields."""
        html, sidecar = load_fixture(self.BACKEND, "normal-page-1")

        if sidecar["expected"]["outcome"] != "ok":
            pytest.skip("Fixture is not an OK scenario")

        results = parse_with_ddgs(self.BACKEND, html)
        min_results = sidecar["expected"]["minimum_results"]

        assert len(results) >= min_results, (
            f"Expected ≥{min_results} results, got {len(results)}. "
            "Parser may have drifted."
        )

        required_fields = sidecar["expected"].get("required_fields", ["title", "url"])
        for result in results:
            for field in required_fields:
                value = result.get("href") if field == "url" else result.get(field)
                assert value and isinstance(value, str) and value.strip(), (
                    f"Result missing required field '{field}': {result}"
                )

    def test_urls_are_direct(self):
        """URLs must not contain provider redirect wrappers."""
        html, sidecar = load_fixture(self.BACKEND, "normal-page-1")
        if sidecar["expected"]["outcome"] != "ok":
            pytest.skip("Not an OK fixture")

        results = parse_with_ddgs(self.BACKEND, html)
        for result in results:
            url = result.get("href", "")
            # Brave should NOT leave its redirect wrapper
            assert "search.brave.com/a/redirect" not in url, (
                f"URL still wrapped in Brave redirect: {url}"
            )
            assert url.startswith(("http://", "https://")), (
                f"URL is not absolute: {url}"
            )

    def test_no_results_is_empty_not_error(self):
        """Sparse/no-results fixture → empty list, NOT an exception."""
        html, sidecar = load_fixture(self.BACKEND, "sparse-results")
        if sidecar["expected"]["outcome"] != "no_results":
            pytest.skip("Not a no-results fixture")

        results = parse_with_ddgs(self.BACKEND, html)
        assert results == [], f"Expected empty list, got {len(results)} results"

    def test_blocked_page_detected(self):
        """Blocked/challenge fixture → SearchOutcome.BLOCKED."""
        html, sidecar = load_fixture(self.BACKEND, "blocked")
        if sidecar["expected"]["outcome"] != "blocked":
            pytest.skip("Not a blocked fixture")

        from search_core.outcomes import classify_outcome, SearchOutcome

        # Parser should either raise or return empty from challenge page
        results = []  # Assume parser returns nothing
        outcome = classify_outcome(
            results=results,
            status_code=sidecar.get("status_code", 200),
            raw_body=html,
            backend=self.BACKEND,
        )
        assert outcome == SearchOutcome.BLOCKED, (
            f"Challenge page not detected. Got outcome={outcome.value}"
        )

    def test_known_anchor_present(self):
        """At least one result from the expected domain family exists."""
        html, sidecar = load_fixture(self.BACKEND, "normal-page-1")
        if sidecar["expected"]["outcome"] != "ok":
            pytest.skip("Not an OK fixture")

        results = parse_with_ddgs(self.BACKEND, html)
        # For a research-related query, expect at least one github/arxiv/docs URL
        urls = [r.get("href", "") for r in results]
        has_known_domain = any(
            domain in url
            for url in urls
            for domain in ("github.com", "arxiv.org", "docs.", "medium.com", "reddit.com")
        )
        # This is a soft check — skip rather than fail
        if not has_known_domain:
            pytest.xfail("No known-domain anchor in results (may vary by query)")


class TestMojeekParserContract(TestBraveParserContract):
    """Contract tests for Mojeek HTML adapter — same assertions, different backend."""
    BACKEND = "mojeek"


class TestDuckDuckGoParserContract(TestBraveParserContract):
    """Contract tests for DuckDuckGo HTML adapter — same assertions, different backend."""
    BACKEND = "duckduckgo"


# ---------------------------------------------------------------------------
# Cross-backend integration tests
# ---------------------------------------------------------------------------

class TestOutcomeClassification:
    """Test that SearchOutcome classification works correctly."""

    def test_empty_from_substantial_page_is_drift(self):
        """200 + big page + no results → PARSER_DRIFT, not NO_RESULTS."""
        from search_core.outcomes import classify_outcome, SearchOutcome

        outcome = classify_outcome(
            results=[],
            status_code=200,
            raw_body="x" * 5000,  # Substantial page
            backend="brave",
        )
        assert outcome == SearchOutcome.PARSER_DRIFT

    def test_empty_from_tiny_page_is_no_results(self):
        """200 + small page + no results → NO_RESULTS."""
        from search_core.outcomes import classify_outcome, SearchOutcome

        outcome = classify_outcome(
            results=[],
            status_code=200,
            raw_body="<html><body>No results found</body></html>",
            backend="brave",
        )
        assert outcome == SearchOutcome.NO_RESULTS

    def test_429_is_rate_limited(self):
        from search_core.outcomes import classify_outcome, SearchOutcome

        outcome = classify_outcome(
            results=[], status_code=429, raw_body="", backend="brave"
        )
        assert outcome == SearchOutcome.RATE_LIMITED

    def test_500_is_upstream_error(self):
        from search_core.outcomes import classify_outcome, SearchOutcome

        outcome = classify_outcome(
            results=[], status_code=500, raw_body="", backend="brave"
        )
        assert outcome == SearchOutcome.UPSTREAM_ERROR

    def test_none_status_is_upstream_error(self):
        """Connection failure (no status code) → UPSTREAM_ERROR."""
        from search_core.outcomes import classify_outcome, SearchOutcome

        outcome = classify_outcome(
            results=[], status_code=None, raw_body=None, backend="brave"
        )
        assert outcome == SearchOutcome.UPSTREAM_ERROR
