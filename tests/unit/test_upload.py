"""Tests for upload guardrails in the service layer (caps enforced before embedding)."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ragchat.errors import FileTooLargeError, TooManySectionsError, UnsupportedFileTypeError
from ragchat.service import IngestLimits, RAGService


def test_upload_happy_path_writes_sections(make_service: Callable[..., RAGService]) -> None:
    svc = make_service("session_a")
    result = svc.ingest_upload("notes.txt", b"Some content about networking.")
    assert result.sections_written == 1
    assert svc.section_count() == 1


def test_upload_too_large_is_rejected(make_service: Callable[..., RAGService]) -> None:
    svc = make_service("session_a", limits=IngestLimits(max_upload_bytes=10))
    with pytest.raises(FileTooLargeError):
        svc.ingest_upload("notes.txt", b"this is definitely more than ten bytes")
    assert svc.section_count() == 0  # nothing written


def test_upload_too_many_sections_is_rejected(
    make_service: Callable[..., RAGService],
) -> None:
    svc = make_service(
        "session_a",
        limits=IngestLimits(max_sections=1, chunk_max_chars=50, chunk_overlap=0),
    )
    big = "\n\n".join(f"Paragraph {i} with enough words to fill a chunk." for i in range(10))
    with pytest.raises(TooManySectionsError):
        svc.ingest_upload("big.txt", big.encode())
    assert svc.section_count() == 0


def test_upload_unsupported_type_is_rejected(
    make_service: Callable[..., RAGService],
) -> None:
    svc = make_service("session_a")
    with pytest.raises(UnsupportedFileTypeError):
        svc.ingest_upload("archive.zip", b"PK\x03\x04")
