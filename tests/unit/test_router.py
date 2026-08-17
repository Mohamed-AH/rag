"""Tests for the intake router (no network: text vs. image path selection)."""

from __future__ import annotations

import io

import pytest

from ragchat.errors import EmptyDocumentError, UnsupportedFileTypeError
from ragchat.ingestion import router as router_module
from ragchat.ingestion.router import route

_LONG = "This is a commercial invoice with plenty of readable text content on it."


def test_txt_uses_text_path() -> None:
    content = route("invoice.txt", _LONG.encode())
    assert content.mode == "text"
    assert content.text is not None and "commercial invoice" in content.text
    assert content.media == ()
    assert content.filename == "invoice.txt"


def test_markdown_uses_text_path() -> None:
    content = route("notes.md", f"# Heading\n\n{_LONG}".encode())
    assert content.mode == "text"


def test_image_uses_image_path() -> None:
    content = route("scan.png", b"\x89PNG\r\n\x1a\n fake image bytes here")
    assert content.mode == "image"
    assert content.text is None
    assert len(content.media) == 1
    assert content.media[0].mime_type == "image/png"


def test_unsupported_extension_raises() -> None:
    with pytest.raises(UnsupportedFileTypeError):
        route("archive.zip", b"...")


def test_empty_text_file_raises() -> None:
    with pytest.raises(EmptyDocumentError):
        route("blank.txt", b"   \n  ")


def test_digital_pdf_uses_text_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "_pdf_to_text", lambda _data: _LONG)
    content = route("invoice.pdf", b"%PDF-1.4 ...")
    assert content.mode == "text"
    assert content.text == _LONG


def _blank_pdf(pages: int) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_scanned_pdf_rasterizes_to_png_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    # A scan (no usable text layer) is rendered to PNG pages — the format every multimodal
    # provider accepts (Mistral/Groq reject inline application/pdf).
    monkeypatch.setattr(router_module, "_pdf_to_text", lambda _data: "   ")
    content = route("scan.pdf", _blank_pdf(2))
    assert content.mode == "image"
    assert len(content.media) == 2
    assert all(m.mime_type == "image/png" for m in content.media)
    assert all(m.data[:8] == b"\x89PNG\r\n\x1a\n" for m in content.media)


def test_scanned_pdf_page_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(router_module, "_pdf_to_text", lambda _data: "")
    content = route("scan.pdf", _blank_pdf(5), max_scan_pages=3)
    assert content.mode == "image"
    assert len(content.media) == 3  # capped


def test_unrasterizable_pdf_falls_back_to_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    # If rendering fails (corrupt bytes), fall back to the raw PDF so the audit still runs on
    # the primary (Gemini accepts inline PDF) rather than failing outright.
    monkeypatch.setattr(router_module, "_pdf_to_text", lambda _data: "   ")
    raw = b"%PDF-1.4 not really a parseable pdf"
    content = route("scan.pdf", raw)
    assert content.mode == "image"
    assert content.media[0].mime_type == "application/pdf"
    assert content.media[0].data == raw


def test_docx_uses_text_path() -> None:
    from docx import Document as DocxDocument

    doc = DocxDocument()
    doc.add_paragraph(_LONG)
    buf = io.BytesIO()
    doc.save(buf)
    content = route("packing.docx", buf.getvalue())
    assert content.mode == "text"
    assert content.text is not None and "commercial invoice" in content.text
