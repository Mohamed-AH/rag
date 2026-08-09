"""initial schema: sessions, knowledge_base, usage_counters

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-08

Mirrors ``ragchat.db.models`` at the time multi-tenancy + usage metering landed. On an
existing database created by ``create_all`` this migration is *stamped* (not run) by the
bootstrap in ``ragchat.db.engine.init_db``; future schema changes get their own revisions.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "knowledge_base",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_knowledge_base_session_id", "knowledge_base", ["session_id"], unique=False)
    op.create_table(
        "usage_counters",
        sa.Column("scope", sa.String(length=96), nullable=False),
        sa.Column("day", sa.String(length=10), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("scope", "day"),
    )


def downgrade() -> None:
    op.drop_table("usage_counters")
    op.drop_index("ix_knowledge_base_session_id", table_name="knowledge_base")
    op.drop_table("knowledge_base")
    op.drop_table("sessions")
