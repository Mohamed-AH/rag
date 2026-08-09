"""Alembic migration environment.

Wired to the application's own settings and metadata: the database URL comes from
``Settings.sqlalchemy_url`` (psycopg3 driver) and the target schema is
``ragchat.db.models.Base.metadata``, so ``alembic revision --autogenerate`` and the
runtime bootstrap (``ragchat.db.engine.init_db``) both stay in sync with the models.
"""

from __future__ import annotations

from alembic import context

from ragchat.config import get_settings
from ragchat.db.models import Base

target_metadata = Base.metadata


def _url() -> str:
    return get_settings().sqlalchemy_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    from ragchat.db.engine import get_engine

    connectable = get_engine()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
