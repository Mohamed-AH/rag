"""Tests for the missing-items request renderer (multi-vertical, pure)."""

from __future__ import annotations

from ragchat.audit.export import render_request
from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer


def test_clear_report_says_no_action() -> None:
    report = GapReport((Finding("doc.a", FindingStatus.PRESENT, "A present", 1.0),))
    text = render_request(report, checklist_name="Customs Pre-Clearance")
    assert "No action required" in text
    assert "Customs Pre-Clearance" in text


def test_request_lists_gaps_with_counts_and_citations() -> None:
    report = GapReport(
        (
            Finding(
                "doc.commercial_invoice", FindingStatus.PRESENT, "Commercial Invoice present", 1.0
            ),
            Finding(
                "doc.certificate_of_origin",
                FindingStatus.MISSING,
                "Certificate of Origin is missing",
                1.0,
            ),
            Finding(
                "rule.quantity_matches",
                FindingStatus.DEFICIENT,
                "Quantity mismatch: invoice 1,000 vs packing list 2,000",
                1.0,
                (SourcePointer("set2.pdf", page=1), SourcePointer("set2.pdf", page=2)),
            ),
            Finding(
                "rule.weight_count",
                FindingStatus.NEEDS_REVIEW,
                "Weight/carton counts are not numeric — verify manually",
                1.0,
            ),
        )
    )
    text = render_request(report, checklist_name="Customs Pre-Clearance")

    assert "Action Required — Customs Pre-Clearance" in text
    # Header counts.
    assert "1 missing document" in text
    assert "1 discrepancy" in text
    assert "1 item to verify" in text
    # Buckets and their items.
    assert "MISSING (1)" in text and "Certificate of Origin is missing" in text
    assert "DISCREPANCIES (1)" in text
    assert "Quantity mismatch: invoice 1,000 vs packing list 2,000" in text
    # Page-cited evidence.
    assert "(set2.pdf p.1; set2.pdf p.2)" in text
    assert "TO VERIFY (1)" in text
    assert "resubmit" in text.lower()


def test_request_is_multi_vertical() -> None:
    # Any vertical's report renders — the function reads only the unified GapReport.
    report = GapReport(
        (
            Finding("doc.passport", FindingStatus.MISSING, "Passport Photo Page is missing", 1.0),
            Finding(
                "rule.language_score_recent",
                FindingStatus.DEFICIENT,
                "Language test score is older than 2 years",
                1.0,
                (SourcePointer("scorecard.pdf", page=1),),
            ),
        )
    )
    text = render_request(report, checklist_name="Education Admission & Student Visa Audit")
    assert "Education Admission & Student Visa Audit" in text
    assert "Passport Photo Page is missing" in text
    assert "Language test score is older than 2 years  (scorecard.pdf p.1)" in text
