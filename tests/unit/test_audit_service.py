"""End-to-end tests for the audit pipeline (router -> analyzer fake -> engine -> DB).

The analyzer is a scripted fake, so these exercise the real router, the real gap engine,
and real SQLite persistence with no network and no API keys.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy.orm import Session

from ragchat.audit.evidence import ExtractedField
from ragchat.audit.report import FindingStatus
from ragchat.db import repository
from ragchat.errors import TooManyFilesError
from ragchat.service import AuditService
from tests.conftest import FakeAnalysis, FakeAnalyzer

_TEXT = "A trade document with enough readable text to take the router's text path."


def _fields(**pairs: Any) -> dict[str, ExtractedField]:
    return {k: ExtractedField(name=k, value=v, confidence=0.95) for k, v in pairs.items()}


def _complete_packet() -> tuple[list[tuple[str, bytes]], FakeAnalyzer]:
    files = [
        ("invoice.txt", _TEXT.encode()),
        ("packing.txt", _TEXT.encode()),
        ("bol.txt", _TEXT.encode()),
        ("origin.txt", _TEXT.encode()),
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
            "origin.txt": FakeAnalysis(
                "certificate_of_origin", 0.94, _fields(country_of_origin="China")
            ),
        }
    )
    return files, analyzer


def test_complete_packet_is_clear_and_persisted(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _complete_packet()
    service = make_audit_service(analyzer=analyzer)

    result = service.audit_packet(files)

    assert result.report.is_clear is True
    assert result.checklist_id == "customs"
    with session_factory() as db:
        packet = repository.get_packet(db, "session_a", result.packet_id)
        assert packet is not None
        docs = repository.list_packet_documents(db, "session_a", result.packet_id)
        assert len(docs) == 4
        invoice = next(d for d in docs if d.filename == "invoice.txt")
        assert invoice.doc_type == "commercial_invoice"
        assert invoice.fields is not None and invoice.fields["hts_code"]["value"] == "8471.30.01"


def test_missing_document_is_reported(
    make_audit_service: Callable[..., AuditService],
) -> None:
    files, analyzer = _complete_packet()
    files = [f for f in files if f[0] != "packing.txt"]  # drop the packing list
    service = make_audit_service(analyzer=analyzer)

    report = service.audit_packet(files).report

    assert not report.is_clear
    assert any(f.requirement_id == "doc.packing_list" for f in report.missing)


def test_deficient_field_surfaces_as_gap(
    make_audit_service: Callable[..., AuditService],
) -> None:
    files, analyzer = _complete_packet()
    # Break the HTS code on the invoice.
    analyzer._by_filename["invoice.txt"].fields["hts_code"] = ExtractedField("hts_code", "12", 0.95)
    service = make_audit_service(analyzer=analyzer)

    report = service.audit_packet(files).report
    assert any(f.requirement_id == "rule.hts_code" for f in report.deficient)


def test_unrecognized_file_needs_review(
    make_audit_service: Callable[..., AuditService],
) -> None:
    files, analyzer = _complete_packet()
    files.append(("mystery.txt", _TEXT.encode()))  # unmapped -> classified as None
    service = make_audit_service(analyzer=analyzer)

    report = service.audit_packet(files).report
    assert any(f.requirement_id == "doc.unrecognized:mystery.txt" for f in report.needs_review)


def test_low_confidence_classification_needs_review(
    make_audit_service: Callable[..., AuditService],
) -> None:
    files, analyzer = _complete_packet()
    analyzer._by_filename["bol.txt"].confidence = 0.20  # unsure
    service = make_audit_service(analyzer=analyzer)

    report = service.audit_packet(files).report
    review = next(f for f in report.needs_review if f.requirement_id == "doc.bill_of_lading")
    assert review.status is FindingStatus.NEEDS_REVIEW


def test_too_many_files_rejected(
    make_audit_service: Callable[..., AuditService],
) -> None:
    files, analyzer = _complete_packet()
    service = make_audit_service(analyzer=analyzer, max_files=2)
    with pytest.raises(TooManyFilesError):
        service.audit_packet(files)


def test_empty_packet_rejected(
    make_audit_service: Callable[..., AuditService],
) -> None:
    _files, analyzer = _complete_packet()
    service = make_audit_service(analyzer=analyzer)
    with pytest.raises(ValueError):
        service.audit_packet([])


def test_combined_file_yields_multiple_documents(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    """One uploaded file (a combined packet PDF) whose pages are four different documents
    must be split into all four — not collapsed into a single document."""
    analyzer = FakeAnalyzer(
        {
            "packet.txt": [
                FakeAnalysis(
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
                FakeAnalysis(
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
                FakeAnalysis(
                    "bill_of_lading",
                    0.95,
                    _fields(
                        net_weight="500",
                        total_cartons="20",
                        exporter="Acme Ltd",
                        consignee="Globex Inc",
                    ),
                ),
                FakeAnalysis("certificate_of_origin", 0.94, _fields(country_of_origin="China")),
            ]
        }
    )
    service = make_audit_service(analyzer=analyzer)

    result = service.audit_packet([("packet.txt", _TEXT.encode())])

    assert result.report.is_clear is True
    with session_factory() as db:
        docs = repository.list_packet_documents(db, "session_a", result.packet_id)
        assert len(docs) == 4
        assert {d.doc_type for d in docs} == {
            "commercial_invoice",
            "packing_list",
            "bill_of_lading",
            "certificate_of_origin",
        }
        assert {d.filename for d in docs} == {"packet.txt"}


def test_sessions_are_isolated(
    make_audit_service: Callable[..., AuditService],
    session_factory: Callable[[], Session],
) -> None:
    files, analyzer = _complete_packet()
    svc_a = make_audit_service("session_a", analyzer=analyzer)
    result_a = svc_a.audit_packet(files)

    with session_factory() as db:
        assert repository.get_packet(db, "session_b", result_a.packet_id) is None
