"""Intake router: decide how each uploaded file reaches the model.

Real packets are mixed — a digital, searchable PDF next to a scanned image. The router
picks the cheapest path that will actually work per file:

* **text path** — the file has a usable text layer (``.txt``/``.md``/``.docx``, or a
  digital PDF), so the existing extractors read it for ~free and no image is sent.
* **image path** — the file is a scan or an image (a low-text PDF, ``.png``/``.jpg``…), so
  the raw bytes travel to the multimodal model, which parses and extracts in one call.

The router itself does **no** model calls — it only prepares content — so it stays cheap,
deterministic, and unit-testable with no network. It wraps ``extractors``; it does not
replace them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from ragchat.errors import EmptyDocumentError, UnsupportedFileTypeError
from ragchat.ingestion.extractors import _docx_to_text, _pdf_to_text, pdf_to_png_pages

logger = logging.getLogger(__name__)

_TEXT_EXTENSIONS: frozenset[str] = frozenset({".txt", ".text", ".md", ".markdown"})
_IMAGE_EXTENSIONS: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
}
SUPPORTED_EXTENSIONS: frozenset[str] = (
    _TEXT_EXTENSIONS | {".docx", ".pdf"} | frozenset(_IMAGE_EXTENSIONS)
)

TEXT = "text"
IMAGE = "image"


@dataclass(frozen=True, slots=True)
class MediaPart:
    """A binary part (an image, or a scanned PDF) to hand to the multimodal model."""

    mime_type: str
    data: bytes


@dataclass(frozen=True, slots=True)
class DocumentContent:
    """A file prepared for the model: either extracted ``text`` or ``media`` parts."""

    filename: str
    mode: str  # TEXT or IMAGE
    text: str | None = None
    media: tuple[MediaPart, ...] = ()


def _usable_text(text: str, min_chars: int) -> bool:
    """A text layer is usable if it has enough non-whitespace characters to parse."""
    return sum(1 for ch in text if not ch.isspace()) >= min_chars


def route(
    filename: str,
    data: bytes,
    *,
    min_text_layer_chars: int = 32,
    max_scan_pages: int = 10,
    scan_dpi: int = 150,
) -> DocumentContent:
    """Prepare ``data`` for the model, choosing the text or image path from ``filename``.

    Raises :class:`UnsupportedFileTypeError` for unknown extensions and
    :class:`EmptyDocumentError` when a text-path file yields no usable text.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    label = PurePosixPath(filename).name

    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"Unsupported file type '{suffix or filename}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )

    if suffix in _IMAGE_EXTENSIONS:
        return DocumentContent(
            filename=label, mode=IMAGE, media=(MediaPart(_IMAGE_EXTENSIONS[suffix], data),)
        )

    if suffix in _TEXT_EXTENSIONS:
        text = data.decode("utf-8", errors="replace")
        if not _usable_text(text, min_text_layer_chars):
            raise EmptyDocumentError(f"No usable text found in '{label}'.")
        return DocumentContent(filename=label, mode=TEXT, text=text)

    if suffix == ".docx":
        text = _docx_to_text(data)
        if not _usable_text(text, min_text_layer_chars):
            raise EmptyDocumentError(f"No usable text found in '{label}'.")
        return DocumentContent(filename=label, mode=TEXT, text=text)

    # .pdf — prefer the text layer; fall back to the multimodal path for scans.
    text = _pdf_to_text(data)
    if _usable_text(text, min_text_layer_chars):
        return DocumentContent(filename=label, mode=TEXT, text=text)
    # Scanned PDF → rasterize each page to PNG. Every multimodal provider accepts image/png,
    # whereas only Gemini accepts an inline application/pdf part; PNG keeps the whole fallback
    # ladder working. If rasterization fails, fall back to the raw PDF (Gemini-only) so the
    # audit still runs on the primary rather than failing outright.
    try:
        pages = pdf_to_png_pages(data, max_pages=max_scan_pages, dpi=scan_dpi)
    except Exception:
        logger.exception("PDF rasterization failed for '%s'; sending raw PDF (Gemini-only)", label)
        pages = []
    if pages:
        media = tuple(MediaPart("image/png", png) for png in pages)
        return DocumentContent(filename=label, mode=IMAGE, media=media)
    return DocumentContent(filename=label, mode=IMAGE, media=(MediaPart("application/pdf", data),))
