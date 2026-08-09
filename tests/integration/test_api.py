"""API tests driving the real FastAPI app with the service dependency overridden.

These are integration-style (full request/response through routing, validation, and
serialization) but self-contained: the injected service uses SQLite + fakes, so no keys
or live database are required.
"""

from __future__ import annotations


def test_ask_returns_answer_and_sources(api_client) -> None:
    response = api_client.post("/ask", json={"question": "What is a VPC?"})
    assert response.status_code == 200
    body = response.json()
    assert "VPC" in body["answer"]
    assert len(body["sources"]) == 2
    assert body["sources"][0]["metadata"]["title"] == "VPC"


def test_ask_rejects_blank_question_with_422(api_client) -> None:
    response = api_client.post("/ask", json={"question": ""})
    assert response.status_code == 422


def test_ask_requires_question_field(api_client) -> None:
    response = api_client.post("/ask", json={})
    assert response.status_code == 422


def test_health_reports_ok_when_db_reachable(api_client) -> None:
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_returns_503_when_db_unreachable(api_client) -> None:
    from ragchat.api.routes import get_db_session_factory

    def _broken_factory():
        def _session():
            raise RuntimeError("db down")

        return _session

    # Point the readiness probe at a factory whose sessions fail.
    api_client.app.dependency_overrides[get_db_session_factory] = _broken_factory
    response = api_client.get("/health")
    assert response.status_code == 503


def test_ingest_file_upload_succeeds(api_client) -> None:
    response = api_client.post(
        "/ingest/file",
        files={"file": ("notes.txt", b"Networking notes about VPCs and subnets.", "text/plain")},
    )
    assert response.status_code == 200
    assert response.json()["sections_written"] >= 1


def test_ingest_file_rejects_unsupported_type(api_client) -> None:
    response = api_client.post(
        "/ingest/file",
        files={"file": ("archive.zip", b"PK\x03\x04", "application/zip")},
    )
    assert response.status_code == 415


def test_ask_provider_error_returns_clean_json_502(api_client) -> None:
    from ragchat.api.routes import get_service

    class _BoomService:
        def ask(self, question: str):
            raise RuntimeError("Error calling model 'x' (NOT_FOUND): model not found")

    api_client.app.dependency_overrides[get_service] = lambda: _BoomService()
    response = api_client.post("/ask", json={"question": "hi"})
    assert response.status_code == 502
    assert "model request failed" in response.json()["detail"]


def test_ask_provider_quota_error_maps_to_429(api_client) -> None:
    from ragchat.api.routes import get_service

    class _QuotaService:
        def ask(self, question: str):
            raise RuntimeError("429 RESOURCE_EXHAUSTED: You exceeded your current quota")

    api_client.app.dependency_overrides[get_service] = lambda: _QuotaService()
    response = api_client.post("/ask", json={"question": "hi"})
    assert response.status_code == 429
    assert "Retry-After" in response.headers
    assert "quota" in response.json()["detail"].lower()


def _guards(**overrides):
    from ragchat.api.guards import Guards, RateLimiter

    base = {
        "ask_limiter": RateLimiter(10_000, 60.0),
        "ingest_limiter": RateLimiter(10_000, 3600.0),
        "daily_free_allowance": 0,
        "daily_budget": 0,
        "hash_salt": "test-salt",
    }
    base.update(overrides)
    return Guards(**base)


def test_ask_is_rate_limited(api_client) -> None:
    from ragchat.api.guards import RateLimiter

    # Install a strict burst limiter: one ask allowed, the next is 429.
    api_client.app.state.guards = _guards(ask_limiter=RateLimiter(1, 60.0))
    first = api_client.post("/ask", json={"question": "What is a VPC?"})
    second = api_client.post("/ask", json={"question": "And a subnet?"})
    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers


def test_free_allowance_gate_prompts_for_byo_keys(api_client) -> None:
    # One free ask/day, then the gate should ask the user to bring their own keys.
    api_client.app.state.guards = _guards(daily_free_allowance=1)
    first = api_client.post("/ask", json={"question": "one"})
    second = api_client.post("/ask", json={"question": "two"})
    assert first.status_code == 200
    assert first.headers.get("X-Free-Remaining") == "0"
    assert second.status_code == 429
    assert second.json()["detail"]["byok_required"] is True


def test_spoofed_forwarded_for_cannot_mint_new_allowance(api_client) -> None:
    # Same real IP (rightmost, added by the trusted proxy) => one shared allowance,
    # even if the client varies the spoofable leftmost X-Forwarded-For entries.
    api_client.app.state.guards = _guards(daily_free_allowance=1)
    r1 = api_client.post(
        "/ask", json={"question": "a"}, headers={"X-Forwarded-For": "9.9.9.9, 5.5.5.5"}
    )
    r2 = api_client.post(
        "/ask", json={"question": "b"}, headers={"X-Forwarded-For": "8.8.8.8, 5.5.5.5"}
    )
    assert r1.status_code == 200
    assert r2.status_code == 429


def test_distinct_ips_get_separate_allowances(api_client) -> None:
    api_client.app.state.guards = _guards(daily_free_allowance=1)
    r1 = api_client.post("/ask", json={"question": "a"}, headers={"X-Forwarded-For": "1.1.1.1"})
    r2 = api_client.post("/ask", json={"question": "b"}, headers={"X-Forwarded-For": "2.2.2.2"})
    assert r1.status_code == 200
    assert r2.status_code == 200


def test_byo_keys_bypass_the_free_allowance(api_client) -> None:
    api_client.app.state.guards = _guards(daily_free_allowance=1)
    headers = {"X-Cohere-Api-Key": "user-cohere", "X-Google-Api-Key": "user-google"}
    # Well beyond the free allowance, but BYO keys bypass shared-key limits entirely.
    for _ in range(3):
        resp = api_client.post("/ask", json={"question": "q"}, headers=headers)
        assert resp.status_code == 200


def test_index_serves_web_ui(api_client) -> None:
    response = api_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "ragchat" in response.text


def test_openapi_schema_is_served(api_client) -> None:
    response = api_client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/ask" in paths
    assert "/ingest/file" in paths
