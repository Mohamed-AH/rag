"""Tests for the POST /audit endpoint (fake audit service, no network)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer
from ragchat.service import AuditResult

_REPORT = GapReport(
    (
        Finding(
            "doc.commercial_invoice", FindingStatus.PRESENT, "Commercial Invoice present", 0.97
        ),
        Finding("doc.packing_list", FindingStatus.MISSING, "Packing List is missing", 1.0),
        Finding(
            "rule.hts_code",
            FindingStatus.DEFICIENT,
            "HTS code '12' is malformed",
            0.9,
            (SourcePointer("invoice.txt", snippet="HTS: 12"),),
        ),
    )
)


class _FakeAuditService:
    max_files = 25
    max_upload_bytes = 2 * 1024 * 1024

    def __init__(self) -> None:
        self.received: list[tuple[str, bytes]] = []

    def audit_packet(self, files: list[tuple[str, bytes]]) -> AuditResult:
        self.received = files
        return AuditResult(packet_id="pkt123", checklist_id="customs", report=_REPORT)


@pytest.fixture
def audit_client() -> Iterator[tuple[TestClient, _FakeAuditService]]:
    from ragchat.api.app import create_app
    from ragchat.api.guards import Guards, RateLimiter
    from ragchat.api.routes import get_audit_service

    fake = _FakeAuditService()
    app = create_app()
    app.dependency_overrides[get_audit_service] = lambda: fake
    app.state.guards = Guards(
        ask_limiter=RateLimiter(10_000, 60.0),
        ingest_limiter=RateLimiter(10_000, 3600.0),
        daily_free_allowance=0,
        daily_budget=0,
        hash_salt="test-salt",
    )
    yield TestClient(app), fake


def test_audit_returns_gap_report(audit_client: tuple[TestClient, _FakeAuditService]) -> None:
    client, fake = audit_client
    resp = client.post(
        "/audit",
        files=[
            ("files", ("invoice.txt", b"invoice text content here", "text/plain")),
            ("files", ("origin.txt", b"certificate of origin text", "text/plain")),
        ],
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["packet_id"] == "pkt123"
    assert body["checklist_id"] == "customs"
    assert [f["requirement_id"] for f in body["report"]["missing"]] == ["doc.packing_list"]
    assert body["report"]["deficient"][0]["sources"][0]["snippet"] == "HTS: 12"
    assert body["report"]["is_clear"] is False
    # The endpoint forwarded both files to the service.
    assert len(fake.received) == 2


def test_list_checklists_returns_available_verticals(
    audit_client: tuple[TestClient, _FakeAuditService],
) -> None:
    client, _fake = audit_client
    resp = client.get("/checklists")
    assert resp.status_code == 200
    by_id = {c["id"]: c["name"] for c in resp.json()}
    assert "customs" in by_id
    assert "education_admissions" in by_id
    assert by_id["customs"]  # has a display name


def test_audit_rejects_too_many_files(
    audit_client: tuple[TestClient, _FakeAuditService],
) -> None:
    client, fake = audit_client
    fake.max_files = 1
    resp = client.post(
        "/audit",
        files=[
            ("files", ("a.txt", b"aaaaaaaaaa", "text/plain")),
            ("files", ("b.txt", b"bbbbbbbbbb", "text/plain")),
        ],
    )
    assert resp.status_code == 413
