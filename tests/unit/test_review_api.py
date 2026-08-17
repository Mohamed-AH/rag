"""Tests for the reviewer/history endpoints (real services over SQLite, no network).

Unlike ``test_audit_api`` (which fakes the whole audit service), these wire the *real*
``AuditService`` (scripted analyzer) and ``AuditReviewService`` so a packet is genuinely
persisted, listed, re-opened, and reviewed through HTTP.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from ragchat.audit.evidence import ExtractedField
from ragchat.service import AuditReviewService, AuditService
from tests.conftest import FakeAnalysis, FakeAnalyzer

_TEXT = "A trade document with enough readable text to take the router's text path."


def _fields(**pairs: Any) -> dict[str, ExtractedField]:
    return {k: ExtractedField(name=k, value=v, confidence=0.95) for k, v in pairs.items()}


def _analyzer_missing_origin() -> FakeAnalyzer:
    return FakeAnalyzer(
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


@pytest.fixture
def review_client(session_factory: Callable[[], Session]) -> Iterator[TestClient]:
    from ragchat.api.app import create_app
    from ragchat.api.guards import Guards, RateLimiter
    from ragchat.api.routes import (
        get_audit_review_service,
        get_audit_service,
        get_db_session_factory,
    )
    from ragchat.audit.manifest import CUSTOMS_CHECKLIST

    def _audit_service() -> AuditService:
        return AuditService(
            session_id="session_a",
            session_factory=session_factory,
            checklist=CUSTOMS_CHECKLIST,
            analyzer=_analyzer_missing_origin(),
        )

    def _review_service() -> AuditReviewService:
        return AuditReviewService(session_id="session_a", session_factory=session_factory)

    app = create_app()
    app.dependency_overrides[get_audit_service] = _audit_service
    app.dependency_overrides[get_audit_review_service] = _review_service
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.state.guards = Guards(
        ask_limiter=RateLimiter(10_000, 60.0),
        ingest_limiter=RateLimiter(10_000, 3600.0),
        daily_free_allowance=0,
        daily_budget=0,
        hash_salt="test-salt",
    )
    yield TestClient(app)


def _run_audit(client: TestClient) -> str:
    resp = client.post(
        "/audit",
        files=[
            ("files", ("invoice.txt", _TEXT.encode(), "text/plain")),
            ("files", ("packing.txt", _TEXT.encode(), "text/plain")),
            ("files", ("bol.txt", _TEXT.encode(), "text/plain")),
        ],
    )
    assert resp.status_code == 200
    return str(resp.json()["packet_id"])


def test_history_lists_a_completed_audit(review_client: TestClient) -> None:
    packet_id = _run_audit(review_client)

    resp = review_client.get("/audits")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    row = body[0]
    assert row["packet_id"] == packet_id
    assert row["checklist_name"]  # resolved display name
    assert row["missing"] >= 1  # certificate of origin
    assert row["reviewed_count"] == 0
    assert row["is_clear"] is False


def test_reopen_audit_returns_findings_with_machine_status(review_client: TestClient) -> None:
    packet_id = _run_audit(review_client)

    resp = review_client.get(f"/audits/{packet_id}")
    assert resp.status_code == 200
    report = resp.json()["report"]
    coo = next(f for f in report["missing"] if f["requirement_id"] == "doc.certificate_of_origin")
    assert coo["status"] == "missing"
    assert coo["machine_status"] == "missing"
    assert coo["review"] is None


def test_override_moves_finding_and_persists(review_client: TestClient) -> None:
    packet_id = _run_audit(review_client)

    resp = review_client.post(
        f"/audits/{packet_id}/findings/doc.certificate_of_origin/review",
        json={"action": "override", "status": "present", "note": "verified by hand"},
    )
    assert resp.status_code == 200
    report = resp.json()["report"]
    # No longer in missing; now present with the review attached and machine verdict retained.
    assert all(f["requirement_id"] != "doc.certificate_of_origin" for f in report["missing"])
    coo = next(f for f in report["present"] if f["requirement_id"] == "doc.certificate_of_origin")
    assert coo["machine_status"] == "missing"
    assert coo["review"]["action"] == "override"
    assert coo["review"]["note"] == "verified by hand"

    # Persisted across a re-open, and reflected in history counts.
    again = review_client.get(f"/audits/{packet_id}").json()["report"]
    assert all(f["requirement_id"] != "doc.certificate_of_origin" for f in again["missing"])
    assert review_client.get("/audits").json()[0]["reviewed_count"] == 1


def test_override_without_status_is_rejected(review_client: TestClient) -> None:
    packet_id = _run_audit(review_client)
    resp = review_client.post(
        f"/audits/{packet_id}/findings/doc.certificate_of_origin/review",
        json={"action": "override"},
    )
    assert resp.status_code == 422


def test_review_unknown_finding_is_404(review_client: TestClient) -> None:
    packet_id = _run_audit(review_client)
    resp = review_client.post(
        f"/audits/{packet_id}/findings/rule.nope/review",
        json={"action": "accept"},
    )
    assert resp.status_code == 404


def test_reopen_unknown_audit_is_404(review_client: TestClient) -> None:
    resp = review_client.get("/audits/deadbeef")
    assert resp.status_code == 404
