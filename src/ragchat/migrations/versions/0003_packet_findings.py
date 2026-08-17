"""packet findings: persist audit results + reviewer decisions

Revision ID: 0003_packet_findings
Revises: 0002_packet_auditor
Create Date: 2026-08-16

Adds ``packet_findings`` so a Gap Report is persisted (not just computed and returned),
which is what lets a packet be re-opened from history and reviewed. The machine verdict
columns are written once at audit time; the nullable ``review_*`` columns hold a reviewer's
accept/override decision. Cascades from ``packets`` (and thus from ``sessions``), so the
existing TTL purge cleans them up unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_packet_findings"
down_revision: str | None = "0002_packet_auditor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "packet_findings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("packet_id", sa.String(length=64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("requirement_id", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("sources", sa.JSON(), nullable=True),
        sa.Column("review_action", sa.String(length=16), nullable=True),
        sa.Column("review_status", sa.String(length=16), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["packet_id"], ["packets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_packet_findings_packet_id", "packet_findings", ["packet_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_packet_findings_packet_id", table_name="packet_findings")
    op.drop_table("packet_findings")
