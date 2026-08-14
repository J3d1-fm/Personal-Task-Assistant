from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(StrEnum):
    backlog = "backlog"
    in_progress = "in_progress"
    waiting_review = "waiting_review"
    blocked = "blocked"
    done = "done"
    cancelled = "cancelled"


class TaskAssignee(StrEnum):
    me = "me"
    codex = "codex"
    unassigned = "unassigned"


class TaskOrigin(StrEnum):
    manual = "manual"
    slack = "slack"
    telegram = "telegram"
    email = "email"
    codex = "codex"
    other = "other"


class SourceHealthStatus(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"
    reauth_required = "reauth_required"
    misconfigured = "misconfigured"
    rate_limited = "rate_limited"
    failed = "failed"


class IngestionDecisionType(StrEnum):
    created = "created"
    duplicate = "duplicate"
    ignored = "ignored"
    needs_review = "needs_review"
    failed = "failed"


class IngestionDecisionActor(StrEnum):
    adapter = "adapter"
    agent = "agent"
    human = "human"
    system = "system"


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    title: Mapped[str] = mapped_column(String(240), nullable=False, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[TaskStatus] = mapped_column(
        Enum(TaskStatus), default=TaskStatus.backlog, nullable=False, index=True
    )
    assignee: Mapped[TaskAssignee] = mapped_column(
        Enum(TaskAssignee), default=TaskAssignee.unassigned, nullable=False, index=True
    )
    origin: Mapped[TaskOrigin] = mapped_column(
        Enum(TaskOrigin), default=TaskOrigin.manual, nullable=False, index=True
    )
    priority: Mapped[int] = mapped_column(Integer, default=3, nullable=False, index=True)
    source_name: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_context: Mapped[str | None] = mapped_column(Text, nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(200), nullable=True, unique=True, index=True)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    reminder_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    reminder_last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_shown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class SourceState(Base):
    __tablename__ = "source_states"

    source_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    source_name: Mapped[str] = mapped_column(String(160), nullable=False)
    adapter_type: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[SourceHealthStatus] = mapped_column(
        Enum(SourceHealthStatus), default=SourceHealthStatus.healthy, nullable=False, index=True
    )
    last_checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    action_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_checked: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    candidates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicates: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ignored: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suppressed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )


class IngestionDecision(Base):
    __tablename__ = "ingestion_decisions"
    __table_args__ = (
        UniqueConstraint(
            "source_id",
            "source_item_id",
            "content_fingerprint",
            name="uq_ingestion_decision_item_fingerprint",
        ),
        Index("ix_ingestion_decisions_source_item", "source_id", "source_item_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    source_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    source_item_id: Mapped[str] = mapped_column(String(300), nullable=False)
    content_fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    decision: Mapped[IngestionDecisionType] = mapped_column(Enum(IngestionDecisionType), nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_by: Mapped[IngestionDecisionActor] = mapped_column(Enum(IngestionDecisionActor), nullable=False)
    revisit_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
