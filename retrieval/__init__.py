"""Retrieval: read pages, extract content, chunk for LLM consumption."""

from .reader import read_url, ReadResult
from .html import html_to_markdown
from .chunking import chunk_text, Passage

__all__ = ["read_url", "ReadResult", "html_to_markdown", "chunk_text", "Passage"]
