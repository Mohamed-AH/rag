"""Hermetic end-to-end eval of the Customs audit over gold-standard packets.

Runs each labelled packet through the real router + gap engine + persistence with a
scripted model, then measures precision/recall of the *gap* findings (missing / deficient /
needs_review) against the expected requirement ids. For the gold set the pipeline must be
exact — any invented or dropped gap fails the gate, which is precisely the regression a
prompt or checklist change could otherwise slip past before a live demo.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from ragchat.audit.classifier import Classification
from ragchat.audit.evidence import ExtractedField
from ragchat.service import AuditService
from tests.conftest import FakeClassifier, FakeExtractor
from tests.eval.customs_packets import DOC_TEXT, PACKETS, EvalPacket


def _run(packet: EvalPacket, make_audit_service: Callable[..., AuditService]) -> set[str]:
    """Audit a gold packet and return the requirement ids flagged as gaps."""
    files = [(doc.filename, DOC_TEXT) for doc in packet.docs]
    classifier = FakeClassifier(
        {doc.filename: Classification(doc.doc_type, doc.confidence) for doc in packet.docs}
    )
    extractor = FakeExtractor(
        {
            doc.filename: {
                name: ExtractedField(name=name, value=value, confidence=0.95)
                for name, value in doc.fields.items()
            }
            for doc in packet.docs
        }
    )
    service = make_audit_service(classifier=classifier, extractor=extractor)
    report = service.audit_packet(files).report
    return {f.requirement_id for f in (*report.missing, *report.deficient, *report.needs_review)}


@pytest.mark.parametrize("packet", PACKETS, ids=lambda p: p.name)
def test_packet_gaps_match_expected(
    packet: EvalPacket, make_audit_service: Callable[..., AuditService]
) -> None:
    predicted = _run(packet, make_audit_service)
    assert predicted == packet.expected_gaps, (
        f"{packet.name}: expected {sorted(packet.expected_gaps)}, got {sorted(predicted)}"
    )


def test_gold_set_precision_and_recall_are_perfect(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    """Aggregate precision/recall of gap findings across the whole gold set."""
    tp = fp = fn = 0
    for packet in PACKETS:
        predicted = _run(packet, make_audit_service)
        expected = packet.expected_gaps
        tp += len(predicted & expected)
        fp += len(predicted - expected)
        fn += len(expected - predicted)

    precision = tp / (tp + fp) if (tp + fp) else 1.0
    recall = tp / (tp + fn) if (tp + fn) else 1.0
    assert precision == 1.0, f"precision {precision:.3f} (fp={fp})"
    assert recall == 1.0, f"recall {recall:.3f} (fn={fn})"
