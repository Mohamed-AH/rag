"""The Gap Report — the structured output of a packet audit.

Four buckets, not three. ``needs_review`` is the safety valve: when the pipeline cannot
classify a document or read a field with enough confidence, the honest output is "a human
should look here", never a false ``missing``. Every :class:`Finding` carries a confidence
score and the :class:`SourcePointer`\\ s a reviewer needs to verify it in one glance.

This module is the leaf of the audit package's dependency graph — it imports nothing from
its siblings, so ``evidence``, ``checklist``, and ``engine`` can all build on it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class FindingStatus(StrEnum):
    """The four states a requirement can be in after an audit."""

    PRESENT = "present"
    MISSING = "missing"
    DEFICIENT = "deficient"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True, slots=True)
class SourcePointer:
    """Where in the packet a finding's evidence lives, for one-glance verification."""

    doc_id: str
    page: int | None = None
    snippet: str | None = None


@dataclass(frozen=True, slots=True)
class Finding:
    """One requirement's outcome: its status, why, how sure, and where to look."""

    requirement_id: str
    status: FindingStatus
    summary: str
    confidence: float
    sources: tuple[SourcePointer, ...] = ()


@dataclass(frozen=True, slots=True)
class GapReport:
    """The audit result. The four public buckets are derived views over ``findings`` so
    they can never drift out of sync with each other."""

    findings: tuple[Finding, ...] = ()

    def _bucket(self, status: FindingStatus) -> list[Finding]:
        return [f for f in self.findings if f.status is status]

    @property
    def present(self) -> list[Finding]:
        return self._bucket(FindingStatus.PRESENT)

    @property
    def missing(self) -> list[Finding]:
        return self._bucket(FindingStatus.MISSING)

    @property
    def deficient(self) -> list[Finding]:
        return self._bucket(FindingStatus.DEFICIENT)

    @property
    def needs_review(self) -> list[Finding]:
        return self._bucket(FindingStatus.NEEDS_REVIEW)

    @property
    def is_clear(self) -> bool:
        """True when nothing blocks or needs a human: no missing/deficient/needs_review."""
        return not (self.missing or self.deficient or self.needs_review)
