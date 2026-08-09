"""Markdown parsing.

Splits a markdown document into titled sections keyed on headings. This is a typed,
tested refactor of the original ``extract_sections`` proof-of-concept helper: same
heading/image-skipping behaviour, but returning immutable domain objects and driven by
a small set of pure functions that are trivial to unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Section:
    """A single titled chunk of content extracted from a document."""

    title: str
    content: str

    def as_document_text(self) -> str:
        """Render the section as a single string for embedding: ``"title: content"``."""
        return f"{self.title}: {self.content}"


def _is_heading(line: str) -> bool:
    return line.lstrip().startswith("#")


def _is_image(line: str) -> bool:
    return line.lstrip().startswith("![")


def parse_markdown(text: str) -> list[Section]:
    """Parse ``text`` into a list of :class:`Section` objects.

    A section starts at a ``#`` heading and runs until the next heading. Image
    reference lines are skipped, blank lines are collapsed, and a heading with no
    body text produces no section.
    """
    sections: list[Section] = []
    current_title: str | None = None
    buffer: list[str] = []

    def flush() -> None:
        if current_title is None:
            return
        content = " ".join(buffer).strip()
        if content:
            sections.append(Section(title=current_title, content=content))

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or _is_image(line):
            continue
        if _is_heading(line):
            flush()
            current_title = line.strip("#").strip()
            buffer = []
        elif current_title is not None:
            buffer.append(line)

    flush()
    return sections


def parse_markdown_file(path: str | Path) -> list[Section]:
    """Read a markdown file from ``path`` and parse it into sections."""
    return parse_markdown(Path(path).read_text(encoding="utf-8"))
