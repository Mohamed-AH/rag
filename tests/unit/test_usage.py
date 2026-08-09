"""Tests for durable daily usage counters."""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from ragchat.db import repository


def test_bump_usage_increments_per_scope_and_day(
    session_factory: Callable[[], Session],
) -> None:
    with session_factory() as db:
        assert repository.bump_usage(db, "ip:abc", "2026-01-01") == 1
        assert repository.bump_usage(db, "ip:abc", "2026-01-01") == 2
        # Different scope and different day each start fresh.
        assert repository.bump_usage(db, "global", "2026-01-01") == 1
        assert repository.bump_usage(db, "ip:abc", "2026-01-02") == 1
        db.commit()


def test_delete_usage_before(session_factory: Callable[[], Session]) -> None:
    with session_factory() as db:
        repository.bump_usage(db, "ip:x", "2026-01-01")
        repository.bump_usage(db, "ip:x", "2026-01-03")
        db.commit()

    with session_factory() as db:
        removed = repository.delete_usage_before(db, "2026-01-03")
        db.commit()
        assert removed == 1  # only the 2026-01-01 row

    with session_factory() as db:
        assert repository.bump_usage(db, "ip:x", "2026-01-03") == 2  # survivor kept its count
        db.commit()
