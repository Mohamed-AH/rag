"""Tests for markdown parsing."""

from __future__ import annotations

from ragchat.ingestion.parser import Section, parse_markdown, parse_markdown_file


def test_splits_on_headings() -> None:
    md = "# Title A\nBody a1\nBody a2\n\n# Title B\nBody b1\n"
    sections = parse_markdown(md)
    assert sections == [
        Section(title="Title A", content="Body a1 Body a2"),
        Section(title="Title B", content="Body b1"),
    ]


def test_skips_image_lines() -> None:
    md = "# Title\n![](img.png)\nReal content\n"
    sections = parse_markdown(md)
    assert len(sections) == 1
    assert "img.png" not in sections[0].content
    assert sections[0].content == "Real content"


def test_heading_without_body_is_dropped() -> None:
    md = "# Empty Heading\n\n# Has Body\nContent\n"
    sections = parse_markdown(md)
    assert [s.title for s in sections] == ["Has Body"]


def test_content_before_any_heading_is_ignored() -> None:
    md = "orphan text\n# Heading\nbody\n"
    sections = parse_markdown(md)
    assert sections == [Section(title="Heading", content="body")]


def test_multiple_hashes_are_stripped_from_title() -> None:
    md = "### Deep Heading\nbody\n"
    sections = parse_markdown(md)
    assert sections[0].title == "Deep Heading"


def test_empty_document_yields_no_sections() -> None:
    assert parse_markdown("") == []


def test_as_document_text_combines_title_and_content() -> None:
    section = Section(title="VPC", content="A private cloud network.")
    assert section.as_document_text() == "VPC: A private cloud network."


def test_parse_markdown_file(tmp_path) -> None:
    path = tmp_path / "doc.md"
    path.write_text("# Heading\nbody text\n", encoding="utf-8")
    sections = parse_markdown_file(path)
    assert sections == [Section(title="Heading", content="body text")]
