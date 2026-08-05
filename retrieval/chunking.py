"""Content chunking and passage identification.

Splits extracted content into citable passages with stable IDs.
Passages are the unit of evidence for the research synthesizer.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class Passage:
    """A citable unit of content from a source page."""
    passage_id: str  # Stable ID: "{source_id}-P{n}" e.g. "S3-P7"
    source_url: str
    text: str
    char_offset: int  # Start position in the full document
    heading_context: Optional[str] = None  # Nearest heading above this passage
    content_hash: str = ""  # For dedup across sources

    def __post_init__(self):
        if not self.content_hash:
            self.content_hash = hashlib.sha256(self.text.encode()).hexdigest()[:12]


def chunk_text(
    text: str,
    *,
    source_url: str = "",
    source_id: str = "S0",
    target_size: int = 800,
    max_size: int = 1200,
    overlap: int = 100,
) -> list[Passage]:
    """Split text into passages of approximately target_size characters.

    Strategy:
    1. Split on paragraph boundaries (double newline)
    2. If a paragraph exceeds max_size, split on sentence boundaries
    3. Merge small consecutive paragraphs up to target_size
    4. Track heading context for each passage

    Args:
        text: The full document text (Markdown).
        source_url: URL of the source document.
        source_id: Source identifier for passage IDs.
        target_size: Preferred passage size in characters.
        max_size: Maximum passage size before forced split.
        overlap: Character overlap between adjacent passages.

    Returns:
        List of Passage objects with stable IDs.
    """
    if not text.strip():
        return []

    # Split into paragraphs
    paragraphs = re.split(r"\n\n+", text)

    # Track headings for context
    current_heading: Optional[str] = None
    chunks: list[tuple[str, Optional[str], int]] = []  # (text, heading, char_offset)
    current_chunk = ""
    current_offset = 0
    running_offset = 0

    for para in paragraphs:
        para = para.strip()
        if not para:
            running_offset += 2  # Account for removed newlines
            continue

        # Detect headings
        heading_match = re.match(r"^(#{1,6})\s+(.+)$", para)
        if heading_match:
            # Flush current chunk before heading
            if current_chunk.strip():
                chunks.append((current_chunk.strip(), current_heading, current_offset))
                current_chunk = ""
            current_heading = heading_match.group(2).strip()
            current_offset = running_offset
            running_offset += len(para) + 2
            continue

        # Check if adding this paragraph exceeds target
        if current_chunk and len(current_chunk) + len(para) + 2 > target_size:
            # Flush current chunk
            chunks.append((current_chunk.strip(), current_heading, current_offset))
            current_chunk = para
            current_offset = running_offset
        elif len(para) > max_size:
            # Flush current chunk first
            if current_chunk.strip():
                chunks.append((current_chunk.strip(), current_heading, current_offset))
                current_chunk = ""

            # Split oversized paragraph on sentences
            sentences = _split_sentences(para)
            sent_chunk = ""
            sent_offset = running_offset
            for sent in sentences:
                if sent_chunk and len(sent_chunk) + len(sent) + 1 > target_size:
                    chunks.append((sent_chunk.strip(), current_heading, sent_offset))
                    sent_chunk = sent
                    sent_offset = running_offset + len(para) - len(sent)
                else:
                    sent_chunk = sent_chunk + " " + sent if sent_chunk else sent
            if sent_chunk.strip():
                chunks.append((sent_chunk.strip(), current_heading, sent_offset))
            current_offset = running_offset + len(para) + 2
        else:
            if current_chunk:
                current_chunk += "\n\n" + para
            else:
                current_chunk = para
                current_offset = running_offset

        running_offset += len(para) + 2

    # Flush remaining
    if current_chunk.strip():
        chunks.append((current_chunk.strip(), current_heading, current_offset))

    # Build Passage objects
    passages = []
    for i, (chunk_text_val, heading, offset) in enumerate(chunks, start=1):
        passages.append(Passage(
            passage_id=f"{source_id}-P{i}",
            source_url=source_url,
            text=chunk_text_val,
            char_offset=offset,
            heading_context=heading,
        ))

    return passages


def _split_sentences(text: str) -> list[str]:
    """Split text into sentences using simple heuristics."""
    # Split on sentence-ending punctuation followed by space and uppercase
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z])", text)
    return [p.strip() for p in parts if p.strip()]
