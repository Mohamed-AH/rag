"""SQLAlchemy models.

The service is multi-tenant: every visitor gets an isolated ``Session``, and all of
their uploaded content is scoped to it. ``KnowledgeBase`` is the *relational source of
truth* for a session's corpus; embeddings are derived from these rows and stored in a
per-session pgvector collection, so a session's index can always be rebuilt from its rows.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
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
