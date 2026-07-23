"""Add canonical SE Ranking backlink discovery records.

Revision ID: 20260720_0002
Revises: 20260715_0001
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op

from deal_tracker.platform.models import (
    BacklinkAnalysisRun,
    BacklinkAnalysisTarget,
    BacklinkCandidate,
    BacklinkCreditUsage,
    BacklinkEvidence,
    BacklinkProviderRequest,
    BacklinkReviewDecision,
)


revision = "20260720_0002"
down_revision = "20260715_0001"
branch_labels = None
depends_on = None


TABLES = [
    BacklinkAnalysisRun.__table__,
    BacklinkAnalysisTarget.__table__,
    BacklinkProviderRequest.__table__,
    BacklinkCreditUsage.__table__,
    BacklinkEvidence.__table__,
    BacklinkCandidate.__table__,
    BacklinkReviewDecision.__table__,
]


def upgrade() -> None:
    bind = op.get_bind()
    for table in TABLES:
        table.create(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    for table in reversed(TABLES):
        table.drop(bind=bind, checkfirst=True)
