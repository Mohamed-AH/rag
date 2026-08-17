"""Request/response models for the HTTP API.

Pydantic models double as validation and as the source for the generated OpenAPI schema
served at ``/docs``.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from ragchat.audit.report import Finding, GapReport
    from ragchat.audit.review import ReviewedFinding
    from ragchat.service import AuditSummary, StoredAudit


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


class ReviewSchema(BaseModel):
    """A reviewer's decision on a finding."""

    action: str = Field(examples=["accept", "override"])
    status: str = Field(examples=["present", "missing", "deficient", "needs_review"])
    note: str | None = None
    reviewed_at: datetime | None = None


class FindingSchema(BaseModel):
    """One requirement's audit outcome.

    ``status`` is the *effective* status (what bucket the finding sits in): the reviewer's
    override when overridden, otherwise the machine's verdict. ``machine_status`` is always
    the original machine verdict, and ``review`` carries the human decision if any.
    """

    requirement_id: str
    status: str = Field(examples=["present", "missing", "deficient", "needs_review"])
    machine_status: str = Field(examples=["present", "missing", "deficient", "needs_review"])
    summary: str
    confidence: float
    sources: list[SourcePointerSchema] = Field(default_factory=list)
    review: ReviewSchema | None = None

    @classmethod
    def from_finding(cls, finding: Finding) -> FindingSchema:
        return cls(
            requirement_id=finding.requirement_id,
            status=finding.status.value,
            machine_status=finding.status.value,
            summary=finding.summary,
            confidence=finding.confidence,
            sources=[
                SourcePointerSchema(doc_id=s.doc_id, page=s.page, snippet=s.snippet)
                for s in finding.sources
            ],
        )

    @classmethod
    def from_reviewed(cls, reviewed: ReviewedFinding) -> FindingSchema:
        finding = reviewed.finding
        review = reviewed.review
        return cls(
            requirement_id=finding.requirement_id,
            status=reviewed.effective_status.value,
            machine_status=finding.status.value,
            summary=finding.summary,
            confidence=finding.confidence,
            sources=[
                SourcePointerSchema(doc_id=s.doc_id, page=s.page, snippet=s.snippet)
                for s in finding.sources
            ],
            review=(
                None
                if review is None
                else ReviewSchema(
                    action=review.action.value,
                    status=review.status.value,
                    note=review.note,
                    reviewed_at=review.reviewed_at,
                )
            ),
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

    @classmethod
    def from_reviewed(cls, findings: tuple[ReviewedFinding, ...]) -> GapReportSchema:
        """Bucket reviewed findings by *effective* status, keeping each one's review info."""
        from ragchat.audit.report import FindingStatus

        buckets: dict[FindingStatus, list[FindingSchema]] = {s: [] for s in FindingStatus}
        for rf in findings:
            buckets[rf.effective_status].append(FindingSchema.from_reviewed(rf))
        return cls(
            present=buckets[FindingStatus.PRESENT],
            missing=buckets[FindingStatus.MISSING],
            deficient=buckets[FindingStatus.DEFICIENT],
            needs_review=buckets[FindingStatus.NEEDS_REVIEW],
            is_clear=not (
                buckets[FindingStatus.MISSING]
                or buckets[FindingStatus.DEFICIENT]
                or buckets[FindingStatus.NEEDS_REVIEW]
            ),
        )


class AuditResponse(BaseModel):
    """Result of auditing a packet: its id, the checklist used, the Gap Report, and a
    ready-to-send missing-items request rendered from that report."""

    packet_id: str
    checklist_id: str
    report: GapReportSchema
    request_summary: str


class ChecklistOption(BaseModel):
    """A selectable audit vertical, for the UI's vertical picker."""

    id: str
    name: str


class ProviderStatus(BaseModel):
    """One rung of the audit fallback ladder, for the ``/providers`` diagnostic.

    Reports only non-secret facts: whether the provider's key is present (``configured`` —
    never the key itself) and the resolved model ids. Lets an operator confirm env overrides
    and the active ladder right after deploy.
    """

    id: str
    known: bool = Field(description="Whether this id is a recognized provider.")
    configured: bool = Field(description="Whether the provider's key/URL is set (never the key).")
    multimodal: bool
    text_model: str | None = None
    vision_model: str | None = None


# --- Reviewer workflow & history -----------------------------------------


class AuditSummarySchema(BaseModel):
    """A past audit as it appears in the history list (effective, post-review counts)."""

    packet_id: str
    checklist_id: str
    checklist_name: str
    created_at: datetime
    present: int
    missing: int
    deficient: int
    needs_review: int
    reviewed_count: int
    is_clear: bool

    @classmethod
    def from_summary(cls, summary: AuditSummary, *, checklist_name: str) -> AuditSummarySchema:
        report = summary.report
        return cls(
            packet_id=summary.packet_id,
            checklist_id=summary.checklist_id,
            checklist_name=checklist_name,
            created_at=summary.created_at,
            present=len(report.present),
            missing=len(report.missing),
            deficient=len(report.deficient),
            needs_review=len(report.needs_review),
            reviewed_count=summary.reviewed_count,
            is_clear=report.is_clear,
        )


class StoredAuditSchema(BaseModel):
    """A re-opened audit: the checklist it used, its Gap Report with reviews applied, and a
    freshly rendered missing-items request reflecting the effective (post-review) state."""

    packet_id: str
    checklist_id: str
    checklist_name: str
    created_at: datetime
    report: GapReportSchema
    request_summary: str

    @classmethod
    def from_stored(
        cls, stored: StoredAudit, *, checklist_name: str, request_summary: str
    ) -> StoredAuditSchema:
        return cls(
            packet_id=stored.packet_id,
            checklist_id=stored.checklist_id,
            checklist_name=checklist_name,
            created_at=stored.created_at,
            report=GapReportSchema.from_reviewed(stored.findings),
            request_summary=request_summary,
        )


class ReviewRequest(BaseModel):
    """A reviewer's decision on one finding: accept the machine verdict, or override it."""

    action: str = Field(examples=["accept", "override"])
    status: str | None = Field(
        default=None, examples=["present", "missing", "deficient", "needs_review"]
    )
    note: str | None = Field(default=None, max_length=2000)
