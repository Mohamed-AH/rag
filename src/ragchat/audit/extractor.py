"""Field extraction: pull a document type's declared fields into structured values.

The :class:`Extractor` protocol is the pipeline's dependency (tests inject a fake).
:class:`GeminiExtractor` is the production adapter: given a document and its type, it asks
for exactly the fields the checklist declares for that type (see
:meth:`~ragchat.audit.checklist.Checklist.fields_for`) and returns each as an
:class:`~ragchat.audit.evidence.ExtractedField` with a confidence and a source snippet the
reviewer can verify. No retrieval / pgvector is involved — the document goes straight to a
structured call.
"""

from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field

from ragchat.audit.checklist import Checklist, FieldSpec
from ragchat.audit.evidence import ExtractedField
from ragchat.audit.report import SourcePointer
from ragchat.ingestion.router import DocumentContent


class Extractor(Protocol):
    """Extracts a document type's declared fields from a prepared document."""

    def extract(
        self, doc_id: str, content: DocumentContent, doc_type: str, checklist: Checklist
    ) -> dict[str, ExtractedField]: ...


class _FieldResult(BaseModel):
    name: str
    value: str | None = Field(default=None, description="The value, or null if not present.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    snippet: str | None = Field(default=None, description="Short verbatim quote of the source.")


class _ExtractionResult(BaseModel):
    fields: list[_FieldResult] = Field(default_factory=list)


def _prompt(doc_type_name: str, specs: tuple[FieldSpec, ...]) -> str:
    wanted = "\n".join(f"- {spec.name}: {spec.description}" for spec in specs)
    return (
        f"Extract the following fields from this {doc_type_name}. For each, return the "
        "value (or null if absent), your confidence (0-1), and a short verbatim snippet "
        "showing where it came from. Fields:\n"
        f"{wanted}"
    )


class GeminiExtractor:
    """Production extractor backed by a structured Gemini call."""

    def __init__(self, llm: Any) -> None:
        self._llm = llm

    def extract(
        self, doc_id: str, content: DocumentContent, doc_type: str, checklist: Checklist
    ) -> dict[str, ExtractedField]:
        from ragchat.rag.llm import build_document_message

        specs = checklist.fields_for(doc_type)
        if not specs:
            return {}

        dt = checklist.doc_type(doc_type)
        name = dt.name if dt is not None else doc_type
        structured = self._llm.with_structured_output(_ExtractionResult)
        message = build_document_message(_prompt(name, specs), content)
        result: _ExtractionResult = structured.invoke([message])

        wanted = {spec.name for spec in specs}
        extracted: dict[str, ExtractedField] = {}
        for field in result.fields:
            if field.name not in wanted or field.value is None:
                continue
            extracted[field.name] = ExtractedField(
                name=field.name,
                value=field.value,
                confidence=field.confidence,
                source=SourcePointer(doc_id=doc_id, snippet=field.snippet),
            )
        return extracted
