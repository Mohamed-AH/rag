"""Size-based text chunking for documents without headings (txt / pdf / docx).

Markdown is split on its headings (see :mod:`ragchat.ingestion.parser`); everything else
has no reliable structure, so we chunk on paragraph boundaries into overlapping windows.
Overlap keeps a retrieved chunk from losing context that straddled a boundary.
"""

from __future__ import annotations

import re

from ragchat.ingestion.parser import Section

_WHITESPACE = re.compile(r"[ \t\r\f\v]+")


def _normalize(text: str) -> list[str]:
    """Collapse intra-line whitespace and split into non-empty paragraphs."""
    paragraphs = []
    for block in text.split("\n\n"):
        cleaned = _WHITESPACE.sub(" ", block.replace("\n", " ")).strip()
        if cleaned:
            paragraphs.append(cleaned)
    return paragraphs


def chunk_text(
    text: str,
    *,
    source: str,
    max_chars: int = 1200,
    overlap: int = 150,
) -> list[Section]:
    """Split ``text`` into overlapping :class:`Section` chunks titled by ``source``.

    Paragraphs are packed into windows of up to ``max_chars``; when a window closes, the
    next one starts with the last ``overlap`` characters of the previous, so context that
    spans a boundary is retrievable from either side.
    """
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    overlap = max(0, min(overlap, max_chars - 1))

    paragraphs = _normalize(text)
    if not paragraphs:
        return []

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n\n{para}".strip() if tail else para
        else:
            # A single oversized paragraph: hard-split it into windows.
            for i in range(0, len(para), max_chars - overlap):
                chunks.append(para[i : i + max_chars])
            current = ""
    if current:
        chunks.append(current)

    return [
        Section(title=f"{source} (part {i})", content=chunk)
        for i, chunk in enumerate(chunks, start=1)
    ]
