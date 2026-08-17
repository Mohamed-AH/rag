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
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.runnables import Runnable
from sqlalchemy import text
from sqlalchemy.orm import Session

from ragchat.audit.analyzer import Analyzer
from ragchat.audit.checklist import Checklist
from ragchat.audit.engine import evaluate
from ragchat.audit.evidence import ClassifiedDocument, ExtractedField, PacketEvidence
from ragchat.audit.report import Finding, FindingStatus, GapReport, SourcePointer
from ragchat.audit.review import (
    Review,
    ReviewAction,
    ReviewedFinding,
    effective_report,
    normalize_review,
)
from ragchat.config import Settings, get_settings
from ragchat.db import repository
from ragchat.db.models import PacketFinding
from ragchat.errors import FileTooLargeError, TooManyFilesError, TooManySectionsError
from ragchat.ingestion.extractors import extract_sections
from ragchat.ingestion.parser import Section, parse_markdown_file
from ragchat.ingestion.router import DocumentContent, route

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


@dataclass(frozen=True, slots=True)
class AuditResult:
    """The outcome of auditing a packet: its id, the checklist used, and the Gap Report."""

    packet_id: str
    checklist_id: str
    report: GapReport


class AuditService:
    """Audit a submission packet against a checklist, for a single session.

    Orchestrates the Phase 1 pipeline — intake router -> analyzer (single-pass
    classify+extract) -> gap engine — then persists the packet and its per-document
    classification/extraction. All model-facing dependencies are injected
    (``router``/``analyzer``), so tests drive the whole flow with deterministic fakes and
    no network.
    """

    def __init__(
        self,
        *,
        session_id: str,
        session_factory: Callable[[], Session],
        checklist: Checklist,
        analyzer: Analyzer,
        router: Callable[[str, bytes], DocumentContent] = route,
        ttl_hours: int = 24,
        max_files: int = 25,
        max_upload_bytes: int = 2 * 1024 * 1024,
    ) -> None:
        self._session_id = session_id
        self._session_factory = session_factory
        self._checklist = checklist
        self._analyzer = analyzer
        self._router = router
        self._ttl_hours = ttl_hours
        self._max_files = max_files
        self._max_upload_bytes = max_upload_bytes

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def max_files(self) -> int:
        return self._max_files

    @property
    def max_upload_bytes(self) -> int:
        return self._max_upload_bytes

    def audit_packet(self, files: Sequence[tuple[str, bytes]]) -> AuditResult:
        """Audit ``files`` (``(filename, data)`` pairs) and return the Gap Report.

        Enforces the per-packet file cap before any model call. Persists the packet and its
        documents atomically; a mid-run failure rolls back so no partial packet is stored.
        """
        if not files:
            raise ValueError("a packet must contain at least one file")
        if len(files) > self._max_files:
            raise TooManyFilesError(f"Packet has {len(files)} files; limit is {self._max_files}.")

        classified: list[ClassifiedDocument] = []
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for filename, data in files:
            content = self._router(filename, data)
            # One file may contain several documents (a combined packet PDF), so the
            # analyzer returns a list; each detected document becomes its own row.
            for doc in self._analyzer.analyze(filename, content, self._checklist):
                doc = _ensure_unique_id(doc, seen)
                classified.append(doc)
                rows.append(
                    {
                        "filename": filename,
                        "doc_type": doc.doc_type,
                        "classification_confidence": doc.confidence,
                        "fields": _serialize_fields(doc.fields),
                        "raw_text": content.text,
                    }
                )

        report = evaluate(self._checklist, PacketEvidence(tuple(classified)))

        packet_id = uuid4().hex
        db = self._session_factory()
        try:
            expires_at = datetime.now(UTC) + timedelta(hours=self._ttl_hours)
            repository.upsert_session(db, self._session_id, expires_at)
            repository.create_packet(db, packet_id, self._session_id, self._checklist.id)
            for row in rows:
                repository.add_packet_document(db, packet_id=packet_id, **row)
            for position, finding in enumerate(report.findings):
                repository.add_packet_finding(
                    db,
                    packet_id=packet_id,
                    position=position,
                    requirement_id=finding.requirement_id,
                    status=finding.status.value,
                    summary=finding.summary,
                    confidence=finding.confidence,
                    sources=_serialize_sources(finding.sources),
                )
            db.commit()
            logger.info(
                "Audited packet %s (%d files) for session %s",
                packet_id,
                len(files),
                self._session_id,
            )
            return AuditResult(packet_id=packet_id, checklist_id=self._checklist.id, report=report)
        except Exception:
            db.rollback()
            logger.exception("Audit failed for session %s; rolled back", self._session_id)
            raise
        finally:
            db.close()


