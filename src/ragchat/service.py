"""Application service layer.

``RAGService`` is the single orchestration point the API and CLI both call, and it is
**session-scoped**: each instance is bound to one tenant's ``session_id`` and only ever
reads or writes that tenant's relational rows and pgvector collection. It owns three
flows:

* **ingest** — parse content, write the relational source of truth *and* the per-session
  vector index in one consistent operation;
* **ask** — run the RAG chain and return an answer with its supporting sources;
* **purge** — delete all of a session's data (used by TTL cleanup).

All external dependencies (session factory, vector store, chain) are injected, so tests
construct a service backed by fakes and never touch a real database or LLM. Use
:func:`build_session_service` for the fully wired production instance.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from langchain_core.runnables import Runnable
from sqlalchemy import text
from sqlalchemy.orm import Session

from ragchat.config import Settings, get_settings
from ragchat.db import repository
from ragchat.errors import FileTooLargeError, TooManySectionsError
from ragchat.ingestion.extractors import extract_sections
from ragchat.ingestion.parser import Section, parse_markdown_file

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SourceDocument:
    """A retrieved document backing an answer."""

    content: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class AnswerResult:
    """The result of a question: the generated answer plus its sources."""

    answer: str
    sources: list[SourceDocument]


@dataclass(frozen=True, slots=True)
class IngestResult:
    """Summary of an ingestion run."""

    sections_written: int


@dataclass(frozen=True, slots=True)
class IngestLimits:
    """Upload guardrails applied before any embedding work (and cost) happens."""

    max_upload_bytes: int = 2 * 1024 * 1024
    max_sections: int = 150
    chunk_max_chars: int = 1200
    chunk_overlap: int = 150


class RAGService:
    """Coordinates ingestion and question answering for a single session."""

    def __init__(
        self,
        *,
        session_id: str,
        session_factory: Callable[[], Session],
        vector_store: Any,  # PGVector in production; a test double in tests
        chain: Runnable[str, dict[str, Any]],
        ttl_hours: int = 24,
        limits: IngestLimits | None = None,
    ) -> None:
        self._session_id = session_id
        self._session_factory = session_factory
        self._vector_store = vector_store
        self._chain = chain
        self._ttl_hours = ttl_hours
        self._limits = limits or IngestLimits()

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def max_upload_bytes(self) -> int:
        return self._limits.max_upload_bytes

    # -- Ingestion ---------------------------------------------------------
    def ingest_sections(self, sections: Sequence[Section]) -> IngestResult:
        """Atomically replace *this session's* corpus with ``sections``.

        Ordering guarantees consistency without a distributed transaction: relational
        rows are written but *not committed* until the vector index has been rebuilt
        successfully. If embedding/indexing fails, the relational write is rolled back
        and any partially written vectors are cleared, so the two stores never drift.
        """
        documents = [
            Document(
                page_content=s.as_document_text(),
                metadata={"title": s.title, "session_id": self._session_id},
            )
            for s in sections
        ]

        db = self._session_factory()
        try:
            expires_at = datetime.now(UTC) + timedelta(hours=self._ttl_hours)
            repository.upsert_session(db, self._session_id, expires_at)
            count = repository.replace_all_sections(db, self._session_id, sections)
            # Rebuild this session's vector index from scratch to stay in sync with the
            # table: drop the collection, recreate it, then load the fresh embeddings.
            # (langchain-postgres does not auto-create a collection on add.)
            self._vector_store.delete_collection()
            self._vector_store.create_collection()
            if documents:
                self._vector_store.add_documents(documents)
            db.commit()
            logger.info("Ingested %d sections for session %s", count, self._session_id)
            return IngestResult(sections_written=count)
        except Exception:
            db.rollback()
            # Best-effort cleanup of any vectors written before the failure.
            try:
                self._vector_store.delete_collection()
            except Exception:  # pragma: no cover - cleanup is best-effort
                logger.exception("Failed to clean up vector collection after ingest error")
            logger.exception("Ingestion failed for session %s; rolled back", self._session_id)
            raise
        finally:
            db.close()

    def ingest_markdown_file(self, path: str | Path) -> IngestResult:
        """Parse a markdown file and ingest its sections (CLI/admin path)."""
        sections = parse_markdown_file(path)
        logger.info("Parsed %d sections from %s", len(sections), path)
        return self.ingest_sections(sections)

    def ingest_upload(self, filename: str, data: bytes) -> IngestResult:
        """Ingest an uploaded file (.md/.txt/.pdf/.docx), enforcing size/section caps.

        Caps are checked *before* any embedding call so an oversized or malicious upload
        can't run up provider cost. Raises the typed errors in :mod:`ragchat.errors`.
        """
        if len(data) > self._limits.max_upload_bytes:
            raise FileTooLargeError(
                f"File is {len(data)} bytes; limit is {self._limits.max_upload_bytes}."
            )
        sections = extract_sections(
            filename,
            data,
            chunk_max_chars=self._limits.chunk_max_chars,
            chunk_overlap=self._limits.chunk_overlap,
        )
        if len(sections) > self._limits.max_sections:
            raise TooManySectionsError(
                f"Upload produced {len(sections)} sections; limit is "
                f"{self._limits.max_sections}. Try a smaller file."
            )
        logger.info("Extracted %d sections from upload %s", len(sections), filename)
        return self.ingest_sections(sections)

    # -- Querying ----------------------------------------------------------
    def ask(self, question: str) -> AnswerResult:
        """Answer ``question`` using retrieval-augmented generation over this session."""
        if not question or not question.strip():
            raise ValueError("question must not be empty")

        result = self._chain.invoke(question)
        documents: list[Document] = result.get("documents", [])
        sources = [
            SourceDocument(content=doc.page_content, metadata=dict(doc.metadata))
            for doc in documents
        ]
        return AnswerResult(answer=result["answer"], sources=sources)

    # -- Lifecycle ---------------------------------------------------------
    def purge(self) -> None:
        """Delete all of this session's data (relational rows + vector collection)."""
        db = self._session_factory()
        try:
            repository.delete_session(db, self._session_id)
            db.commit()
        finally:
            db.close()
        try:
            self._vector_store.delete_collection()
        except Exception:  # pragma: no cover - collection may not exist
            logger.exception("Failed to drop vector collection for session %s", self._session_id)

    def section_count(self) -> int:
        """Return how many sections this session currently has stored."""
        db = self._session_factory()
        try:
            return repository.count_sections(db, self._session_id)
        finally:
            db.close()

    # -- Health ------------------------------------------------------------
    def health_check(self) -> bool:
        """Return True if the database answers a trivial query, else raise."""
        db = self._session_factory()
        try:
            db.execute(text("SELECT 1"))
            return True
        finally:
            db.close()


