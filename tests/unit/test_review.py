"""Tests for the reviewer overlay (pure domain: accept/override, effective status)."""

from __future__ import annotations

import pytest

from ragchat.audit.report import Finding, FindingStatus, GapReport
from ragchat.audit.review import (
    Review,
    ReviewAction,
    ReviewedFinding,
    effective_report,
    normalize_review,
)


def _finding(status: FindingStatus) -> Finding:
    return Finding("rule.x", status, "summary", 0.6)


def test_unreviewed_finding_keeps_machine_status() -> None:
    rf = ReviewedFinding(_finding(FindingStatus.NEEDS_REVIEW))
    assert rf.is_reviewed is False
    assert rf.is_overridden is False
    assert rf.effective_status is FindingStatus.NEEDS_REVIEW
    assert rf.effective_finding() is rf.finding  # unchanged identity when nothing overrides


def test_accept_confirms_machine_status() -> None:
    review = normalize_review(ReviewAction.ACCEPT, None, FindingStatus.DEFICIENT)
    rf = ReviewedFinding(_finding(FindingStatus.DEFICIENT), review)
    assert rf.is_reviewed is True
    assert rf.is_overridden is False
    assert rf.effective_status is FindingStatus.DEFICIENT


def test_accept_ignores_any_supplied_status() -> None:
    # Accepting *is* agreeing with the machine — a supplied status is pinned to the original.
    review = normalize_review(ReviewAction.ACCEPT, FindingStatus.PRESENT, FindingStatus.MISSING)
    assert review == Review(ReviewAction.ACCEPT, FindingStatus.MISSING)


def test_override_swaps_effective_status() -> None:
    review = normalize_review(
        ReviewAction.OVERRIDE, FindingStatus.PRESENT, FindingStatus.NEEDS_REVIEW
    )
    rf = ReviewedFinding(_finding(FindingStatus.NEEDS_REVIEW), review)
    assert rf.is_overridden is True
    assert rf.effective_status is FindingStatus.PRESENT
    assert rf.effective_finding().status is FindingStatus.PRESENT
    # Machine finding is untouched.
    assert rf.finding.status is FindingStatus.NEEDS_REVIEW


def test_override_requires_a_status() -> None:
    with pytest.raises(ValueError, match="must specify the new status"):
        normalize_review(ReviewAction.OVERRIDE, None, FindingStatus.MISSING)


def test_override_must_change_the_status() -> None:
    with pytest.raises(ValueError, match="must change the status"):
        normalize_review(ReviewAction.OVERRIDE, FindingStatus.MISSING, FindingStatus.MISSING)


def test_effective_report_rebuckets_by_review() -> None:
    # A missing doc overridden to present, and a needs_review accepted (stays needs_review).
    missing = ReviewedFinding(
        _finding(FindingStatus.MISSING),
        normalize_review(ReviewAction.OVERRIDE, FindingStatus.PRESENT, FindingStatus.MISSING),
    )
    review_it = ReviewedFinding(
        _finding(FindingStatus.NEEDS_REVIEW),
        normalize_review(ReviewAction.ACCEPT, None, FindingStatus.NEEDS_REVIEW),
    )
    report = effective_report((missing, review_it))
    assert isinstance(report, GapReport)
    assert len(report.present) == 1
    assert len(report.missing) == 0
    assert len(report.needs_review) == 1
    assert report.is_clear is False  # still blocked by the accepted needs_review