def _ensure_unique_id(doc: ClassifiedDocument, seen: set[str]) -> ClassifiedDocument:
    """Keep document ids unique across the packet (e.g. same filename twice)."""
    if doc.doc_id not in seen:
        seen.add(doc.doc_id)
        return doc
    n = 1
    while (candidate := f"{doc.doc_id} #{n}") in seen:
        n += 1
    seen.add(candidate)
    return replace(doc, doc_id=candidate)


def _serialize_sources(sources: tuple[SourcePointer, ...]) -> list[Any] | None:
    """Render a finding's source pointers to a JSON-storable list (or ``None`` when empty)."""
    if not sources:
        return None
    return [{"doc_id": s.doc_id, "page": s.page, "snippet": s.snippet} for s in sources]


def _serialize_fields(fields: dict[str, ExtractedField]) -> dict[str, Any] | None:
    """Render extracted fields to a JSON-storable dict (or ``None`` when empty)."""
    if not fields:
        return None
    out: dict[str, Any] = {}
    for name, field in fields.items():
        out[name] = {
            "value": field.value,
            "confidence": field.confidence,
            "snippet": field.source.snippet if field.source is not None else None,
        }
    return out


def build_audit_service(
    session_id: str, *, google_key: str | None = None, checklist_id: str | None = None
) -> AuditService:
    """Wire a fully configured, session-scoped :class:`AuditService`.

    The audit path needs only the Gemini model (no embeddings / pgvector). ``google_key``,
    when supplied, is a bring-your-own key used to build the model per request and never
    persisted. ``checklist_id`` selects the vertical manifest, defaulting to the configured
    ``active_checklist``; an unknown id raises :class:`~ragchat.errors.UnknownChecklistError`.
    """
    from ragchat.audit.analyzer import GeminiAnalyzer
    from ragchat.audit.manifest import get_checklist
    from ragchat.db.engine import get_session_factory, init_db
    from ragchat.ingestion.router import IMAGE
    from ragchat.rag.llm import build_llm, build_vision_llm

    init_db()
    settings = get_settings()
    # Route by path to conserve quota: text-path documents use the cheaper, higher
    # free-quota lite chat model; only scans/images spend the multimodal model. Retries
    # are bounded so a quota/rate error fails fast instead of tying up a worker.
    text_llm = build_llm(settings, google_key=google_key, max_retries=2)
    vision_llm = build_vision_llm(settings, google_key=google_key, max_retries=2)

    def select_llm(content: DocumentContent) -> Any:
        return vision_llm if content.mode == IMAGE else text_llm

    checklist = get_checklist(checklist_id or settings.active_checklist)

    return AuditService(
        session_id=session_id,
        session_factory=get_session_factory(),
        checklist=checklist,
        analyzer=GeminiAnalyzer(select_llm),
        ttl_hours=settings.session_ttl_hours,
        max_files=settings.max_files_per_packet,
        max_upload_bytes=settings.max_upload_bytes,
    )


@dataclass(frozen=True, slots=True)
class AuditSummary:
    """A past audit as it appears in the history list: id, vertical, when, and effective counts."""

    packet_id: str
    checklist_id: str
    created_at: datetime
    report: GapReport  # effective (post-review) — buckets/counts reflect human decisions
    reviewed_count: int


@dataclass(frozen=True, slots=True)
class StoredAudit:
    """A re-opened audit: its persisted findings with any reviewer decisions applied."""

    packet_id: str
    checklist_id: str
    created_at: datetime
    findings: tuple[ReviewedFinding, ...]

    @property
    def report(self) -> GapReport:
        """The Gap Report keyed on effective (post-review) status."""
        return effective_report(self.findings)


