"""Guardrails for the cost-incurring endpoints.

Two layers, deliberately different in where their state lives:

* **Burst rate limiting** — a short fixed window (per minute/hour), kept in memory: a
  restart harmlessly resets it, and it only shapes bursts.
* **Daily limits** — the per-user free allowance and the instance-wide budget are *counts
  per day*, so they are enforced against durable counters in the database (see
  ``repository.bump_usage``); the numbers and the IP-hash salt are carried here.

All of this protects the shared provider keys (Phase 1). Users who bring their own keys
bypass every layer, since they spend their own quota.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from ragchat.config import Settings


class RateLimiter:
    """In-memory fixed-window rate limiter keyed by an arbitrary string."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self._limit = limit
        self._window = window_seconds
        self._lock = threading.Lock()
        self._state: dict[str, tuple[float, int]] = {}  # key -> (window_start, count)

    def allow(self, key: str, *, now: float | None = None) -> bool:
        """Return True if ``key`` may proceed, counting this call against its window."""
        now = time.monotonic() if now is None else now
        with self._lock:
            start, count = self._state.get(key, (now, 0))
            if now - start >= self._window:
                start, count = now, 0
            count += 1
            self._state[key] = (start, count)
            return count <= self._limit


@dataclass
class Guards:
    """Burst limiters (in-memory) plus the daily-limit numbers (enforced via the DB)."""

    ask_limiter: RateLimiter
    ingest_limiter: RateLimiter
    daily_free_allowance: int  # shared-key asks/day per user (0 = unlimited)
    daily_budget: int  # shared-key asks/day across the instance (0 = unlimited)
    hash_salt: str  # salt for hashing client IPs before storing usage
    trusted_proxy_hops: int = 1  # reverse-proxy hops in front of the app (Render = 1)

    @classmethod
    def from_settings(cls, settings: Settings) -> Guards:
        return cls(
            ask_limiter=RateLimiter(settings.rate_limit_asks_per_minute, 60.0),
            ingest_limiter=RateLimiter(settings.rate_limit_ingests_per_hour, 3600.0),
            daily_free_allowance=settings.daily_free_allowance,
            daily_budget=settings.daily_request_budget,
            hash_salt=settings.usage_hash_salt.get_secret_value(),
            trusted_proxy_hops=settings.trusted_proxy_hops,
        )
