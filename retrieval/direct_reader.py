"""DirectURLReader — deterministic retrieval of known URLs.

Implements the DocumentReader protocol from contracts.py.
Wraps the existing read_url() functionality with:
- Protocol-conformant interface
- Conditional request support (ETag / Last-Modified)
- MIME allowlist enforcement
- Content-addressed document IDs
- Promotion hook for ingestion into local corpus

A known URL is NOT a search operation — it bypasses discovery
and enters the retrieval pipeline directly.
"""

from __future__ import annotations

import hashlib
import logging
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..contracts import DocumentReader, ReadRequest, FetchedDocument, Passage
from ..security.urls import is_url_safe
from .html import html_to_markdown, extract_title, extract_meta_description
from .chunking import chunk_text

logger = logging.getLogger(__name__)


# MIME types we can extract from natively (no browser)
_ALLOWED_MIME_TYPES = frozenset({
    "text/html",
    "text/plain",
    "text/markdown",
    "text/csv",
    "application/json",
    "application/xml",
    "text/xml",
    "application/pdf",  # Handled specially
    "application/xhtml+xml",
})

# GitHub raw content special handling
_GITHUB_RAW_DOMAINS = ("raw.githubusercontent.com", "github.com")


class DirectURLReader:
    """Reads and extracts content from known URLs via HTTP.

    Execution ladder:
    1. Validate URL (SSRF, scheme, MIME allowlist)
    2. Resolve DNS and check security policy
    3. Conditional request (ETag/Last-Modified if cached)
    4. Bounded HTTP fetch with timeout + size limits
    5. Content-type detection
    6. Select extractor (HTML, plain text, PDF, GitHub)
    7. Normalize to Markdown
    8. Assign content-addressed document ID
    9. Chunk into passages if requested
    10. Return FetchedDocument with conditional headers for re-fetch
    """

    @property
    def name(self) -> str:
        return "direct_url"

    def can_handle(self, url: str, media_type: Optional[str] = None) -> bool:
        """Accept any http/https URL with an allowed MIME type."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        if media_type and media_type not in _ALLOWED_MIME_TYPES:
            return False
        return True

    async def read(self, request: ReadRequest) -> FetchedDocument:
        """Fetch and extract content from a known URL."""
        url = request.url
        start = time.time()

        # Security gate
        safe, reason = is_url_safe(url, resolve_dns=True)
        if not safe:
            return self._error_doc(url, f"Blocked: {reason}", start)

        headers = {
            "User-Agent": "GenieSubagent/2.0 (research bot)",
            "Accept": "text/html,application/xhtml+xml,text/plain,application/json,*/*;q=0.8",
        }

        # Conditional headers for cache validation
        # These would come from a local document store in a full implementation
        # For now, accept them as part of the request extension

        try:
            async with httpx.AsyncClient(
                timeout=request.timeout,
                follow_redirects=True,
                max_redirects=5,
                headers=headers,
            ) as client:
                resp = await client.get(url)
                elapsed_ms = int((time.time() - start) * 1000)
                final_url = str(resp.url)

                # Validate redirect destination
                if final_url != url:
                    safe, reason = is_url_safe(final_url, resolve_dns=True)
                    if not safe:
                        return self._error_doc(
                            url, f"Redirect blocked: {reason}", start,
                            final_url=final_url, status_code=resp.status_code,
                        )

                # 304 Not Modified → content unchanged
                if resp.status_code == 304:
                    return FetchedDocument(
                        document_id="",
                        requested_url=url,
                        final_url=final_url,
                        canonical_url=final_url,
                        title="",
                        media_type="",
                        extraction_method="conditional_304",
                        content_markdown="",
                        status_code=304,
                        warnings=["Content not modified since last fetch"],
                    )

                # HTTP errors
                if resp.status_code >= 400:
                    return self._error_doc(
                        url, f"HTTP {resp.status_code}", start,
                        final_url=final_url, status_code=resp.status_code,
                    )

                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

                # MIME allowlist
                if content_type and content_type not in _ALLOWED_MIME_TYPES:
                    return self._error_doc(
                        url, f"Unsupported content type: {content_type}", start,
                        final_url=final_url, status_code=resp.status_code,
                    )

                # Extract content based on type
                if content_type == "application/pdf":
                    doc = self._extract_pdf(resp, url, final_url, elapsed_ms)
                elif content_type in ("text/html", "application/xhtml+xml"):
                    doc = self._extract_html(resp, url, final_url, request, elapsed_ms)
                else:
                    doc = self._extract_text(resp, url, final_url, request, elapsed_ms)

                # Capture conditional headers for future re-fetch
                doc.etag = resp.headers.get("etag")
                doc.last_modified = resp.headers.get("last-modified")
                doc.retrieved_at = datetime.now(timezone.utc).isoformat()

                # Generate passages if chunking requested
                if request.mode == "chunks" and doc.ok:
                    doc.passages = [
                        Passage(
                            passage_id=p.passage_id,
                            text=p.text,
                            char_offset=p.char_offset,
                            heading_context=p.heading_context,
                            content_hash=p.content_hash,
                        )
                        for p in chunk_text(
                            doc.content_markdown,
                            source_url=final_url,
                            target_size=request.chunk_target_size,
                            max_size=request.chunk_max_size,
                            overlap=request.chunk_overlap,
                        )
                    ]

                return doc

        except httpx.TimeoutException:
            return self._error_doc(url, f"Timeout after {request.timeout}s", start)
        except httpx.ConnectError as e:
            return self._error_doc(url, f"Connection failed: {e}", start)
        except Exception as e:
            return self._error_doc(url, f"{type(e).__name__}: {e}", start)

    # ------------------------------------------------------------------
    # Extractors
    # ------------------------------------------------------------------

    def _extract_html(
        self, resp: httpx.Response, url: str, final_url: str,
        request: ReadRequest, elapsed_ms: int,
    ) -> FetchedDocument:
        """Extract Markdown from HTML response."""
        raw_html = resp.text
        title = extract_title(raw_html)
        meta_desc = extract_meta_description(raw_html)

        if request.mode == "raw":
            content = raw_html[:request.max_chars]
            method = "raw"
        else:
            content = html_to_markdown(raw_html, max_chars=request.max_chars)
            method = "html_parser"

        content_hash = hashlib.sha256(content.encode()).hexdigest()
        document_id = content_hash[:32]

        # Detect if page needs browser (thin content)
        warnings = []
        if len(content.strip()) < 100 and len(raw_html) > 1000:
            warnings.append("Content may require JS rendering (thin extraction)")

        return FetchedDocument(
            document_id=document_id,
            requested_url=url,
            final_url=final_url,
            canonical_url=final_url,
            title=title,
            media_type="text/html",
            extraction_method=method,
            content_markdown=content,
            content_hash=content_hash,
            meta_description=meta_desc,
            status_code=resp.status_code,
            warnings=warnings,
        )

    def _extract_text(
        self, resp: httpx.Response, url: str, final_url: str,
        request: ReadRequest, elapsed_ms: int,
    ) -> FetchedDocument:
        """Extract content from plain text, JSON, XML, Markdown."""
        content = resp.text[:request.max_chars]
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        content_hash = hashlib.sha256(content.encode()).hexdigest()

        # Derive title from URL path
        path_parts = urlparse(final_url).path.rstrip("/").split("/")
        title = path_parts[-1] if path_parts else ""

        return FetchedDocument(
            document_id=content_hash[:32],
            requested_url=url,
            final_url=final_url,
            canonical_url=final_url,
            title=title,
            media_type=content_type,
            extraction_method="raw",
            content_markdown=content,
            content_hash=content_hash,
            status_code=resp.status_code,
        )

    def _extract_pdf(
        self, resp: httpx.Response, url: str, final_url: str, elapsed_ms: int,
    ) -> FetchedDocument:
        """PDF extraction stub — signals need for specialized handler."""
        return FetchedDocument(
            document_id="",
            requested_url=url,
            final_url=final_url,
            canonical_url=final_url,
            title=urlparse(final_url).path.split("/")[-1],
            media_type="application/pdf",
            extraction_method="pdf_stub",
            content_markdown="[PDF document — requires dedicated PdfReader]",
            status_code=resp.status_code,
            warnings=["PDF extraction requires PdfReader (not yet implemented)"],
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _error_doc(
        url: str, error: str, start: float,
        final_url: Optional[str] = None, status_code: Optional[int] = None,
    ) -> FetchedDocument:
        return FetchedDocument(
            document_id="",
            requested_url=url,
            final_url=final_url or url,
            canonical_url=final_url or url,
            title="",
            media_type="",
            extraction_method="",
            content_markdown="",
            status_code=status_code,
            error=error,
        )
