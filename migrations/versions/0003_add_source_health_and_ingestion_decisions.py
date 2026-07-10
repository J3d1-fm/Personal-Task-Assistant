# Add durable source health and ingestion decision receipts.
#
# Revision: 0003
# Revises: 0002
# Created: 2026-07-10

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_HEALTH_STATUS = sa.Enum(
    "healthy",
    "degraded",
    "unavailable",
    "reauth_required",
    "misconfigured",
    "rate_limited",
    "failed",
    name="sourcehealthstatus",
)
INGESTION_DECISION_TYPE = sa.Enum(
    "created",
    "duplicate",
    "ignored",
    "needs_review",
    "failed",
    name="ingestiondecisiontype",
)
INGESTION_DECISION_ACTOR = sa.Enum(
    "adapter",
    "agent",
    "human",
    "system",
    name="ingestiondecisionactor",
)


def upgrade() -> None:
    op.create_table(
        "source_states",
        sa.Column("source_id", sa.String(length=200), primary_key=True),
        sa.Column("source_name", sa.String(length=160), nullable=False),
        sa.Column("adapter_type", sa.String(length=120), nullable=False),
        sa.Column("status", SOURCE_HEALTH_STATUS, nullable=False),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=120), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("action_url", sa.Text(), nullable=True),
        sa.Column("items_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candidates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicates", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("ignored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("suppressed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_source_states_status"), "source_states", ["status"])
    op.create_index(op.f("ix_source_states_last_checked_at"), "source_states", ["last_checked_at"])

    op.create_table(
        "ingestion_decisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(length=200), nullable=False),
        sa.Column("source_item_id", sa.String(length=300), nullable=False),
        sa.Column("content_fingerprint", sa.String(length=200), nullable=False),
        sa.Column("decision", INGESTION_DECISION_TYPE, nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column("decided_by", INGESTION_DECISION_ACTOR, nullable=False),
        sa.Column("revisit_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "source_id",
            "source_item_id",
            "content_fingerprint",
            name="uq_ingestion_decision_item_fingerprint",
        ),
    )
    op.create_index(op.f("ix_ingestion_decisions_id"), "ingestion_decisions", ["id"])
    op.create_index(op.f("ix_ingestion_decisions_source_id"), "ingestion_decisions", ["source_id"])
    op.create_index(op.f("ix_ingestion_decisions_decision"), "ingestion_decisions", ["decision"])
    op.create_index(
        "ix_ingestion_decisions_source_item",
        "ingestion_decisions",
        ["source_id", "source_item_id"],
    )


def downgrade() -> None:
    op.drop_table("ingestion_decisions")
    op.drop_table("source_states")
