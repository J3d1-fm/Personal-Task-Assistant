from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from app.models import (
    IngestionDecisionActor,
    IngestionDecisionType,
    SourceHealthStatus,
    TaskAssignee,
    TaskOrigin,
    TaskStatus,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


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
    external_id: str | None = Field(default=None, min_length=1, max_length=200)
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
    external_id: str | None = Field(default=None, min_length=1, max_length=200)
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
    duplicates: list[TaskRead] = Field(default_factory=list)


class SourceReport(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    source_name: str = Field(min_length=1, max_length=160)
    adapter_type: str = Field(min_length=1, max_length=120)
    status: SourceHealthStatus
    checked_at: datetime | None = None
    error_code: str | None = Field(default=None, max_length=120)
    error_message: str | None = Field(default=None, max_length=2000)
    action_url: str | None = Field(default=None, max_length=2048)
    items_checked: int = Field(default=0, ge=0)
    candidates: int = Field(default=0, ge=0)
    created: int = Field(default=0, ge=0)
    duplicates: int = Field(default=0, ge=0)
    ignored: int = Field(default=0, ge=0)
    suppressed: int = Field(default=0, ge=0)

    @field_validator("checked_at")
    @classmethod
    def checked_at_must_be_utc(cls, value: datetime | None) -> datetime | None:
        normalized = _as_utc(value)
        if normalized is not None and normalized > datetime.now(timezone.utc) + timedelta(minutes=5):
            raise ValueError("checked_at cannot be more than five minutes in the future")
        return normalized

    @field_validator("action_url")
    @classmethod
    def action_url_must_be_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("action_url must be an absolute http(s) URL without embedded credentials")
        return value


class SourceStateRead(BaseModel):
    source_id: str
    source_name: str
    adapter_type: str
    status: SourceHealthStatus
    last_checked_at: datetime
    last_success_at: datetime | None = None
    last_error_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    action_url: str | None = None
    items_checked: int
    candidates: int
    created: int
    duplicates: int
    ignored: int
    suppressed: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("last_checked_at", "last_success_at", "last_error_at", "created_at", "updated_at")
    @classmethod
    def timestamps_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class IngestionDecisionWrite(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    source_item_id: str = Field(min_length=1, max_length=300)
    content_fingerprint: str = Field(min_length=1, max_length=200)
    decision: IngestionDecisionType
    reason: str | None = Field(default=None, max_length=2000)
    task_id: int | None = Field(default=None, ge=1)
    decided_by: IngestionDecisionActor = IngestionDecisionActor.adapter
    revisit_after: datetime | None = None

    @field_validator("revisit_after")
    @classmethod
    def revisit_after_must_be_utc(cls, value: datetime | None) -> datetime | None:
        return _as_utc(value)


class IngestionDecisionRead(IngestionDecisionWrite):
    decided_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}

    @field_validator("decided_at", "updated_at")
    @classmethod
    def decision_timestamps_must_be_utc(cls, value: datetime) -> datetime:
        normalized = _as_utc(value)
        if normalized is None:
            raise ValueError("decision timestamp is required")
        return normalized


class IngestionCheckItem(BaseModel):
    source_item_id: str = Field(min_length=1, max_length=300)
    content_fingerprint: str = Field(min_length=1, max_length=200)


class IngestionCheckRequest(BaseModel):
    source_id: str = Field(min_length=1, max_length=200)
    items: list[IngestionCheckItem] = Field(min_length=1, max_length=500)


class IngestionCheckResultItem(IngestionCheckItem):
    should_process: bool
    existing_decision: IngestionDecisionRead | None = None


class IngestionCheckResult(BaseModel):
    source_id: str
    items: list[IngestionCheckResultItem]


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
