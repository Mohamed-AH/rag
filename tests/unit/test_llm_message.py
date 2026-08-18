"""Tests for build_document_message — the multimodal content-block shape.

The image content block must use the canonical OpenAI form (image_url as an OBJECT with a
"url" key). Groq / OpenAI-compatible endpoints reject the bare-string shorthand.
"""

from __future__ import annotations

from ragchat.ingestion.router import IMAGE, TEXT, DocumentContent, MediaPart
from ragchat.rag.llm import build_document_message


def test_text_mode_is_a_plain_string_message() -> None:
    msg = build_document_message("INSTR", DocumentContent(filename="a.txt", mode=TEXT, text="hi"))
    assert isinstance(msg.content, str)
    assert "INSTR" in msg.content and "hi" in msg.content


def test_image_mode_uses_object_image_url_blocks() -> None:
    content = DocumentContent(
        filename="scan.pdf",
        mode=IMAGE,
        media=(MediaPart("image/png", b"\x89PNG\r\n\x1a\n"), MediaPart("image/png", b"\x89PNG..")),
    )
    msg = build_document_message("INSTR", content)
    assert isinstance(msg.content, list)
    # First block is the text instruction.
    assert msg.content[0] == {"type": "text", "text": "INSTR"}
    # Each image block: image_url is an OBJECT {"url": "data:image/png;base64,..."}, not a string.
    image_blocks = msg.content[1:]
    assert len(image_blocks) == 2
    for block in image_blocks:
        assert block["type"] == "image_url"
        assert isinstance(block["image_url"], dict)
        assert block["image_url"]["url"].startswith("data:image/png;base64,")
