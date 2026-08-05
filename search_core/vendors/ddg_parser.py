"""Vendored DuckDuckGo HTML parser.

Extracts search results from html.duckduckgo.com/html/ responses.
Written against fixture captured 2026-08-05 (ddgs 9.14.4 adapter was broken).

This parser is intentionally simple and fixture-tested:
- Regex-based (no lxml/BS4 dependency)
- Extracts: title, URL (unwrapped from DDG redirect), snippet
- Handles DDG's uddg= redirect wrapper
- Falls back gracefully on markup changes

When ddgs upstream fixes their DuckDuckGo adapter, this can be retired.
Until then, it serves as the fallback parser for EMERGENCY tier.
"""

from __future__ import annotations

import re
from html import unescape
from urllib.parse import urlparse, unquote, parse_qs


def parse_ddg_html(html: str, max_results: int = 10) -> list[dict]:
    """Parse DuckDuckGo HTML endpoint response into structured results.

    Args:
        html: Raw HTML from POST to https://html.duckduckgo.com/html/
        max_results: Maximum results to extract.

    Returns:
        List of dicts with keys: title, href, body
        Empty list if parsing fails (NOT an exception — caller
        should check SearchOutcome for classification).
    """
    results = []

    # <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=...">Title</a>
    link_pattern = re.compile(
        r'<a[^>]*class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        re.DOTALL,
    )
    # <a class="result__snippet" href="...">Snippet text with <b>highlights</b></a>
    snippet_pattern = re.compile(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        re.DOTALL,
    )

    links = link_pattern.findall(html)
    snippets = snippet_pattern.findall(html)

    for i, (raw_url, raw_title) in enumerate(links[:max_results]):
        url = _extract_url(raw_url)
        if not url or not url.startswith("http"):
            continue

        title = _strip_tags(raw_title)
        if not title:
            continue

        snippet = ""
        if i < len(snippets):
            snippet = _strip_tags(snippets[i])

        results.append({
            "title": title,
            "href": url,
            "body": snippet[:500],
        })

    return results


def _extract_url(raw_url: str) -> str:
    """Extract actual URL from DDG redirect wrapper.

    DDG wraps as: //duckduckgo.com/l/?uddg=<percent-encoded-url>&rut=...
    """
    if "uddg=" in raw_url:
        try:
            parsed = parse_qs(urlparse(raw_url).query)
            if "uddg" in parsed:
                return unquote(parsed["uddg"][0])
        except Exception:
            pass

    url = raw_url
    if url.startswith("//"):
        url = "https:" + url
    return unquote(url)


def _strip_tags(text: str) -> str:
    """Remove HTML tags and decode entities."""
    text = re.sub(r"<[^>]+>", "", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()
