"""Real pgvector round-trip — the one test that needs a live database.

Marked ``integration`` so it's skipped by default (see pyproject ``addopts``) and only
runs where a PostgreSQL + pgvector instance is available — the CI integration job spins
one up in a service container. It still needs no API keys: embeddings and the LLM are
deterministic fakes, but the vector storage, similarity search, and cleanup are all real.
"""

from __future__ import annotations

import os
import uuid

import pytest
from langchain_core.embeddings import DeterministicFakeEmbedding
from langchain_core.language_models.fake_chat_models import FakeListChatModel

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _require_postgres() -> None:
    url = os.getenv("DATABASE_URL", "")
    if not url or "sqlite" in url:
        pytest.skip("requires a real PostgreSQL + pgvector DATABASE_URL")


def test_real_pgvector_ingest_ask_and_purge() -> None:
    from ragchat.config import get_settings
    from ragchat.db.engine import get_session_factory, init_db
    from ragchat.ingestion.parser import Section
    from ragchat.rag.pipeline import build_rag_chain
    from ragchat.rag.vectorstore import build_vector_store, session_collection_name
    from ragchat.service import RAGService

    init_db()
    settings = get_settings()
    session_id = "it" + uuid.uuid4().hex[:8]
    embeddings = DeterministicFakeEmbedding(size=settings.embedding_dimension)
    vector_store = build_vector_store(
        embeddings, settings, collection_name=session_collection_name(session_id)
    )
    service = RAGService(
        session_id=session_id,
        session_factory=get_session_factory(),
        vector_store=vector_store,
        chain=build_rag_chain(
            vector_store.as_retriever(search_kwargs={"k": 3}),
            FakeListChatModel(responses=["a grounded answer"]),
        ),
    )

    try:
        result = service.ingest_sections(
            [
                Section(title="VPC", content="A private, isolated cloud network."),
                Section(title="Subnet", content="A range within a VPC."),
            ]
        )
        assert result.sections_written == 2
        assert service.section_count() == 2

        # Real pgvector similarity search returns this session's documents.
        answer = service.ask("what is a vpc?")
        assert answer.answer == "a grounded answer"
        assert {s.metadata["title"] for s in answer.sources} <= {"VPC", "Subnet"}
        assert len(answer.sources) == 2
    finally:
        service.purge()

    assert service.section_count() == 0
