"""Add commercial anchor intent to backlink candidates.

Revision ID: 20260722_0006
Revises: 20260720_0005
Create Date: 2026-07-22
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260722_0006"
down_revision = "20260720_0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backlink_candidates",
        sa.Column("commercial_anchor_class", sa.String(length=32), nullable=False, server_default="unknown"),
    )
    op.add_column(
        "backlink_candidates",
        sa.Column("commercial_anchor_score", sa.Float(), nullable=False, server_default="0"),
    )
    op.add_column(
        "backlink_candidates",
        sa.Column("commercial_anchor_signals", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.create_index(
        "ix_backlink_candidates_anchor_class",
        "backlink_candidates",
        ["commercial_anchor_class"],
    )


def downgrade() -> None:
    op.drop_index("ix_backlink_candidates_anchor_class", table_name="backlink_candidates")
    op.drop_column("backlink_candidates", "commercial_anchor_signals")
    op.drop_column("backlink_candidates", "commercial_anchor_score")
    op.drop_column("backlink_candidates", "commercial_anchor_class")
