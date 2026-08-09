"""Document classification: label each packet file as one of the checklist's doc types.

The :class:`Classifier` protocol is what the audit pipeline depends on, so tests inject a
deterministic fake and never touch a model. :class:`GeminiClassifier` is the production
adapter — a single structured call that returns a type id (or ``None`` for unrecognized)
and a confidence the engine uses to decide ``present`` vs ``needs_review``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ragchat.audit.checklist import Checklist
from ragchat.ingestion.router import DocumentContent

_UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class Classification:
    """The outcome of classifying one document."""

    doc_type: str | None
    confidence: float


class Classifier(Protocol):
    """Classifies a prepared document against a checklist's known types."""

    def classify(self, content: DocumentContent, checklist: Checklist) -> Classification: ...


class _ClassificationResult(BaseModel):
    """Structured schema the model fills in."""

    doc_type: str = Field(description="One of the listed type ids, or 'unknown'.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the type, 0-1.")


def _prompt(checklist: Checklist) -> str:
    catalogue = "\n".join(f"- {dt.id}: {dt.name}" for dt in checklist.doc_types)
    return (
        "You classify a single trade/customs document. Choose the one type id that best "
        "matches the document below, or 'unknown' if none fit. Types:\n"
        f"{catalogue}\n\n"
        "Return the type id and your confidence (0-1)."
    )


class GeminiClassifier:
    """Production classifier backed by a structured Gemini call."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def classify(self, content: DocumentContent, checklist: Checklist) -> Classification:
        from ragchat.rag.llm import build_document_message

        structured = self._llm.with_structured_output(_ClassificationResult)
        message = build_document_message(_prompt(checklist), content)
        result: _ClassificationResult = structured.invoke([message])

        doc_type = result.doc_type.strip()
        if not doc_type or doc_type == _UNKNOWN or doc_type not in checklist.doc_type_ids():
            return Classification(doc_type=None, confidence=result.confidence)
        return Classification(doc_type=doc_type, confidence=result.confidence)
