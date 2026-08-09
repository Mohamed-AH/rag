"""Tests for the in-memory burst rate limiter."""

from __future__ import annotations

from ragchat.api.guards import RateLimiter


def test_rate_limiter_allows_up_to_limit_then_blocks() -> None:
    limiter = RateLimiter(limit=3, window_seconds=60)
    assert [limiter.allow("k", now=0) for _ in range(3)] == [True, True, True]
    assert limiter.allow("k", now=1) is False


def test_rate_limiter_window_resets() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow("k", now=0) is True
    assert limiter.allow("k", now=30) is False
    assert limiter.allow("k", now=61) is True  # new window


def test_rate_limiter_keys_are_independent() -> None:
    limiter = RateLimiter(limit=1, window_seconds=60)
    assert limiter.allow("a", now=0) is True
    assert limiter.allow("b", now=0) is True  # different key, own budget
    assert limiter.allow("a", now=0) is False
