"""Tests for the session-scoped RAG service (no keys, no live DB)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy.orm import Session

from ragchat.db import repository
from ragchat.ingestion.parser import Section
from ragchat.service import RAGService

SECTIONS_A = [
    Section(title="VPC", content="A private, isolated cloud network."),
    Section(title="Subnets", content="Ranges within a VPC."),
]
SECTIONS_B = [
    Section(title="IAM", content="Identity and access management."),
]


def test_ask_returns_answer_and_sources(rag_service: RAGService) -> None:
    result = rag_service.ask("What is a VPC?")
    assert "VPC" in result.answer
    assert len(result.sources) == 2
    assert result.sources[0].metadata["title"] == "VPC"


def test_ask_rejects_empty_question(rag_service: RAGService) -> None:
    with pytest.raises(ValueError):
        rag_service.ask("   ")


def test_ingest_writes_relational_and_vectors(
    rag_service: RAGService,
    fake_vector_store,
    session_factory: Callable[[], Session],
) -> None:
    result = rag_service.ingest_sections(SECTIONS_A)

    assert result.sections_written == 2
    with session_factory() as db:
        assert repository.count_sections(db, "session_a") == 2
    # Vector index rebuilt: dropped, recreated, then documents added.
    assert fake_vector_store.delete_collection_calls == 1
    assert fake_vector_store.create_collection_calls == 1
    assert len(fake_vector_store.documents) == 2
    # Documents carry the owning session id in their metadata.
    assert all(d.metadata["session_id"] == "session_a" for d in fake_vector_store.documents)


def test_ingest_is_atomic_on_vector_failure(
    rag_service: RAGService,
    fake_vector_store,
    session_factory: Callable[[], Session],
) -> None:
    fake_vector_store.fail_on_add = True

    with pytest.raises(RuntimeError):
        rag_service.ingest_sections(SECTIONS_A)

    with session_factory() as db:
        assert repository.count_sections(db, "session_a") == 0


def test_health_check_passes_against_live_session(rag_service: RAGService) -> None:
    assert rag_service.health_check() is True


# --- Multi-tenant isolation (the security-critical invariant) --------------


def test_sessions_are_isolated(make_service: Callable[..., RAGService]) -> None:
    """Two sessions sharing one datastore never see or clobber each other's data."""
    svc_a = make_service("session_a")
    svc_b = make_service("session_b")

    svc_a.ingest_sections(SECTIONS_A)
    svc_b.ingest_sections(SECTIONS_B)

    # Each session sees only its own rows...
    assert svc_a.section_count() == 2
    assert svc_b.section_count() == 1


def test_reingest_does_not_touch_other_sessions(
    make_service: Callable[..., RAGService],
) -> None:
    """Re-ingesting one session must not delete another session's rows."""
    svc_a = make_service("session_a")
    svc_b = make_service("session_b")

    svc_a.ingest_sections(SECTIONS_A)
    svc_b.ingest_sections(SECTIONS_B)
    # Re-ingest A with different content; B must be untouched.
    svc_a.ingest_sections([Section(title="NACL", content="Subnet firewall.")])

    assert svc_a.section_count() == 1
    assert svc_b.section_count() == 1


def test_purge_removes_only_its_own_session(
    make_service: Callable[..., RAGService],
) -> None:
    svc_a = make_service("session_a")
    svc_b = make_service("session_b")
    svc_a.ingest_sections(SECTIONS_A)
    svc_b.ingest_sections(SECTIONS_B)

    svc_a.purge()

    assert svc_a.section_count() == 0
    assert svc_b.section_count() == 1
