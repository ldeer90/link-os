"""Add canonical referring URL filter to backlink analysis runs.

Revision ID: 20260720_0004
Revises: 20260720_0003
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260720_0004"
down_revision = "20260720_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "backlink_analysis_runs",
        sa.Column("source_url_filter", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("backlink_analysis_runs", "source_url_filter")
