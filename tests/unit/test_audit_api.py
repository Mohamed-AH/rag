"""Tests for the POST /audit endpoint (fake audit service, no network)."""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

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
def audit_client(
    session_factory: Callable[[], Session],
) -> Iterator[tuple[TestClient, _FakeAuditService]]:
    from ragchat.api.app import create_app
    from ragchat.api.guards import Guards, RateLimiter
    from ragchat.api.routes import get_audit_service, get_db_session_factory

    fake = _FakeAuditService()
    app = create_app()
    app.dependency_overrides[get_audit_service] = lambda: fake
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
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
    # A ready-to-send missing-items request is rendered from the report.
    assert "Action Required" in body["request_summary"]
    assert "Packing List is missing" in body["request_summary"]
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


def _audit_app(session_factory: Callable[[], Session], *, allowance: int, budget: int):  # type: ignore[no-untyped-def]
    """Build an app whose /audit guard uses the given shared-key audit limits."""
    from ragchat.api.app import create_app
    from ragchat.api.guards import Guards, RateLimiter
    from ragchat.api.routes import get_audit_service, get_db_session_factory

    app = create_app()
    app.dependency_overrides[get_audit_service] = lambda: _FakeAuditService()
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    app.state.guards = Guards(
        ask_limiter=RateLimiter(10_000, 60.0),
        ingest_limiter=RateLimiter(10_000, 3600.0),
        daily_free_allowance=0,
        daily_budget=0,
        hash_salt="test-salt",
        daily_audit_allowance=allowance,
        daily_audit_budget=budget,
    )
    return app


def _one_file() -> list[tuple[str, tuple[str, bytes, str]]]:
    return [("files", ("a.txt", b"invoice text", "text/plain"))]


def test_audit_daily_allowance_is_ip_keyed_not_cookie(
    session_factory: Callable[[], Session],
) -> None:
    """Dropping the session cookie must NOT reset the audit allowance — it is keyed on the
    client IP, so a fresh session per request cannot evade it."""
    app = _audit_app(session_factory, allowance=2, budget=0)
    # Each request from a brand-new client (separate cookie jar) still counts against one IP.
    assert TestClient(app).post("/audit", files=_one_file()).status_code == 200
    assert TestClient(app).post("/audit", files=_one_file()).status_code == 200
    resp = TestClient(app).post("/audit", files=_one_file())
    assert resp.status_code == 429
    assert resp.json()["detail"]["byok_required"] is True


def test_audit_instance_budget_caps_everyone(
    session_factory: Callable[[], Session],
) -> None:
    app = _audit_app(session_factory, allowance=0, budget=2)
    client = TestClient(app)
    assert client.post("/audit", files=_one_file()).status_code == 200
    assert client.post("/audit", files=_one_file()).status_code == 200
    resp = client.post("/audit", files=_one_file())
    assert resp.status_code == 429
    # The instance budget message is a plain string (not a byok prompt).
    assert "daily audit budget" in resp.json()["detail"].lower()


@pytest.mark.parametrize("header", ["X-Google-Api-Key", "X-Mistral-Api-Key", "X-Groq-Api-Key"])
def test_byo_key_for_any_provider_bypasses_audit_limits(
    session_factory: Callable[[], Session], header: str
) -> None:
    app = _audit_app(session_factory, allowance=1, budget=0)
    client = TestClient(app)
    assert client.post("/audit", files=_one_file()).status_code == 200
    assert client.post("/audit", files=_one_file()).status_code == 429  # shared-key limit hit
    # A bring-your-own key for any of the three providers spends the caller's own quota.
    resp = client.post("/audit", files=_one_file(), headers={header: "byo-test-key"})
    assert resp.status_code == 200


def test_providers_endpoint_reports_ladder(monkeypatch: pytest.MonkeyPatch) -> None:
    import ragchat.config as config_mod
    from ragchat.api.app import create_app
    from ragchat.config import Settings

    settings = Settings(
        database_url="postgresql://u:p@localhost:5432/db",
        cohere_api_key="c",
        google_api_key="g",
        audit_model_order="gemini,groq,bogus",
        groq_api_key="gk",
    )
    monkeypatch.setattr(config_mod, "get_settings", lambda: settings)

    resp = TestClient(create_app()).get("/providers")
    assert resp.status_code == 200
    by_id = {r["id"]: r for r in resp.json()}
    assert by_id["gemini"]["configured"] is True
    assert by_id["groq"]["configured"] is True  # key present
    assert by_id["groq"]["vision_model"] == settings.groq_vision_model
    assert by_id["bogus"]["known"] is False
    assert "gk" not in resp.text  # the endpoint never leaks the key
