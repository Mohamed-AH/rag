"""Single-pass document analysis: classify + extract in one model call.

Earlier the pipeline made two calls per document (classify, then extract). This merges
them: one structured call identifies the document type *and* extracts that type's fields,
halving the model calls (and latency) per packet — the difference between staying inside a
free-tier daily quota and blowing past it.

The :class:`Analyzer` protocol is the pipeline's dependency, so tests inject a deterministic
fake and never touch a model. :class:`GeminiAnalyzer` is the production adapter; it takes a
``select_llm`` callable so text-path documents use the cheap, higher-quota lite model and
only scans/images spend the multimodal model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ragchat.audit.checklist import Checklist
from ragchat.audit.evidence import ClassifiedDocument, ExtractedField
from ragchat.audit.report import SourcePointer
from ragchat.ingestion.router import DocumentContent

_UNKNOWN = "unknown"


class Analyzer(Protocol):
    """Classifies a document and extracts its fields in one step."""

    def analyze(
        self, doc_id: str, content: DocumentContent, checklist: Checklist
    ) -> ClassifiedDocument: ...


class _FieldResult(BaseModel):
    name: str
    value: str | None = Field(default=None, description="The value, or null if not present.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    snippet: str | None = Field(default=None, description="Short verbatim quote of the source.")


class _AnalysisResult(BaseModel):
    doc_type: str = Field(description="One of the listed type ids, or 'unknown'.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the type, 0-1.")
    fields: list[_FieldResult] = Field(default_factory=list)


def _prompt(checklist: Checklist) -> str:
    lines = []
    for dt in checklist.doc_types:
        fields = "; ".join(f"{s.name} ({s.description})" for s in dt.fields) or "(no fields)"
        lines.append(f"- {dt.id} — {dt.name}: {fields}")
    catalogue = "\n".join(lines)
    return (
        "You are auditing one document from a customs packet. First classify it as exactly "
        "one of the types below, or 'unknown' if none fit. Then extract the chosen type's "
        "fields: for each, return the value (or null if absent), your confidence (0-1), and "
        "a short verbatim snippet showing where it came from.\n\n"
        f"Types and their fields:\n{catalogue}\n\n"
        "Return doc_type (a type id above or 'unknown'), confidence (0-1) for the "
        "classification, and the fields for the chosen type only."
    )


class GeminiAnalyzer:
    """Production analyzer backed by a single structured Gemini call per document."""

    def __init__(self, select_llm: Callable[[DocumentContent], Any]) -> None:
        self._select_llm = select_llm

    def analyze(
        self, doc_id: str, content: DocumentContent, checklist: Checklist
    ) -> ClassifiedDocument:
        from ragchat.rag.llm import build_document_message

        structured = self._select_llm(content).with_structured_output(_AnalysisResult)
        message = build_document_message(_prompt(checklist), content)
        result: _AnalysisResult = structured.invoke([message])

        doc_type = result.doc_type.strip()
        if not doc_type or doc_type == _UNKNOWN or doc_type not in checklist.doc_type_ids():
            return ClassifiedDocument(
                doc_id=doc_id, doc_type=None, confidence=result.confidence, fields={}
            )

        wanted = {spec.name for spec in checklist.fields_for(doc_type)}
        fields: dict[str, ExtractedField] = {}
        for field in result.fields:
            if field.name not in wanted or field.value is None:
                continue
            fields[field.name] = ExtractedField(
                name=field.name,
                value=field.value,
                confidence=field.confidence,
                source=SourcePointer(doc_id=doc_id, snippet=field.snippet),
            )
        return ClassifiedDocument(
            doc_id=doc_id, doc_type=doc_type, confidence=result.confidence, fields=fields
        )
