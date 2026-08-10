"""SQLAlchemy models.

The service is multi-tenant: every visitor gets an isolated ``Session``, and all of
their uploaded content is scoped to it. ``KnowledgeBase`` is the *relational source of
truth* for a session's corpus; embeddings are derived from these rows and stored in a
per-session pgvector collection, so a session's index can always be rebuilt from its rows.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class Session(Base):
    """An isolated tenant. Uploaded content and vectors are namespaced by ``id``."""

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    sections: Mapped[list[KnowledgeBase]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    packets: Mapped[list[Packet]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Session(id={self.id!r}, expires_at={self.expires_at!r})"


class KnowledgeBase(Base):
    """A single titled section of source content, owned by one session."""

    __tablename__ = "knowledge_base"
    __table_args__ = (Index("ix_knowledge_base_session_id", "session_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[Session] = relationship(back_populates="sections")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"KnowledgeBase(id={self.id!r}, session_id={self.session_id!r})"


class Packet(Base):
    """A submission packet: the unit of work for an audit, owned by one session.

    A packet groups the several files a shipment/application is reviewed as, and records
    which checklist it was audited against. Its documents cascade-delete with it, and the
    packet itself cascade-deletes with its session — the same tenancy invariant as
    ``KnowledgeBase``.
    """

    __tablename__ = "packets"
    __table_args__ = (Index("ix_packets_session_id", "session_id"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    checklist_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[Session] = relationship(back_populates="packets")
    documents: Mapped[list[PacketDocument]] = relationship(
        back_populates="packet",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Packet(id={self.id!r}, session_id={self.session_id!r})"


class PacketDocument(Base):
    """One file within a :class:`Packet`, with its classification and extracted fields.

    ``doc_type`` / ``classification_confidence`` / ``fields`` are populated by the Phase 1
    analyzer (single-pass classify+extract); in Phase 0 the columns exist but may be null.
    """

    __tablename__ = "packet_documents"
    __table_args__ = (Index("ix_packet_documents_packet_id", "packet_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    packet_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("packets.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    doc_type: Mapped[str | None] = mapped_column(Text, nullable=True)
    classification_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    fields: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    packet: Mapped[Packet] = relationship(back_populates="documents")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PacketDocument(id={self.id!r}, packet_id={self.packet_id!r})"


class UsageCounter(Base):
    """Durable per-day usage counter keyed by scope (e.g. ``ip:<hash>`` or ``global``).

    Lives in the database (not memory) so daily limits survive the free tier's frequent
    restarts — otherwise every cold start would reset everyone's allowance.
    """

    __tablename__ = "usage_counters"

    scope: Mapped[str] = mapped_column(String(96), primary_key=True)
    day: Mapped[str] = mapped_column(String(10), primary_key=True)  # "YYYY-MM-DD" (UTC)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"UsageCounter(scope={self.scope!r}, day={self.day!r}, count={self.count!r})"
