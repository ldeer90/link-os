"""Create the canonical LINK OS platform schema.

Revision ID: 20260715_0001
Revises:
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from deal_tracker.platform.models import Base


revision = "20260715_0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind, checkfirst=False)
    json_value = "{\"paused\":true,\"reason\":\"initial fail-closed default\"}"
    value_expression = (
        "CAST(:json_value AS JSON)"
        if bind.dialect.name == "postgresql"
        else ":json_value"
    )
    op.execute(
        sa.text(
            "INSERT INTO system_settings "
            "(key, value, version, updated_at, updated_by) VALUES "
            f"('outreach.global_pause', {value_expression}, 1, CURRENT_TIMESTAMP, 'migration')"
        ).bindparams(sa.bindparam("json_value", value=json_value, type_=sa.String()))
    )


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind(), checkfirst=False)
