"""packet auditor schema: packets, packet_documents

Revision ID: 0002_packet_auditor
Revises: 0001_initial
Create Date: 2026-08-09

Adds the Packet Auditor tables alongside the existing knowledge-base schema. A ``Packet``
is a submission reviewed as one unit; ``PacketDocument`` is a single file within it, with
its classification and extracted fields (populated by the Phase 1 pipeline). Both cascade
from ``sessions`` so the existing TTL purge cleans them up unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_packet_auditor"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "packets",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("checklist_id", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["session_id"], ["sessions.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_packets_session_id", "packets", ["session_id"], unique=False)
    op.create_table(
        "packet_documents",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("packet_id", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("doc_type", sa.Text(), nullable=True),
        sa.Column("classification_confidence", sa.Float(), nullable=True),
        sa.Column("fields", sa.JSON(), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["packet_id"], ["packets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_packet_documents_packet_id", "packet_documents", ["packet_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_packet_documents_packet_id", table_name="packet_documents")
    op.drop_table("packet_documents")
    op.drop_index("ix_packets_session_id", table_name="packets")
    op.drop_table("packets")
