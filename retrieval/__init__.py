"""Retrieval layer: deterministic content extraction from known URLs.

Components:
- DirectURLReader:  Protocol-conformant document reader (HTTP-first)
- read_url:         Legacy function interface (still used by research orchestration)
- html_to_markdown: HTML → Markdown extraction
- chunk_text:       Text → passage chunking with heading context

Architecture note:
    DirectURLReader implements the DocumentReader protocol from contracts.py.
    It is a retrieval primitive, NOT a search provider. Known URLs bypass
    discovery entirely and enter the extraction pipeline directly.
"""

from .reader import read_url, ReadResult
from .direct_reader import DirectURLReader
from .html import html_to_markdown
from .chunking import chunk_text, Passage

__all__ = [
    "read_url",
    "ReadResult",
    "DirectURLReader",
    "html_to_markdown",
    "chunk_text",
    "Passage",
]
