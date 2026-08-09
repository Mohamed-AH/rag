"""Tests for the Gap Report value object and its API serialization."""

from __future__ import annotations

from ragchat.api.schemas import GapReportSchema
from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer

_PRESENT = Finding("doc.a", FindingStatus.PRESENT, "A present", 0.9)
_MISSING = Finding("doc.b", FindingStatus.MISSING, "B missing", 1.0)
_DEFICIENT = Finding("rule.c", FindingStatus.DEFICIENT, "C malformed", 0.8)
_REVIEW = Finding(
    "doc.d",
    FindingStatus.NEEDS_REVIEW,
    "D unclear",
    0.3,
    (SourcePointer("d", page=2, snippet="x"),),
)


def test_buckets_partition_findings_by_status() -> None:
    report = GapReport((_PRESENT, _MISSING, _DEFICIENT, _REVIEW))
    assert report.present == [_PRESENT]
    assert report.missing == [_MISSING]
    assert report.deficient == [_DEFICIENT]
    assert report.needs_review == [_REVIEW]


def test_is_clear_only_when_nothing_blocks_or_needs_review() -> None:
    assert GapReport((_PRESENT,)).is_clear is True
    assert GapReport(()).is_clear is True
    assert GapReport((_PRESENT, _MISSING)).is_clear is False
    assert GapReport((_PRESENT, _DEFICIENT)).is_clear is False
    assert GapReport((_PRESENT, _REVIEW)).is_clear is False


def test_schema_from_report_maps_buckets_and_sources() -> None:
    schema = GapReportSchema.from_report(GapReport((_PRESENT, _MISSING, _DEFICIENT, _REVIEW)))
    assert schema.is_clear is False
    assert [f.requirement_id for f in schema.present] == ["doc.a"]
    assert schema.missing[0].status == "missing"
    review = schema.needs_review[0]
    assert review.sources[0].doc_id == "d"
    assert review.sources[0].page == 2
    assert review.sources[0].snippet == "x"
