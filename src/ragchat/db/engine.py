"""SQLAlchemy engine and session factory.

The engine is created lazily from :class:`~ragchat.config.Settings` and cached for the
process lifetime, so connection pooling is shared across requests.
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import Engine, create_engine, inspect
from sqlalchemy.orm import Session, sessionmaker

if TYPE_CHECKING:
    from alembic.config import Config

from ragchat.config import Settings, get_settings
from ragchat.db.models import Base

logger = logging.getLogger(__name__)

_schema_ready = False


@lru_cache
def get_engine() -> Engine:
    """Return a cached SQLAlchemy engine using the psycopg3 driver."""
    settings: Settings = get_settings()
    return create_engine(
        settings.sqlalchemy_url,
        pool_pre_ping=True,  # transparently recover from dropped connections
        future=True,
    )


@lru_cache
def get_session_factory() -> sessionmaker[Session]:
    """Return a cached session factory bound to the engine."""
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


def _alembic_config() -> Config:
    """Alembic config pointed at the migrations shipped inside the package."""
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option(
        "script_location", str(Path(__file__).resolve().parent.parent / "migrations")
    )
    return cfg


def init_db() -> None:
    """Ensure the relational schema is current via Alembic; runs once per process.

    Auto-bootstrap that is seamless for both new and pre-existing databases:

    * If the database has no Alembic version yet (a fresh DB, or a legacy one first created
      by ``create_all``), create any missing tables and **stamp** it at ``head`` — adopting
      the existing schema without recreating it.
    * Otherwise, **upgrade** to ``head``, applying any pending migrations.

    The pgvector-managed tables are created by the vector store itself; this owns the
    application's own tables.
    """
    global _schema_ready
    if _schema_ready:
        return

    from alembic import command

    engine = get_engine()
    cfg = _alembic_config()
    if inspect(engine).has_table("alembic_version"):
        command.upgrade(cfg, "head")
    else:
        Base.metadata.create_all(engine)
        command.stamp(cfg, "head")
    _schema_ready = True
    logger.info("Database schema is at Alembic head")
