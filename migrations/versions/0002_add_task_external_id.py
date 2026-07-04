# Add tasks.external_id for idempotent ingest deduplication.
#
# Revision: 0002
# Revises: 0001
# Created: 2026-07-04

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("external_id", sa.String(length=200), nullable=True))
    op.create_index(op.f("ix_tasks_external_id"), "tasks", ["external_id"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_tasks_external_id"), table_name="tasks")
    op.drop_column("tasks", "external_id")