def _row_to_reviewed_finding(row: PacketFinding) -> ReviewedFinding:
    """Reconstruct a :class:`ReviewedFinding` from a persisted ``packet_findings`` row."""
    sources = tuple(
        SourcePointer(doc_id=s["doc_id"], page=s.get("page"), snippet=s.get("snippet"))
        for s in (row.sources or [])
    )
    finding = Finding(
        requirement_id=row.requirement_id,
        status=FindingStatus(row.status),
        summary=row.summary,
        confidence=row.confidence,
        sources=sources,
    )
    review: Review | None = None
    if row.review_action is not None and row.review_status is not None:
        review = Review(
            action=ReviewAction(row.review_action),
            status=FindingStatus(row.review_status),
            note=row.review_note,
            reviewed_at=row.reviewed_at,
        )
    return ReviewedFinding(finding=finding, review=review)


class AuditReviewService:
    """Read past audits and record reviewer decisions, for a single session.

    Purely relational — no model calls, no keys — so it is cheap to build per request and
    trivially testable. Every method is scoped to ``session_id``: one tenant can never read
    or review another's audit, even by guessing a packet id.
    """

    def __init__(self, *, session_id: str, session_factory: Callable[[], Session]) -> None:
        self._session_id = session_id
        self._session_factory = session_factory

    def list_audits(self, limit: int = 25) -> list[AuditSummary]:
        """Return this session's recent audits (newest first) with effective status counts."""
        db = self._session_factory()
        try:
            packets = repository.recent_packets(db, self._session_id, limit)
            rows = repository.findings_for_packets(db, [p.id for p in packets])
            by_packet: dict[str, list[PacketFinding]] = {}
            for row in rows:
                by_packet.setdefault(row.packet_id, []).append(row)
            summaries: list[AuditSummary] = []
            for packet in packets:
                reviewed = tuple(_row_to_reviewed_finding(r) for r in by_packet.get(packet.id, []))
                summaries.append(
                    AuditSummary(
                        packet_id=packet.id,
                        checklist_id=packet.checklist_id,
                        created_at=packet.created_at,
                        report=effective_report(reviewed),
                        reviewed_count=sum(1 for rf in reviewed if rf.is_reviewed),
                    )
                )
            return summaries
        finally:
            db.close()

    def get_audit(self, packet_id: str) -> StoredAudit | None:
        """Return a re-opened audit, or ``None`` if it isn't this session's."""
        db = self._session_factory()
        try:
            packet = repository.get_packet(db, self._session_id, packet_id)
            if packet is None:
                return None
            rows = repository.list_packet_findings(db, self._session_id, packet_id)
            return StoredAudit(
                packet_id=packet.id,
                checklist_id=packet.checklist_id,
                created_at=packet.created_at,
                findings=tuple(_row_to_reviewed_finding(r) for r in rows),
            )
        finally:
            db.close()

    def review_finding(
        self,
        packet_id: str,
        requirement_id: str,
        *,
        action: ReviewAction,
        status: FindingStatus | None,
        note: str | None,
    ) -> StoredAudit:
        """Record an accept/override on one finding and return the refreshed stored audit.

        Raises :class:`LookupError` if the finding isn't this session's, and ``ValueError``
        (from :func:`~ragchat.audit.review.normalize_review`) if the decision is malformed.
        """
        db = self._session_factory()
        try:
            row = repository.get_packet_finding(db, self._session_id, packet_id, requirement_id)
            if row is None:
                raise LookupError(f"no finding {requirement_id!r} in packet {packet_id!r}")
            review = normalize_review(action, status, FindingStatus(row.status))
            repository.apply_finding_review(
                db,
                row,
                action=review.action.value,
                status=review.status.value,
                note=(note or None),
                reviewed_at=datetime.now(UTC),
            )
            rows = repository.list_packet_findings(db, self._session_id, packet_id)
            findings = tuple(_row_to_reviewed_finding(r) for r in rows)
            checklist_id = row.packet.checklist_id
            created_at = row.packet.created_at
            db.commit()
            return StoredAudit(
                packet_id=packet_id,
                checklist_id=checklist_id,
                created_at=created_at,
                findings=findings,
            )
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


def build_audit_review_service(session_id: str) -> AuditReviewService:
    """Wire a session-scoped :class:`AuditReviewService` (relational only; no keys needed)."""
    from ragchat.db.engine import get_session_factory, init_db

    init_db()
    return AuditReviewService(session_id=session_id, session_factory=get_session_factory())


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
