from datetime import datetime

from pydantic import BaseModel, Field

from app.models import TaskAssignee, TaskOrigin, TaskStatus


class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    description: str | None = None
    status: TaskStatus = TaskStatus.backlog
    assignee: TaskAssignee = TaskAssignee.unassigned
    origin: TaskOrigin = TaskOrigin.manual
    priority: int = Field(default=3, ge=1, le=5)
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    source_context: str | None = None
    due_at: datetime | None = None
    reminder_at: datetime | None = None


class TaskCreate(TaskBase):
    pass


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    description: str | None = None
    status: TaskStatus | None = None
    assignee: TaskAssignee | None = None
    origin: TaskOrigin | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    source_name: str | None = Field(default=None, max_length=160)
    source_url: str | None = None
    source_context: str | None = None
    due_at: datetime | None = None
    reminder_at: datetime | None = None
    reminder_last_sent_at: datetime | None = None


class TaskRead(TaskBase):
    id: int
    reminder_last_sent_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ContextIngest(BaseModel):
    origin: TaskOrigin
    source_name: str | None = None
    source_url: str | None = None
    source_context: str | None = None
    tasks: list[TaskCreate]


class ContextIngestResult(BaseModel):
    created: list[TaskRead]


class AgentQueueSummary(BaseModel):
    active: int
    overdue: int
    due_soon: int
    codex_ready: int
    human_input: int
    review: int
    blocked: int
    unassigned: int


class AgentQueueRead(BaseModel):
    summary: AgentQueueSummary
    tasks: list[TaskRead]