@dataclass(frozen=True, slots=True)
class _Providers:
    """Process-shared, stateless building blocks reused across all sessions."""

    settings: Settings
    embeddings: Any
    llm: Any


@lru_cache(maxsize=1)
def _get_providers() -> _Providers:
    """Build and cache the embedding model and LLM once per process.

    These are keyed only on configuration and carry no per-session state, so they are
    safely shared; only the vector collection and relational scoping vary by session.
    """
    from ragchat.rag.embeddings import build_embeddings
    from ragchat.rag.llm import build_llm

    settings = get_settings()
    return _Providers(
        settings=settings,
        embeddings=build_embeddings(settings),
        llm=build_llm(settings),
    )


def purge_expired_sessions() -> int:
    """Delete every session whose retention window has elapsed (relational + vectors).

    Intended to run on a schedule (see the GitHub Actions cleanup workflow /
    ``ragchat cleanup``). Dropping a collection needs no embedding calls, so a no-op
    embedding is used instead of a real provider — the scheduled job therefore requires
    only ``DATABASE_URL``, not the Cohere/Gemini keys.
    """
    from langchain_core.embeddings import DeterministicFakeEmbedding

    from ragchat.db.engine import get_session_factory, init_db
    from ragchat.rag.vectorstore import build_vector_store, session_collection_name

    init_db()
    settings = get_settings()
    session_factory = get_session_factory()

    # Drop stale daily usage counters (keep only today's).
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    with session_factory() as db:
        removed = repository.delete_usage_before(db, today)
        db.commit()
    if removed:
        logger.info("Deleted %d stale usage counters", removed)

    with session_factory() as db:
        expired = repository.expired_session_ids(db, datetime.now(UTC))

    if not expired:
        logger.info("No expired sessions to purge")
        return 0

    # Placeholder embedding: only needed to satisfy the vector store constructor; no
    # embedding is ever computed on the delete path.
    embeddings = DeterministicFakeEmbedding(size=settings.embedding_dimension)
    for session_id in expired:
        try:
            store = build_vector_store(
                embeddings, settings, collection_name=session_collection_name(session_id)
            )
            store.delete_collection()
        except Exception:  # pragma: no cover - best effort per session
            logger.exception("Failed to drop collection for expired session %s", session_id)
        with session_factory() as db:
            repository.delete_session(db, session_id)
            db.commit()

    logger.info("Purged %d expired sessions", len(expired))
    return len(expired)


def build_session_service(
    session_id: str,
    *,
    cohere_key: str | None = None,
    google_key: str | None = None,
) -> RAGService:
    """Wire a fully configured, session-scoped :class:`RAGService`.

    When both ``cohere_key`` and ``google_key`` are given (bring-your-own-keys), providers
    are built per-request from those keys instead of the cached shared ones; the keys are
    used only to construct the clients and are never persisted or logged.
    """
    from ragchat.db.engine import get_session_factory, init_db
    from ragchat.rag.pipeline import build_rag_chain
    from ragchat.rag.vectorstore import build_vector_store, session_collection_name

    init_db()  # ensure the relational schema exists (idempotent)
    settings = get_settings()

    if cohere_key and google_key:
        from ragchat.rag.embeddings import build_embeddings
        from ragchat.rag.llm import build_llm

        embeddings = build_embeddings(settings, cohere_key=cohere_key)
        llm = build_llm(settings, google_key=google_key)
    else:
        providers = _get_providers()
        embeddings, llm = providers.embeddings, providers.llm

    vector_store = build_vector_store(
        embeddings, settings, collection_name=session_collection_name(session_id)
    )
    retriever = vector_store.as_retriever(search_kwargs={"k": settings.retriever_k})
    chain = build_rag_chain(retriever, llm)

    return RAGService(
        session_id=session_id,
        session_factory=get_session_factory(),
        vector_store=vector_store,
        chain=chain,
        ttl_hours=settings.session_ttl_hours,
        limits=IngestLimits(
            max_upload_bytes=settings.max_upload_bytes,
            max_sections=settings.max_sections_per_upload,
            chunk_max_chars=settings.chunk_max_chars,
            chunk_overlap=settings.chunk_overlap_chars,
        ),
    )
