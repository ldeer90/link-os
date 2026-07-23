"""Add referring-domain totals and coverage inputs.

Revision ID: 20260720_0003
Revises: 20260720_0002
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260720_0003"
down_revision = "20260720_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backlink_analysis_targets",
        sa.Column("referring_domain_count", sa.Integer(), nullable=True),
    )
    op.add_column(
        "backlink_analysis_targets",
        sa.Column(
            "count_actual_credits",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "backlink_analysis_targets",
        sa.Column("count_checked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backlink_analysis_targets", "count_checked_at")
    op.drop_column("backlink_analysis_targets", "count_actual_credits")
    op.drop_column("backlink_analysis_targets", "referring_domain_count")
