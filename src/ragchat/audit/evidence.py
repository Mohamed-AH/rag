"""The evidence contract the gap-analysis engine consumes.

Phase 0 hand-builds these objects in tests; Phase 1's classifier and extractor will
produce them from real files. Keeping the input contract in its own module (separate from
the *checklist* that describes requirements and the *report* that is the output) gives the
engine a single, stable input type and keeps the package a clean dependency DAG.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ragchat.audit.report import FindingStatus, SourcePointer


@dataclass(frozen=True, slots=True)
class ExtractedField:
    """A single value pulled from a document, with how sure we are and where it came from."""

    name: str
    value: Any
    confidence: float = 1.0
    source: SourcePointer | None = None


@dataclass(frozen=True, slots=True)
class ClassifiedDocument:
    """One uploaded file after classification: what type it is (or ``None`` if unknown),
    how confident that call is, and the fields extracted from it."""

    doc_id: str
    doc_type: str | None
    confidence: float
    fields: dict[str, ExtractedField] = field(default_factory=dict)

    def get(self, name: str) -> ExtractedField | None:
        """Return the extracted field named ``name``, or ``None`` if it wasn't found."""
        return self.fields.get(name)


@dataclass(frozen=True, slots=True)
class PacketEvidence:
    """The whole packet's classified documents — the engine's sole input alongside a
    checklist."""

    documents: tuple[ClassifiedDocument, ...] = ()

    def of_type(self, doc_type: str, *, min_confidence: float = 0.0) -> list[ClassifiedDocument]:
        """All documents classified as ``doc_type`` at or above ``min_confidence``."""
        return [
            d for d in self.documents if d.doc_type == doc_type and d.confidence >= min_confidence
        ]

    def first(self, doc_type: str, *, min_confidence: float = 0.0) -> ClassifiedDocument | None:
        """The highest-confidence document of ``doc_type``, or ``None`` if there is none."""
        matches = self.of_type(doc_type, min_confidence=min_confidence)
        return max(matches, key=lambda d: d.confidence, default=None)


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Thresholds a Layer-2 field rule evaluates against. Split out so they can be tuned
    against the eval fixture without touching rule logic."""

    min_field_confidence: float = 0.5
    value_tolerance: float = 0.01  # relative tolerance for numeric matches (1%)


@dataclass(frozen=True, slots=True)
class RuleResult:
    """What a Layer-2 field rule returns. A rule can land on ``present``, ``deficient``, or
    ``needs_review`` — never ``missing`` (absence of a whole document is Layer 1's job)."""

    status: FindingStatus
    summary: str
    confidence: float
    sources: tuple[SourcePointer, ...] = ()
