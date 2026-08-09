"""Tests for size-based text chunking."""

from __future__ import annotations

from ragchat.ingestion.chunker import chunk_text


def test_short_text_is_one_chunk() -> None:
    sections = chunk_text("Just a little text.", source="doc.txt")
    assert len(sections) == 1
    assert sections[0].title == "doc.txt (part 1)"
    assert sections[0].content == "Just a little text."


def test_long_text_splits_into_multiple_chunks() -> None:
    paras = "\n\n".join(f"Paragraph number {i} with some filler words." for i in range(50))
    sections = chunk_text(paras, source="big.txt", max_chars=200, overlap=20)
    assert len(sections) > 1
    assert all(len(s.content) <= 200 for s in sections)
    assert [s.title for s in sections] == [
        f"big.txt (part {i})" for i in range(1, len(sections) + 1)
    ]


def test_oversized_single_paragraph_is_hard_split() -> None:
    sections = chunk_text("x" * 1000, source="d.txt", max_chars=100, overlap=10)
    assert len(sections) > 1
    assert all(len(s.content) <= 100 for s in sections)


def test_empty_text_yields_no_sections() -> None:
    assert chunk_text("   \n\n  ", source="d.txt") == []


def test_whitespace_is_collapsed() -> None:
    sections = chunk_text("a\n  b\t\tc", source="d.txt")
    assert sections[0].content == "a b c"
