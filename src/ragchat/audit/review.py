"""Reviewer overlay on a Gap Report — pure domain, no I/O.

An audit produces a machine verdict per requirement (:class:`~ragchat.audit.report.Finding`).
A human reviewer can then **accept** that verdict (confirm the machine was right) or
**override** it (set a different status, e.g. mark a ``needs_review`` field as ``present``
after checking it by hand). This module models that overlay:

* :class:`ReviewAction` — accept vs override.
* :class:`Review` — one reviewer decision (+ optional note and timestamp).
* :class:`ReviewedFinding` — a finding paired with its review; its *effective* status is
  the override when overridden, else the machine's original status.
* :func:`effective_report` — collapse reviewed findings back into a :class:`GapReport`
  keyed on effective status, so ``is_clear`` and the four buckets reflect human decisions.

Like :mod:`ragchat.audit.report`, this is a leaf: it depends only on ``report`` and is
trivially unit-testable. Persistence and HTTP shapes are built on top of it, not inside it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from ragchat.audit.report import Finding, FindingStatus, GapReport


class ReviewAction(StrEnum):
    """What a reviewer did with a finding."""

    ACCEPT = "accept"
    OVERRIDE = "override"


@dataclass(frozen=True, slots=True)
class Review:
    """A single reviewer decision on one finding.

    ``status`` is the reviewer's chosen status; it is required for an ``override`` and is
    kept equal to the machine's status for an ``accept`` (see :func:`normalize_review`).
    """

    action: ReviewAction
    status: FindingStatus
    note: str | None = None
    reviewed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ReviewedFinding:
    """A machine finding paired with an optional human review."""

    finding: Finding
    review: Review | None = None

    @property
    def is_reviewed(self) -> bool:
        return self.review is not None

    @property
    def is_overridden(self) -> bool:
        return self.review is not None and self.review.action is ReviewAction.OVERRIDE

    @property
    def effective_status(self) -> FindingStatus:
        """The status that should count: the override if overridden, else the machine's."""
        if self.review is not None and self.review.action is ReviewAction.OVERRIDE:
            return self.review.status
        return self.finding.status

    def effective_finding(self) -> Finding:
        """The finding as it should be bucketed after review (status swapped if overridden)."""
        if self.effective_status is self.finding.status:
            return self.finding
        return Finding(
            requirement_id=self.finding.requirement_id,
            status=self.effective_status,
            summary=self.finding.summary,
            confidence=self.finding.confidence,
            sources=self.finding.sources,
        )


def normalize_review(
    action: ReviewAction, status: FindingStatus | None, original: FindingStatus
) -> Review:
    """Validate a raw review decision and return a well-formed :class:`Review`.

    * **accept** confirms the machine: the status is pinned to ``original`` (any supplied
      status is ignored — accepting *is* agreeing with the machine).
    * **override** requires an explicit ``status`` that differs from ``original`` — an
      override that changes nothing is a no-op and rejected, so the audit trail stays
      meaningful.
    """
    if action is ReviewAction.ACCEPT:
        return Review(action=action, status=original)
    if status is None:
        raise ValueError("an override must specify the new status")
    if status is original:
        raise ValueError("an override must change the status; use accept to confirm it")
    return Review(action=action, status=status)


def effective_report(reviewed: tuple[ReviewedFinding, ...]) -> GapReport:
    """Collapse reviewed findings into a :class:`GapReport` keyed on effective status."""
    return GapReport(tuple(rf.effective_finding() for rf in reviewed))
