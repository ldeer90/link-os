"""Add canonical segmented campaign launches.

Revision ID: 20260723_0007
Revises: 20260722_0006
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_0007"
down_revision = "20260722_0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "campaign_launches",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("deterministic_key", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=9), nullable=False),
        sa.Column("contact_policy", sa.String(length=80), nullable=False),
        sa.Column("config_version", sa.String(length=80), nullable=False),
        sa.Column("safety_policy_version", sa.String(length=80), nullable=False),
        sa.Column("batch_size", sa.Integer(), nullable=False),
        sa.Column("daily_new_leads_per_sender", sa.Integer(), nullable=False),
        sa.Column("hard_bounce_limit", sa.Integer(), nullable=False),
        sa.Column("settings_snapshot", sa.JSON(), nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_campaign_launches")),
        sa.UniqueConstraint("deterministic_key", name=op.f("uq_campaign_launches_deterministic_key")),
    )
    op.create_index(op.f("ix_campaign_launches_created_at"), "campaign_launches", ["created_at"])
    op.create_index(op.f("ix_campaign_launches_deterministic_key"), "campaign_launches", ["deterministic_key"])
    op.create_index(op.f("ix_campaign_launches_status"), "campaign_launches", ["status"])
    op.add_column("campaign_batches", sa.Column("launch_id", sa.String(length=36), nullable=True))
    op.add_column("campaign_batches", sa.Column("segment", sa.String(length=40), nullable=True))
    op.add_column("campaign_batches", sa.Column("hard_bounce_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("campaign_batches", sa.Column("unsubscribe_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index(op.f("ix_campaign_batches_launch_id"), "campaign_batches", ["launch_id"])
    op.create_index(op.f("ix_campaign_batches_segment"), "campaign_batches", ["segment"])
    op.create_foreign_key(op.f("fk_campaign_batches_launch_id_campaign_launches"), "campaign_batches", "campaign_launches", ["launch_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint(op.f("fk_campaign_batches_launch_id_campaign_launches"), "campaign_batches", type_="foreignkey")
    op.drop_index(op.f("ix_campaign_batches_segment"), table_name="campaign_batches")
    op.drop_index(op.f("ix_campaign_batches_launch_id"), table_name="campaign_batches")
    op.drop_column("campaign_batches", "unsubscribe_count")
    op.drop_column("campaign_batches", "hard_bounce_count")
    op.drop_column("campaign_batches", "segment")
    op.drop_column("campaign_batches", "launch_id")
    op.drop_index(op.f("ix_campaign_launches_status"), table_name="campaign_launches")
    op.drop_index(op.f("ix_campaign_launches_deterministic_key"), table_name="campaign_launches")
    op.drop_index(op.f("ix_campaign_launches_created_at"), table_name="campaign_launches")
    op.drop_table("campaign_launches")
