"""Extract structured :class:`Section` objects from uploaded files.

Dispatches on file extension: markdown keeps its heading structure; plain text, PDF, and
Word documents are text-extracted and then size-chunked. All formats funnel into the same
``Section`` pipeline the rest of the app already understands.
"""

from __future__ import annotations

import io
from pathlib import PurePosixPath

from ragchat.errors import EmptyDocumentError, UnsupportedFileTypeError
from ragchat.ingestion.chunker import chunk_text
from ragchat.ingestion.parser import Section, parse_markdown

SUPPORTED_EXTENSIONS: frozenset[str] = frozenset(
    {".md", ".markdown", ".txt", ".text", ".pdf", ".docx"}
)


def _pdf_to_text(data: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def _docx_to_text(data: bytes) -> str:
    from docx import Document as DocxDocument

    document = DocxDocument(io.BytesIO(data))
    return "\n\n".join(p.text for p in document.paragraphs if p.text.strip())


def extract_sections(
    filename: str,
    data: bytes,
    *,
    chunk_max_chars: int = 1200,
    chunk_overlap: int = 150,
) -> list[Section]:
    """Parse ``data`` (an uploaded file's bytes) into sections based on ``filename``.

    Raises :class:`UnsupportedFileTypeError` for unknown extensions and
    :class:`EmptyDocumentError` when no text can be extracted.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{suffix or filename}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    label = PurePosixPath(filename).name

    if suffix in {".md", ".markdown"}:
        sections = parse_markdown(data.decode("utf-8", errors="replace"))
    else:
        if suffix in {".txt", ".text"}:
            text = data.decode("utf-8", errors="replace")
        elif suffix == ".pdf":
            text = _pdf_to_text(data)
        else:  # .docx
            text = _docx_to_text(data)
        sections = chunk_text(text, source=label, max_chars=chunk_max_chars, overlap=chunk_overlap)

    if not sections:
        raise EmptyDocumentError(f"No extractable text found in '{label}'.")
    return sections
