# Initial tasks table matching the 0.3.x schema.
#
# Revision: 0001
# Revises:
# Created: 2026-07-04

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATUS = sa.Enum(
    "backlog",
    "in_progress",
    "waiting_review",
    "blocked",
    "done",
    "cancelled",
    name="taskstatus",
)
TASK_ASSIGNEE = sa.Enum("me", "codex", "unassigned", name="taskassignee")
TASK_ORIGIN = sa.Enum("manual", "slack", "telegram", "email", "codex", "other", name="taskorigin")


def upgrade() -> None:
    op.create_table(
        "tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(length=240), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", TASK_STATUS, nullable=False),
        sa.Column("assignee", TASK_ASSIGNEE, nullable=False),
        sa.Column("origin", TASK_ORIGIN, nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("source_name", sa.String(length=160), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_context", sa.Text(), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reminder_last_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("ix_tasks_id"), "tasks", ["id"])
    op.create_index(op.f("ix_tasks_title"), "tasks", ["title"])
    op.create_index(op.f("ix_tasks_status"), "tasks", ["status"])
    op.create_index(op.f("ix_tasks_assignee"), "tasks", ["assignee"])
    op.create_index(op.f("ix_tasks_origin"), "tasks", ["origin"])
    op.create_index(op.f("ix_tasks_priority"), "tasks", ["priority"])
    op.create_index(op.f("ix_tasks_due_at"), "tasks", ["due_at"])
    op.create_index(op.f("ix_tasks_reminder_at"), "tasks", ["reminder_at"])


def downgrade() -> None:
    op.drop_table("tasks")
