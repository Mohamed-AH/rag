"""Request/response models for the HTTP API.

Pydantic models double as validation and as the source for the generated OpenAPI schema
served at ``/docs``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ragchat.audit.report import Finding, GapReport


class AskRequest(BaseModel):
    """A question to answer against the knowledge base."""

    question: str = Field(..., min_length=1, max_length=2000, examples=["What is a VPC?"])


class SourceSchema(BaseModel):
    """A source document that supported an answer."""

    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class AskResponse(BaseModel):
    """An answer plus the sources it was grounded in."""

    answer: str
    sources: list[SourceSchema]


class IngestResponse(BaseModel):
    """Result of an ingestion run."""

    sections_written: int


class HealthResponse(BaseModel):
    """Readiness probe payload."""

    status: str = Field(examples=["ok"])


class ErrorResponse(BaseModel):
    """Uniform error envelope."""

    detail: str


# --- Packet auditor -------------------------------------------------------


class SourcePointerSchema(BaseModel):
    """Where in the packet a finding's evidence lives."""

    doc_id: str
    page: int | None = None
    snippet: str | None = None


class FindingSchema(BaseModel):
    """One requirement's audit outcome."""

    requirement_id: str
    status: str = Field(examples=["present", "missing", "deficient", "needs_review"])
    summary: str
    confidence: float
    sources: list[SourcePointerSchema] = Field(default_factory=list)

    @classmethod
    def from_finding(cls, finding: Finding) -> FindingSchema:
        return cls(
            requirement_id=finding.requirement_id,
            status=finding.status.value,
            summary=finding.summary,
            confidence=finding.confidence,
            sources=[
                SourcePointerSchema(doc_id=s.doc_id, page=s.page, snippet=s.snippet)
                for s in finding.sources
            ],
        )


class GapReportSchema(BaseModel):
    """The four-bucket Gap Report, plus a top-level clear/blocked flag."""

    present: list[FindingSchema]
    missing: list[FindingSchema]
    deficient: list[FindingSchema]
    needs_review: list[FindingSchema]
    is_clear: bool

    @classmethod
    def from_report(cls, report: GapReport) -> GapReportSchema:
        return cls(
            present=[FindingSchema.from_finding(f) for f in report.present],
            missing=[FindingSchema.from_finding(f) for f in report.missing],
            deficient=[FindingSchema.from_finding(f) for f in report.deficient],
            needs_review=[FindingSchema.from_finding(f) for f in report.needs_review],
            is_clear=report.is_clear,
        )


class AuditResponse(BaseModel):
    """Result of auditing a packet: its id, the checklist used, and the Gap Report."""

    packet_id: str
    checklist_id: str
    report: GapReportSchema
