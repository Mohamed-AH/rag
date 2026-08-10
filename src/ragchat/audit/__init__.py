"""Packet Auditor domain: audit a document packet against a checklist and report gaps.

The package is a clean dependency DAG built bottom-up:

``report`` (outputs) <- ``evidence`` (engine input) <- ``checklist`` (rubric) <- ``engine``.

Everything here is pure — no I/O, no model calls, no database — so the whole audit outcome
is deterministic and unit-testable from hand-built evidence. The analyzer that produces
real evidence from files (single-pass classify+extract) arrives in Phase 1.
"""

from __future__ import annotations

from ragchat.audit.checklist import (
    CUSTOMS_CHECKLIST,
    Checklist,
    DocType,
    DocumentRequirement,
    FieldRule,
    FieldSpec,
    get_checklist,
)
from ragchat.audit.engine import evaluate
from ragchat.audit.evidence import (
    ClassifiedDocument,
    ExtractedField,
    PacketEvidence,
    RuleContext,
    RuleResult,
)
from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer

__all__ = [
    "CUSTOMS_CHECKLIST",
    "Checklist",
    "ClassifiedDocument",
    "DocType",
    "DocumentRequirement",
    "ExtractedField",
    "FieldRule",
    "FieldSpec",
    "Finding",
    "FindingStatus",
    "GapReport",
    "PacketEvidence",
    "RuleContext",
    "RuleResult",
    "SourcePointer",
    "evaluate",
    "get_checklist",
]
