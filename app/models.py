from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import DateTime, Enum, Integer, String, Text
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
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now, nullable=False
    )
