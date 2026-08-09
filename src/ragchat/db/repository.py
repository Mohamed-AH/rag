"""Data-access helpers, all scoped by ``session_id`` for tenant isolation.

Keeps SQLAlchemy usage in one place so the service layer works with plain domain
objects (``Section``) and never issues an unscoped query — the single most important
invariant for multi-tenancy is that one session can neither read nor delete another's rows.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.orm import Session as DbSession

from ragchat.db.models import KnowledgeBase, Session, UsageCounter
from ragchat.ingestion.parser import Section

# --- Sessions -------------------------------------------------------------


def upsert_session(db: DbSession, session_id: str, expires_at: datetime) -> None:
    """Create the session row if absent, or refresh its expiry if present."""
    existing = db.get(Session, session_id)
    if existing is None:
        db.add(Session(id=session_id, expires_at=expires_at))
    else:
        existing.expires_at = expires_at
    db.flush()


def delete_session(db: DbSession, session_id: str) -> None:
    """Delete a session and (via cascade) all of its sections."""
    obj = db.get(Session, session_id)
    if obj is not None:
        db.delete(obj)
        db.flush()


def expired_session_ids(db: DbSession, now: datetime) -> list[str]:
    """Return ids of sessions whose retention window has elapsed."""
    stmt = select(Session.id).where(Session.expires_at < now)
    return list(db.execute(stmt).scalars().all())


# --- Sections (always scoped by session) ----------------------------------


def replace_all_sections(db: DbSession, session_id: str, sections: Sequence[Section]) -> int:
    """Replace *this session's* corpus with ``sections`` within the caller's transaction.

    Only rows belonging to ``session_id`` are deleted, so re-ingesting for one session
    never touches another's data. Runs in the caller's transaction (commit/rollback is
    theirs), so a mid-ingest failure never leaves the table half-populated.
    """
    db.execute(delete(KnowledgeBase).where(KnowledgeBase.session_id == session_id))
    db.add_all(
        [KnowledgeBase(session_id=session_id, title=s.title, content=s.content) for s in sections]
    )
    db.flush()
    return len(sections)


def count_sections(db: DbSession, session_id: str) -> int:
    """Return the number of rows owned by ``session_id``."""
    stmt = (
        select(func.count())
        .select_from(KnowledgeBase)
        .where(KnowledgeBase.session_id == session_id)
    )
    return db.execute(stmt).scalar_one()


def get_all_sections(db: DbSession, session_id: str) -> list[Section]:
    """Return every section owned by ``session_id`` as domain objects."""
    stmt = (
        select(KnowledgeBase)
        .where(KnowledgeBase.session_id == session_id)
        .order_by(KnowledgeBase.id)
    )
    rows = db.execute(stmt).scalars().all()
    return [Section(title=row.title, content=row.content) for row in rows]


# --- Usage counters (durable daily metering) ------------------------------


def bump_usage(db: DbSession, scope: str, day: str) -> int:
    """Increment and return the counter for ``(scope, day)``, creating it if needed.

    Read-modify-write within the caller's transaction. On a single instance with short
    transactions this is sufficient; a multi-instance deployment would use an atomic
    upsert (or Redis) instead.
    """
    row = db.get(UsageCounter, (scope, day))
    if row is None:
        row = UsageCounter(scope=scope, day=day, count=0)
        db.add(row)
    row.count += 1
    db.flush()
    return row.count


def delete_usage_before(db: DbSession, day: str) -> int:
    """Delete usage rows for days strictly before ``day``; return how many were removed."""
    result = cast(
        "CursorResult[Any]", db.execute(delete(UsageCounter).where(UsageCounter.day < day))
    )
    return result.rowcount or 0
