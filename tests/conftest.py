"""Shared test fixtures and fakes.

The whole suite runs with **no API keys and no live database**:

* the relational layer is exercised against real SQLite in-memory (so models and the
  repository are genuinely tested, including per-session scoping and cascade delete), and
* embeddings / LLM / pgvector are replaced with deterministic fakes.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from fastapi.testclient import TestClient
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.retrievers import BaseRetriever
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from ragchat.db.models import Base
from ragchat.rag.pipeline import build_rag_chain
from ragchat.service import IngestLimits, RAGService

# --- Fakes ----------------------------------------------------------------


class FakeRetriever(BaseRetriever):
    """Retriever that always returns a fixed set of documents."""

    documents: list[Document]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return self.documents


class FakeVectorStore:
    """Minimal stand-in for ``PGVector`` that records ingestion calls."""

    def __init__(self) -> None:
        self.documents: list[Document] = []
        self.delete_collection_calls = 0
        self.create_collection_calls = 0
        self.fail_on_add = False

    def delete_collection(self) -> None:
        self.delete_collection_calls += 1
        self.documents = []

    def create_collection(self) -> None:
        self.create_collection_calls += 1

    def add_documents(self, documents: list[Document]) -> None:
        if self.fail_on_add:
            raise RuntimeError("simulated embedding failure")
        self.documents.extend(documents)


# --- Fixtures -------------------------------------------------------------


@pytest.fixture
def session_factory() -> Callable[[], Session]:
    """A real SQLite in-memory session factory with FK cascade enabled.

    StaticPool + check_same_thread=False keeps a single shared in-memory database across
    connections/threads, so schema created here is visible to FastAPI's worker-thread
    handlers (each connection to a plain ``sqlite://`` would otherwise get its own DB).
    """
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enable_fk(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@pytest.fixture
def sample_documents() -> list[Document]:
    return [
        Document(
            page_content="A VPC is a private, isolated cloud network.",
            metadata={"title": "VPC"},
        ),
        Document(
            page_content="Subnets divide a VPC into smaller ranges.",
            metadata={"title": "Subnets"},
        ),
    ]


@pytest.fixture
def fake_vector_store() -> FakeVectorStore:
    return FakeVectorStore()


@pytest.fixture
def make_service(
    session_factory: Callable[[], Session],
) -> Callable[..., RAGService]:
    """Factory building a session-scoped service backed by SQLite + fakes.

    Sessions built from the same fixture share one SQLite database, which is exactly
    what the isolation tests need: two tenants, one datastore, no cross-contamination.
    """

    def _make(
        session_id: str,
        *,
        vector_store: FakeVectorStore | None = None,
        documents: list[Document] | None = None,
        answer: str = "A VPC is a private, isolated network in the cloud.",
        limits: IngestLimits | None = None,
    ) -> RAGService:
        retriever = FakeRetriever(documents=documents or [])
        chain = build_rag_chain(retriever, FakeListChatModel(responses=[answer]))
        return RAGService(
            session_id=session_id,
            session_factory=session_factory,
            vector_store=vector_store or FakeVectorStore(),
            chain=chain,
            ttl_hours=24,
            limits=limits,
        )

    return _make


@pytest.fixture
def rag_service(
    make_service: Callable[..., RAGService],
    fake_vector_store: FakeVectorStore,
    sample_documents: list[Document],
) -> RAGService:
    """A single session-scoped service wired with SQLite + fakes."""
    return make_service("session_a", vector_store=fake_vector_store, documents=sample_documents)


@pytest.fixture
def api_client(
    rag_service: RAGService, session_factory: Callable[[], Session]
) -> Iterator[TestClient]:
    """A FastAPI TestClient with the service + DB dependencies overridden by fakes."""
    from ragchat.api.app import create_app
    from ragchat.api.guards import Guards, RateLimiter
    from ragchat.api.routes import get_db_session_factory, get_service

    app = create_app()
    app.dependency_overrides[get_service] = lambda: rag_service
    app.dependency_overrides[get_db_session_factory] = lambda: session_factory
    # Permissive guardrails so ordinary tests don't trip limits (and don't need env).
    app.state.guards = Guards(
        ask_limiter=RateLimiter(10_000, 60.0),
        ingest_limiter=RateLimiter(10_000, 3600.0),
        daily_free_allowance=0,  # unlimited
        daily_budget=0,  # unlimited
        hash_salt="test-salt",
    )
    # Construct the client WITHOUT the `with` block: entering it would run the real
    # lifespan (which needs a real database). The overridden dependencies suffice.
    yield TestClient(app)
