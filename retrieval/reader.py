"""Universal page reader.

Execution ladder:
1. HTTP fetch (fast, no JS)
2. Content-type detection
3. HTML → Markdown extraction
4. PDF → text extraction (future)
5. Playwright only when JS-required heuristic triggers

This replaces the current approach of opening every page in a browser.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

import httpx

from ..security.urls import is_url_safe
from .html import html_to_markdown, extract_title, extract_meta_description
from .chunking import chunk_text, Passage

logger = logging.getLogger(__name__)

# Content types we can handle natively (no browser needed)
_TEXT_CONTENT_TYPES = ("text/html", "text/plain", "text/markdown", "application/json", "application/xml")

# Heuristics for JS-required pages (triggers browser escalation)
_JS_REQUIRED_SIGNALS = [
    "__NEXT_DATA__",  # Next.js SSR hydration
    "window.__remixContext",  # Remix
    "id=\"__nuxt\"",  # Nuxt
    "<noscript",  # Often means JS is needed
    "<div id=\"app\"></div>",  # SPA shell
    "<div id=\"root\"></div>",  # React SPA shell
]


@dataclass
class ReadResult:
    """Result of reading a URL."""
    url: str
    final_url: str  # After redirects
    title: str = ""
    content: str = ""  # Markdown text
    meta_description: str = ""
    content_type: str = ""
    content_hash: str = ""
    passages: list[Passage] = field(default_factory=list)
    method: str = "http"  # http | browser | pdf
    timing_ms: int = 0
    status_code: int = 0
    error: Optional[str] = None
    needs_browser: bool = False  # Signal for caller to escalate

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.content)


async def read_url(
    url: str,
    *,
    mode: str = "markdown",  # markdown | text | chunks | raw
    max_chars: int = 50_000,
    timeout: float = 15.0,
    user_agent: str = "GenieSubagent/2.0 (research bot)",
) -> ReadResult:
    """Read a URL and return extracted content.

    This is the primary entry point for the retrieval layer.
    It tries HTTP first, extracts content, and signals if browser
    escalation is needed.

    Args:
        url: The URL to read.
        mode: Output format (markdown, text, chunks, raw).
        max_chars: Maximum content length.
        timeout: HTTP timeout in seconds.
        user_agent: User-Agent header.

    Returns:
        ReadResult with extracted content or error.
    """
    # Security gate
    safe, reason = is_url_safe(url, resolve_dns=True)
    if not safe:
        return ReadResult(url=url, final_url=url, error=f"Blocked: {reason}")

    start = time.time()

    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": user_agent},
        ) as client:
            resp = await client.get(url)
            elapsed_ms = int((time.time() - start) * 1000)
            final_url = str(resp.url)

            # Validate redirect destination
            if final_url != url:
                safe, reason = is_url_safe(final_url, resolve_dns=True)
                if not safe:
                    return ReadResult(
                        url=url, final_url=final_url,
                        error=f"Redirect blocked: {reason}",
                        timing_ms=elapsed_ms,
                    )

            content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()

            # Handle non-success status
            if resp.status_code >= 400:
                return ReadResult(
                    url=url, final_url=final_url,
                    status_code=resp.status_code,
                    content_type=content_type,
                    timing_ms=elapsed_ms,
                    error=f"HTTP {resp.status_code}",
                )

            # Handle PDF
            if content_type == "application/pdf":
                # For now, signal that we need specialized handling
                return ReadResult(
                    url=url, final_url=final_url,
                    status_code=resp.status_code,
                    content_type=content_type,
                    timing_ms=elapsed_ms,
                    content="[PDF document - extraction not yet implemented]",
                    method="pdf",
                )

            # Handle non-text content
            if not any(content_type.startswith(ct) for ct in _TEXT_CONTENT_TYPES):
                return ReadResult(
                    url=url, final_url=final_url,
                    status_code=resp.status_code,
                    content_type=content_type,
                    timing_ms=elapsed_ms,
                    error=f"Unsupported content type: {content_type}",
                )

            raw_text = resp.text

            # HTML extraction
            if content_type == "text/html":
                title = extract_title(raw_text)
                meta_desc = extract_meta_description(raw_text)

                # Check if page likely needs JS rendering
                needs_browser = _detect_js_required(raw_text)

                if mode == "raw":
                    content = raw_text[:max_chars]
                else:
                    content = html_to_markdown(raw_text, max_chars=max_chars)

                # If extraction yielded very little, signal browser escalation
                if len(content.strip()) < 100 and len(raw_text) > 1000:
                    needs_browser = True

                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

                result = ReadResult(
                    url=url,
                    final_url=final_url,
                    title=title,
                    content=content,
                    meta_description=meta_desc,
                    content_type=content_type,
                    content_hash=content_hash,
                    method="http",
                    timing_ms=elapsed_ms,
                    status_code=resp.status_code,
                    needs_browser=needs_browser,
                )

                # Generate passages if requested
                if mode == "chunks":
                    result.passages = chunk_text(content, source_url=final_url)

                return result

            else:
                # Plain text, JSON, XML — return as-is
                content = raw_text[:max_chars]
                content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

                return ReadResult(
                    url=url,
                    final_url=final_url,
                    title=urlparse(final_url).path.split("/")[-1],
                    content=content,
                    content_type=content_type,
                    content_hash=content_hash,
                    method="http",
                    timing_ms=elapsed_ms,
                    status_code=resp.status_code,
                )

    except httpx.TimeoutException:
        return ReadResult(
            url=url, final_url=url,
            timing_ms=int((time.time() - start) * 1000),
            error=f"Timeout after {timeout}s",
        )
    except httpx.ConnectError as e:
        return ReadResult(
            url=url, final_url=url,
            timing_ms=int((time.time() - start) * 1000),
            error=f"Connection failed: {e}",
        )
    except Exception as e:
        return ReadResult(
            url=url, final_url=url,
            timing_ms=int((time.time() - start) * 1000),
            error=f"{type(e).__name__}: {e}",
        )


def _detect_js_required(html: str) -> bool:
    """Heuristic: does this page need JS to render content?"""
    # Check for SPA signals
    for signal in _JS_REQUIRED_SIGNALS:
        if signal in html:
            return True

    # If body is very short relative to total HTML, likely needs JS
    import re
    body_match = re.search(r"<body[^>]*>(.*?)</body>", html, re.DOTALL | re.IGNORECASE)
    if body_match:
        body = body_match.group(1)
        # Strip tags to see actual text
        text = re.sub(r"<[^>]+>", "", body)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) < 50 and len(html) > 5000:
            return True

    return False
