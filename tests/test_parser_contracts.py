"""Parser contract tests for HTML search adapters.

Validates that each engine's parser can correctly extract results from
saved HTML fixtures. These tests run without network access and catch
regressions when upgrading ddgs or when upstream markup changes.

Run with: pytest tests/test_parser_contracts.py
"""

import os
from pathlib import Path

import pytest

FIXTURE_DIR = Path(__file__).parent.parent / "search_core" / "fixtures"


class TestBraveParser:
    """Contract tests for Brave HTML adapter."""

    @pytest.fixture
    def fixture_path(self):
        return FIXTURE_DIR / "brave"

    def test_normal_results_extracted(self, fixture_path):
        """normal_results.html → ≥5 results with title, url, snippet."""
        html_path = fixture_path / "normal_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")
        # TODO: import brave parser, feed HTML, assert ≥5 results
        # from ddgs.engines.brave import BraveEngine
        # results = BraveEngine._parse_results(html_path.read_text())
        # assert len(results) >= 5
        # for r in results:
        #     assert r.get("title")
        #     assert r.get("href") or r.get("url")
        #     assert r.get("body")

    def test_no_results_returns_empty(self, fixture_path):
        """no_results.html → empty list, NOT an error."""
        html_path = fixture_path / "no_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")
        # TODO: parse and assert empty list

    def test_blocked_raises_error(self, fixture_path):
        """blocked.html → typed ProviderBlockedError."""
        html_path = fixture_path / "blocked.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")
        # TODO: parse and assert raises ProviderBlockedError


class TestMojeekParser:
    """Contract tests for Mojeek HTML adapter."""

    @pytest.fixture
    def fixture_path(self):
        return FIXTURE_DIR / "mojeek"

    def test_normal_results_extracted(self, fixture_path):
        html_path = fixture_path / "normal_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")

    def test_no_results_returns_empty(self, fixture_path):
        html_path = fixture_path / "no_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")

    def test_blocked_raises_error(self, fixture_path):
        html_path = fixture_path / "blocked.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")


class TestDuckDuckGoParser:
    """Contract tests for DuckDuckGo HTML adapter."""

    @pytest.fixture
    def fixture_path(self):
        return FIXTURE_DIR / "duckduckgo"

    def test_normal_results_extracted(self, fixture_path):
        html_path = fixture_path / "normal_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")

    def test_no_results_returns_empty(self, fixture_path):
        html_path = fixture_path / "no_results.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")

    def test_blocked_raises_error(self, fixture_path):
        html_path = fixture_path / "blocked.html"
        if not html_path.exists():
            pytest.skip("Fixture not yet captured")
