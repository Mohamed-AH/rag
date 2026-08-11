"""Single-pass document analysis: classify + extract, and split combined files.

One structured call per uploaded file identifies **every** document in it and extracts each
one's fields. A file may hold one document (a single invoice) or many (a combined packet
PDF where each page is a different document), so :meth:`Analyzer.analyze` returns a *list*
of :class:`ClassifiedDocument`. This is what lets a customs broker drop in one merged PDF
and still get every document recognized — and it stays one model call, so it's quota-cheap.

The :class:`Analyzer` protocol is the pipeline's dependency, so tests inject a deterministic
fake and never touch a model. :class:`GeminiAnalyzer` is the production adapter; it takes a
``select_llm`` callable so text-path files use the cheap, higher-quota lite model and
scans/images use the (also-lite by default) multimodal model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ragchat.audit.checklist import Checklist, FieldSpec
from ragchat.audit.evidence import ClassifiedDocument, ExtractedField
from ragchat.audit.report import SourcePointer
from ragchat.ingestion.router import DocumentContent

_UNKNOWN = "unknown"


class Analyzer(Protocol):
    """Classifies and extracts every document found in one uploaded file."""

    def analyze(
        self, doc_id: str, content: DocumentContent, checklist: Checklist
    ) -> list[ClassifiedDocument]: ...


class _FieldResult(BaseModel):
    name: str
    value: str | None = Field(default=None, description="The value, or null if not present.")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    snippet: str | None = Field(default=None, description="Short verbatim quote of the source.")


class _DocResult(BaseModel):
    doc_type: str = Field(description="One of the listed type ids, or 'unknown'.")
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the type, 0-1.")
    pages: list[int] = Field(
        default_factory=list,
        description="All 1-based page numbers this document occupies (may be non-contiguous).",
    )
    grouping_reason: str | None = Field(
        default=None,
        description="Why these pages were grouped, e.g. a shared invoice/BoL/container number.",
    )
    fields: list[_FieldResult] = Field(default_factory=list)


class _AnalysisResult(BaseModel):
    documents: list[_DocResult] = Field(
        default_factory=list, description="Every distinct document found in the file."
    )


def _prompt(checklist: Checklist) -> str:
    lines = []
    for dt in checklist.doc_types:
        fields = "; ".join(f"{s.name} ({s.description})" for s in dt.fields) or "(no fields)"
        lines.append(f"- {dt.id} — {dt.name}: {fields}")
    catalogue = "\n".join(lines)
    return (
        "You are auditing a customs submission file that may contain ONE or MANY documents, "
        "in ANY page order. Pages of the same document may be non-contiguous (e.g. an invoice "
        "on pages 2 and 4 with a bill of lading on page 3). Do NOT assume documents appear in "
        "order or on consecutive pages — use the whole file at once.\n\n"
        "Work it out page by page:\n"
        "1. Scan every page for document anchor headers/titles (e.g. 'Commercial Invoice', "
        "'Packing List', 'Bill of Lading', 'Certificate of Origin') and distinct layouts.\n"
        "2. Group pages that belong to the same document by tracking shared identifiers across "
        "pages — invoice numbers, bill-of-lading / air-waybill numbers, container or PO numbers.\n"
        "3. For each distinct document: classify it as exactly one of the types below (or "
        "'unknown'), list ALL of its page numbers (contiguous or not), give a short "
        "grouping_reason when you merged pages, and extract that type's fields — each with a "
        "value (or null if absent), confidence (0-1), and a short verbatim snippet.\n\n"
        f"Types and their fields:\n{catalogue}\n\n"
        "Return a `documents` list with exactly one entry per distinct document. Do not split "
        "one document across multiple entries, do not merge different documents into one entry, "
        "and do not invent documents that are not present."
    )


class GeminiAnalyzer:
    """Production analyzer backed by a single structured Gemini call per file."""

    def __init__(self, select_llm: Callable[[DocumentContent], Any]) -> None:
        self._select_llm = select_llm

    def analyze(
        self, doc_id: str, content: DocumentContent, checklist: Checklist
    ) -> list[ClassifiedDocument]:
        from ragchat.rag.llm import build_document_message

        structured = self._select_llm(content).with_structured_output(_AnalysisResult)
        message = build_document_message(_prompt(checklist), content)
        result: _AnalysisResult = structured.invoke([message])

        known = checklist.doc_type_ids()
        multi = len(result.documents) > 1
        seen: set[str] = set()
        out: list[ClassifiedDocument] = []
        for index, doc in enumerate(result.documents):
            did = _doc_id(doc_id, doc, index, multi, seen)
            doc_type = doc.doc_type.strip()
            if not doc_type or doc_type == _UNKNOWN or doc_type not in known:
                out.append(ClassifiedDocument(doc_id=did, doc_type=None, confidence=doc.confidence))
                continue
            out.append(
                ClassifiedDocument(
                    doc_id=did,
                    doc_type=doc_type,
                    confidence=doc.confidence,
                    fields=_fields(did, doc, checklist.fields_for(doc_type)),
                )
            )
        # A file that yielded nothing recognizable is still surfaced (as unrecognized),
        # never silently dropped.
        if not out:
            out.append(ClassifiedDocument(doc_id=doc_id, doc_type=None, confidence=0.0))
        return out


def _doc_id(base: str, doc: _DocResult, index: int, multi: bool, seen: set[str]) -> str:
    """A stable, unique, human-readable id for one detected document within a file."""
    if not multi:
        candidate = base
    elif doc.pages:
        pages = ", ".join(str(p) for p in doc.pages)
        candidate = f"{base} ({'pages' if len(doc.pages) > 1 else 'page'} {pages})"
    else:
        candidate = f"{base} · {doc.doc_type.strip() or 'doc'} #{index + 1}"
    unique = candidate
    n = 1
    while unique in seen:
        n += 1
        unique = f"{candidate} #{n}"
    seen.add(unique)
    return unique


def _fields(
    doc_id: str, doc: _DocResult, specs: tuple[FieldSpec, ...]
) -> dict[str, ExtractedField]:
    first_page = doc.pages[0] if doc.pages else None
    wanted = {spec.name for spec in specs}
    fields: dict[str, ExtractedField] = {}
    for field in doc.fields:
        if field.name not in wanted or field.value is None:
            continue
        fields[field.name] = ExtractedField(
            name=field.name,
            value=field.value,
            confidence=field.confidence,
            source=SourcePointer(doc_id=doc_id, page=first_page, snippet=field.snippet),
        )
    return fields
