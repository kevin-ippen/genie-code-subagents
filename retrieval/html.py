"""HTML-to-Markdown extraction.

Converts raw HTML into clean, readable Markdown text suitable for
LLM consumption. Strips navigation, ads, scripts, and boilerplate.

Uses a lightweight built-in approach (no trafilatura dependency)
with heuristics for main-content extraction.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional


# Tags whose content should be completely ignored
_SKIP_TAGS: frozenset[str] = frozenset([
    "script", "style", "noscript", "nav", "header", "footer",
    "aside", "iframe", "svg", "form", "button", "select",
    "input", "textarea",
])

# Block-level tags that get newlines
_BLOCK_TAGS: frozenset[str] = frozenset([
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
    "li", "blockquote", "pre", "code", "table", "tr",
    "section", "article", "main", "br", "hr",
])

# Heading tags for Markdown formatting
_HEADING_MAP: dict[str, str] = {
    "h1": "# ", "h2": "## ", "h3": "### ",
    "h4": "#### ", "h5": "##### ", "h6": "###### ",
}


class _ContentExtractor(HTMLParser):
    """Simple HTML parser that extracts readable text as Markdown."""

    def __init__(self):
        super().__init__()
        self._output: list[str] = []
        self._skip_depth: int = 0
        self._tag_stack: list[str] = []
        self._in_pre: bool = False
        self._link_href: Optional[str] = None
        self._link_text: list[str] = []
        self._list_depth: int = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]):
        tag = tag.lower()
        self._tag_stack.append(tag)

        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return

        if self._skip_depth > 0:
            return

        if tag in _HEADING_MAP:
            self._output.append("\n\n" + _HEADING_MAP[tag])
        elif tag == "p":
            self._output.append("\n\n")
        elif tag == "br":
            self._output.append("\n")
        elif tag == "hr":
            self._output.append("\n\n---\n\n")
        elif tag in ("ul", "ol"):
            self._list_depth += 1
            self._output.append("\n")
        elif tag == "li":
            indent = "  " * max(0, self._list_depth - 1)
            self._output.append(f"\n{indent}* ")
        elif tag == "pre":
            self._in_pre = True
            self._output.append("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self._output.append("`")
        elif tag == "blockquote":
            self._output.append("\n\n> ")
        elif tag == "a":
            attrs_dict = dict(attrs)
            self._link_href = attrs_dict.get("href")
            self._link_text = []
        elif tag in ("strong", "b"):
            self._output.append("**")
        elif tag in ("em", "i"):
            self._output.append("*")
        elif tag == "div":
            self._output.append("\n")

    def handle_endtag(self, tag: str):
        tag = tag.lower()

        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()

        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return

        if self._skip_depth > 0:
            return

        if tag in _HEADING_MAP:
            self._output.append("\n")
        elif tag == "pre":
            self._in_pre = False
            self._output.append("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self._output.append("`")
        elif tag in ("ul", "ol"):
            self._list_depth = max(0, self._list_depth - 1)
        elif tag == "a":
            text = "".join(self._link_text).strip()
            if self._link_href and text:
                self._output.append(f"[{text}]({self._link_href})")
            elif text:
                self._output.append(text)
            self._link_href = None
            self._link_text = []
        elif tag in ("strong", "b"):
            self._output.append("**")
        elif tag in ("em", "i"):
            self._output.append("*")

    def handle_data(self, data: str):
        if self._skip_depth > 0:
            return

        if self._link_href is not None:
            self._link_text.append(data)
            return

        if not self._in_pre:
            # Collapse whitespace
            data = re.sub(r"\s+", " ", data)

        self._output.append(data)

    def get_markdown(self) -> str:
        text = "".join(self._output)
        # Clean up excessive newlines
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def html_to_markdown(html: str, *, max_chars: int = 50_000) -> str:
    """Convert HTML to readable Markdown.

    Strips scripts, styles, navigation, and boilerplate.
    Preserves headings, links, lists, code blocks, and emphasis.

    Args:
        html: Raw HTML string.
        max_chars: Maximum output length.

    Returns:
        Clean Markdown text.
    """
    extractor = _ContentExtractor()
    try:
        extractor.feed(html)
    except Exception:
        # Fall back to regex-based extraction on parser failure
        return _fallback_extract(html, max_chars)

    result = extractor.get_markdown()
    if len(result) > max_chars:
        result = result[:max_chars] + "\n\n[...truncated]"
    return result


def extract_title(html: str) -> str:
    """Extract the <title> from HTML."""
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return ""


def extract_meta_description(html: str) -> str:
    """Extract meta description from HTML."""
    match = re.search(
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\'>]*)["\']',
        html, re.IGNORECASE,
    )
    if not match:
        match = re.search(
            r'<meta[^>]+content=["\']([^"\'>]*)["\'][^>]+name=["\']description["\']',
            html, re.IGNORECASE,
        )
    return match.group(1).strip() if match else ""


def _fallback_extract(html: str, max_chars: int) -> str:
    """Regex-based fallback when HTMLParser fails."""
    # Remove script/style blocks
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", html, flags=re.IGNORECASE | re.DOTALL)
    # Remove all tags
    text = re.sub(r"<[^>]+>", " ", text)
    # Decode common entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]
