"""Add maximum Domain InLink Rank to backlink analyses.

Revision ID: 20260720_0005
Revises: 20260720_0004
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260720_0005"
down_revision = "20260720_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("backlink_analysis_runs", sa.Column("authority_ceiling", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("backlink_analysis_runs", "authority_ceiling")
