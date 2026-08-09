"""The gap-analysis engine: a pure function from checklist + evidence to a Gap Report.

The engine does no I/O, no model calls, and no database access — it evaluates an already
classified-and-extracted packet against a checklist. That purity is deliberate: it makes
the whole audit outcome reproducible and unit-testable with hand-built evidence (Phase 0),
independent of the classifier/extractor that will feed it real data (Phase 1).
"""

from __future__ import annotations

from ragchat.audit.checklist import Checklist
from ragchat.audit.evidence import PacketEvidence, RuleContext
from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer

DEFAULT_MIN_CLASSIFICATION_CONFIDENCE = 0.5


def evaluate(
    checklist: Checklist,
    evidence: PacketEvidence,
    *,
    min_classification_confidence: float = DEFAULT_MIN_CLASSIFICATION_CONFIDENCE,
    context: RuleContext | None = None,
) -> GapReport:
    """Evaluate ``evidence`` against ``checklist`` and return a :class:`GapReport`.

    Layer 1 (presence) runs first: each required document type resolves to ``present``,
    ``needs_review`` (a match exists but classification confidence is low), or ``missing``.
    Any uploaded document that maps to no known type is surfaced as ``needs_review`` rather
    than silently dropped. Layer 2 (field rules) then runs only for rules whose required
    document types are all confidently present — so a rule never fires on absent evidence,
    and the missing document is reported once, by Layer 1.
    """
    ctx = context or RuleContext()
    findings: list[Finding] = []
    known_types = checklist.doc_type_ids()

    # Layer 1: document presence.
    for req in checklist.document_requirements:
        matches = evidence.of_type(req.doc_type)
        confident = [d for d in matches if d.confidence >= min_classification_confidence]
        if confident:
            best = max(confident, key=lambda d: d.confidence)
            findings.append(
                Finding(
                    req.id,
                    FindingStatus.PRESENT,
                    f"{req.description} present",
                    best.confidence,
                    (SourcePointer(doc_id=best.doc_id),),
                )
            )
        elif matches:
            best = max(matches, key=lambda d: d.confidence)
            findings.append(
                Finding(
                    req.id,
                    FindingStatus.NEEDS_REVIEW,
                    f"A document may be the {req.description.lower()} but was classified "
                    f"with low confidence — confirm before filing",
                    best.confidence,
                    (SourcePointer(doc_id=best.doc_id),),
                )
            )
        else:
            findings.append(
                Finding(
                    req.id,
                    FindingStatus.MISSING,
                    f"{req.description} is missing from the packet",
                    1.0,
                )
            )

    # Unrecognized documents: a file that maps to no type the checklist knows about.
    # (Low-confidence matches to a *known* type are already covered by Layer 1 above.)
    for doc in evidence.documents:
        if doc.doc_type is None or doc.doc_type not in known_types:
            findings.append(
                Finding(
                    f"doc.unrecognized:{doc.doc_id}",
                    FindingStatus.NEEDS_REVIEW,
                    "Uploaded document could not be matched to any required type",
                    doc.confidence,
                    (SourcePointer(doc_id=doc.doc_id),),
                )
            )

    # Layer 2: field rules, gated on their required document types being confidently present.
    for rule in checklist.field_rules:
        if all(
            _confidently_present(evidence, doc_type, min_classification_confidence)
            for doc_type in rule.doc_types
        ):
            result = rule.check(evidence, ctx)
            findings.append(
                Finding(rule.id, result.status, result.summary, result.confidence, result.sources)
            )

    return GapReport(tuple(findings))


def _confidently_present(evidence: PacketEvidence, doc_type: str, threshold: float) -> bool:
    return any(d.confidence >= threshold for d in evidence.of_type(doc_type))
