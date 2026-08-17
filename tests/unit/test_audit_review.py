"""Tests for persisted audits + the reviewer workflow (service over real SQLite).

An audit is run through the real ``AuditService`` (scripted analyzer), which persists its
findings; then ``AuditReviewService`` reads that history back and records accept/override
decisions. No network, no keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ragchat.audit.evidence import ExtractedField
from ragchat.audit.report import FindingStatus
from ragchat.audit.review import ReviewAction
from ragchat.service import AuditReviewService, AuditService
from tests.conftest import FakeAnalysis, FakeAnalyzer

_TEXT = "A trade document with enough readable text to take the router's text path."


def _fields(**pairs: Any) -> dict[str, ExtractedField]:
    return {k: ExtractedField(name=k, value=v, confidence=0.95) for k, v in pairs.items()}


def _packet_missing_origin() -> tuple[list[tuple[str, bytes]], FakeAnalyzer]:
    """A packet missing the certificate of origin -> a MISSING finding to review."""
    files = [
        ("invoice.txt", _TEXT.encode()),
        ("packing.txt", _TEXT.encode()),
        ("bol.txt", _TEXT.encode()),
    ]
    analyzer = FakeAnalyzer(
        {
            "invoice.txt": FakeAnalysis(
                "commercial_invoice",
                0.97,
                _fields(
                    hts_code="8471.30.01",
                    country_of_origin="China",
                    currency="USD",
                    total_value="10000",
                    total_quantity="200",
                    exporter="Acme Ltd",
                    consignee="Globex Inc",
                ),
            ),
            "packing.txt": FakeAnalysis(
                "packing_list",
                0.96,
                _fields(
                    total_value="10000",
                    total_quantity="200",
                    net_weight="500",
                    total_cartons="20",
                    exporter="Acme Ltd",
                    consignee="Globex Inc",
                ),
            ),
            "bol.txt": FakeAnalysis(
                "bill_of_lading",
                0.95,
                _fields(
                    net_weight="500",
                    total_cartons="20",
                    exporter="Acme Ltd",
                    consignee="Globex Inc",
                ),
            ),
        }
    )
    return files, analyzer


def _review_service(
    session_factory: Callable[[], Session], session_id: str = "session_a"
) -> AuditReviewService:
    return AuditReviewService(session_id=session_id, session_factory=session_factory)


def test_audit_findings_are_persisted_and_reopenable(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service(analyzer=analyzer).audit_packet(files)

    reviews = _review_service(session_factory)
    stored = reviews.get_audit(result.packet_id)
    assert stored is not None
    # The whole report round-tripped: same finding count, and the missing COO is there.
    assert len(stored.findings) == len(result.report.findings)
    assert any(
        rf.finding.requirement_id == "doc.certificate_of_origin"
        and rf.effective_status is FindingStatus.MISSING
        for rf in stored.findings
    )
    assert all(rf.is_reviewed is False for rf in stored.findings)


def test_list_audits_returns_recent_with_effective_counts(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service(analyzer=analyzer).audit_packet(files)

    summaries = _review_service(session_factory).list_audits()
    assert len(summaries) == 1
    s = summaries[0]
    assert s.packet_id == result.packet_id
    assert s.checklist_id == "customs"
    assert s.report.missing  # the missing COO
    assert s.reviewed_count == 0
    assert s.report.is_clear is False


def test_override_missing_to_present_clears_the_gap(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service(analyzer=analyzer).audit_packet(files)
    reviews = _review_service(session_factory)

    stored = reviews.review_finding(
        result.packet_id,
        "doc.certificate_of_origin",
        action=ReviewAction.OVERRIDE,
        status=FindingStatus.PRESENT,
        note="COO was attached separately and verified by hand.",
    )
    # The overridden finding now sits in the present bucket; the machine verdict is retained.
    coo = next(
        rf for rf in stored.findings if rf.finding.requirement_id == "doc.certificate_of_origin"
    )
    assert coo.effective_status is FindingStatus.PRESENT
    assert coo.finding.status is FindingStatus.MISSING
    assert coo.review is not None and coo.review.note is not None

    # Persisted: re-opening reflects the override.
    reopened = reviews.get_audit(result.packet_id)
    assert reopened is not None
    assert not reopened.report.missing
    assert reviews.list_audits()[0].reviewed_count == 1


def test_accept_records_a_decision_without_changing_status(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service(analyzer=analyzer).audit_packet(files)
    reviews = _review_service(session_factory)

    stored = reviews.review_finding(
        result.packet_id,
        "doc.certificate_of_origin",
        action=ReviewAction.ACCEPT,
        status=None,
        note=None,
    )
    coo = next(
        rf for rf in stored.findings if rf.finding.requirement_id == "doc.certificate_of_origin"
    )
    assert coo.is_reviewed is True
    assert coo.is_overridden is False
    assert coo.effective_status is FindingStatus.MISSING


def test_review_unknown_finding_raises_lookup(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service(analyzer=analyzer).audit_packet(files)
    reviews = _review_service(session_factory)

    with pytest.raises(LookupError):
        reviews.review_finding(
            result.packet_id,
            "rule.does_not_exist",
            action=ReviewAction.ACCEPT,
            status=None,
            note=None,
        )


def test_review_is_session_scoped(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _packet_missing_origin()
    result = make_audit_service("session_a", analyzer=analyzer).audit_packet(files)

    # Another session can neither see nor review the packet, even with the right id.
    other = _review_service(session_factory, "session_b")
    assert other.get_audit(result.packet_id) is None
    assert other.list_audits() == []
    with pytest.raises(LookupError):
        other.review_finding(
            result.packet_id,
            "doc.certificate_of_origin",
            action=ReviewAction.OVERRIDE,
            status=FindingStatus.PRESENT,
            note=None,
        )
