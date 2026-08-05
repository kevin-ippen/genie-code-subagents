"""Unified contracts for search and retrieval.

These protocols define the service boundary between orchestration logic
and the underlying providers. No provider-specific behavior should leak
above this layer.

Architecture:
    SearchProvider  — discovery (find relevant content for a query)
    DocumentReader  — retrieval (extract content from a known URL)

Implementation map:
    SearchProvider
    ├── LocalDeltaProvider       (governed trusted corpus)
    └── HtmlMetasearchProvider   (forked ddgs adapters, no API keys)
        ├── BraveHtmlAdapter
        ├── MojeekHtmlAdapter
        └── DuckDuckGoHtmlAdapter

    DocumentReader
    ├── DirectURLReader          (HTTP-first, known URLs)
    ├── PdfReader                (PDF extraction)
    ├── GitHubReader             (raw/API GitHub content)
    └── BrowserReader            (Playwright escalation)

Routing policy:
    Known URL?
        → DocumentReader

    Discovery question?
        → LocalDeltaProvider (trusted first)
        → assess coverage
        → HtmlMetasearchProvider (discovery/freshness)
        → DocumentReader for selected results

    High-value external source?
        → optional promotion into LocalDeltaProvider corpus
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable
from datetime import datetime


# ---------------------------------------------------------------------------
# Normalized result schemas
# ---------------------------------------------------------------------------

@dataclass
class ProviderDiagnostics:
    """Health/timing info from a single provider execution."""
    provider: str
    status: str  # "ok", "error:<type>", "rate_limited", "timeout", "circuit_open"
    result_count: int = 0
    elapsed_ms: int = 0
    error: Optional[str] = None


@dataclass
class SearchResult:
    """A single search result from any provider."""
    source_id: str           # Stable ID (e.g. "S1", "local:github:repo:chunk")
    rank: int
    title: str
    url: str
    canonical_url: str
    domain: str
    snippet: str             # Max 500 chars
    published_at: Optional[str] = None
    score: Optional[float] = None
    provider: Optional[str] = None
    trust_tier: Optional[str] = None  # "approved", "trusted", None (unknown)
    content_hash: Optional[str] = None


@dataclass
class SearchRequest:
    """Normalized search request accepted by any SearchProvider."""
    query: str
    num_results: int = 10
    freshness: Optional[str] = None   # "24h", "7d", "30d", "1y"
    category: str = "web"             # "web", "news"
    language: str = "en"
    include_domains: Optional[list[str]] = None
    exclude_domains: Optional[list[str]] = None
    source_types: Optional[list[str]] = None  # For local: ["github", "docs", "arxiv"]
    trust_tiers: Optional[list[str]] = None   # For local: ["approved", "trusted"]
    retrieval_mode: str = "hybrid"            # "hybrid", "vector", "keyword"


@dataclass
class ProviderSearchResponse:
    """Response from a single SearchProvider."""
    query: str
    provider: str
    results: list[SearchResult] = field(default_factory=list)
    timing_ms: int = 0
    total_results_available: Optional[int] = None
    diagnostics: list[ProviderDiagnostics] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and len(self.results) > 0


# ---------------------------------------------------------------------------
# Document retrieval schemas
# ---------------------------------------------------------------------------

@dataclass
class Passage:
    """A content passage within a fetched document."""
    passage_id: str
    text: str
    char_offset: int = 0
    heading_context: Optional[str] = None
    content_hash: Optional[str] = None


@dataclass
class ReadRequest:
    """Request to read/extract content from a known URL."""
    url: str
    mode: str = "markdown"         # "markdown", "chunks", "raw"
    max_chars: int = 50_000
    timeout: float = 15.0
    chunk_target_size: int = 800
    chunk_max_size: int = 1200
    chunk_overlap: int = 100
    use_cache: bool = True         # Check conditional headers
    promote_to_corpus: bool = False # Offer to index in local provider


@dataclass
class FetchedDocument:
    """Result from a DocumentReader."""
    document_id: str                    # Content-addressed: sha256 of content
    requested_url: str
    final_url: str                      # After redirects
    canonical_url: str
    title: str
    media_type: str                     # e.g. "text/html", "application/pdf"
    extraction_method: str              # e.g. "html_parser", "pdf", "raw", "browser"
    content_markdown: str               # Full extracted text as Markdown
    passages: list[Passage] = field(default_factory=list)
    retrieved_at: Optional[str] = None
    content_hash: Optional[str] = None
    meta_description: Optional[str] = None
    status_code: Optional[int] = None
    etag: Optional[str] = None         # For conditional re-fetch
    last_modified: Optional[str] = None # For conditional re-fetch
    warnings: list[str] = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.content_markdown)


# ---------------------------------------------------------------------------
# Protocol definitions
# ---------------------------------------------------------------------------

@runtime_checkable
class SearchProvider(Protocol):
    """Discovery: find relevant content for a query."""

    @property
    def name(self) -> str:
        """Provider identifier (e.g. 'html_metasearch', 'local_delta')."""
        ...

    @property
    def is_available(self) -> bool:
        """Whether this provider is currently operational."""
        ...

    async def search(self, request: SearchRequest) -> ProviderSearchResponse:
        """Execute a search and return normalized results."""
        ...

    async def health_check(self) -> bool:
        """Quick connectivity/readiness check."""
        ...


@runtime_checkable
class DocumentReader(Protocol):
    """Retrieval: extract content from a known URL."""

    @property
    def name(self) -> str:
        """Reader identifier (e.g. 'direct_url', 'pdf', 'github')."""
        ...

    async def read(self, request: ReadRequest) -> FetchedDocument:
        """Fetch and extract content from the given URL."""
        ...

    def can_handle(self, url: str, media_type: Optional[str] = None) -> bool:
        """Whether this reader can process the given URL/media type."""
        ...
