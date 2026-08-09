"""Centralised logging configuration.

Emits single-line, timestamped, structured-ish log records that are easy to grep in a
terminal and to ship to a log aggregator when containerised. Call :func:`configure_logging`
once at process start (the API lifespan and the CLI both do this).
"""

from __future__ import annotations

import logging
from typing import Any

_CONFIGURED = False

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(level: str = "INFO") -> None:
    """Configure the root logger exactly once.

    Idempotent: repeated calls (e.g. CLI command + API startup in tests) are no-ops
    after the first, so handlers are never duplicated.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter(fmt=_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

    # Quieten chatty third-party loggers; keep our own namespace at the chosen level.
    for noisy in ("httpx", "httpcore", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True


def get_logger(name: str, **context: Any) -> logging.LoggerAdapter[logging.Logger]:
    """Return a logger bound to ``name`` with optional static context fields."""
    return logging.LoggerAdapter(logging.getLogger(name), extra=context)
