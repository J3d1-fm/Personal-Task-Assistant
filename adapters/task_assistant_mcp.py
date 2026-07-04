#!/usr/bin/env python3
"""MCP server for Personal Task Assistant.

Exposes the JSON API as MCP tools over stdio, so MCP clients (Claude Code,
Claude Desktop, or any other) can read the agent queue, claim work, create and
update tasks, and ingest context without a custom adapter.

Configuration (environment variables):
- TASK_TRACKER_URL: base URL of a running instance (default http://127.0.0.1:8000)
- TASK_TRACKER_API_KEY: the instance API key (required)

Run directly or register with an MCP client:
    python3 adapters/task_assistant_mcp.py
    claude mcp add task-assistant -e TASK_TRACKER_API_KEY=<key> -- \
        python3 adapters/task_assistant_mcp.py

See docs/MCP.md for setup details.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from enum import Enum

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, field_validator

mcp = FastMCP("task_assistant_mcp")

DEFAULT_TRACKER_URL = "http://127.0.0.1:8000"
REQUEST_TIMEOUT = 30.0


class TaskStatusValue(str, Enum):
    backlog = "backlog"
    in_progress = "in_progress"
    waiting_review = "waiting_review"
    blocked = "blocked"
    done = "done"
    cancelled = "cancelled"


class TaskAssigneeValue(str, Enum):
    me = "me"
    codex = "codex"
    unassigned = "unassigned"


class TaskOriginValue(str, Enum):
    manual = "manual"
    slack = "slack"
    telegram = "telegram"
    email = "email"
    codex = "codex"
    other = "other"


class QueueSort(str, Enum):
    smart = "smart"
    due = "due"
    created = "created"
    updated = "updated"
    priority = "priority"
    owner = "owner"


class ResponseFormat(str, Enum):
    markdown = "markdown"
    json = "json"


def _validate_iso_datetime(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Not an ISO 8601 datetime: {value!r} (expected e.g. 2026-07-10T18:00:00Z)") from exc
    return value


class GetQueueInput(BaseModel):
    """Input for reading the agent queue."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: TaskStatusValue | None = Field(default=None, description="Filter by task status")
    assignee: TaskAssigneeValue | None = Field(default=None, description="Filter by owner: me, codex, unassigned")
    sort: QueueSort = Field(default=QueueSort.smart, description="Sort order; 'smart' ranks urgent work first")
    limit: int = Field(default=20, description="Maximum tasks to return", ge=1, le=250)
    include_done: bool = Field(default=False, description="Include done and cancelled tasks")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.markdown,
        description="'markdown' for human-readable output, 'json' for full structured data",
    )


class ListTasksInput(BaseModel):
    """Input for listing tasks with pagination."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    status: TaskStatusValue | None = Field(default=None, description="Filter by task status")
    assignee: TaskAssigneeValue | None = Field(default=None, description="Filter by owner")
    include_done: bool = Field(default=False, description="Include done and cancelled tasks")
    limit: int = Field(default=20, description="Maximum tasks to return", ge=1, le=500)
    offset: int = Field(default=0, description="Number of tasks to skip for pagination", ge=0)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.markdown,
        description="'markdown' for human-readable output, 'json' for full structured data",
    )


class CreateTaskInput(BaseModel):
    """Input for creating a single task."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Short actionable task title", min_length=1, max_length=240)
    description: str | None = Field(default=None, description="Longer context for the task")
    status: TaskStatusValue = Field(default=TaskStatusValue.backlog, description="Initial status")
    assignee: TaskAssigneeValue = Field(
        default=TaskAssigneeValue.unassigned,
        description="Owner: 'codex' for agent-executable work, 'me' for human work",
    )
    origin: TaskOriginValue = Field(default=TaskOriginValue.codex, description="Where the task came from")
    priority: int = Field(default=3, description="1 (urgent) to 5 (someday)", ge=1, le=5)
    source_name: str | None = Field(default=None, description="Source label, e.g. 'Slack #ops'", max_length=160)
    source_url: str | None = Field(default=None, description="Link back to the source")
    source_context: str | None = Field(default=None, description="Raw source text the task was extracted from")
    external_id: str | None = Field(
        default=None,
        description="Stable source id for deduplication, e.g. 'jira:PROJ-42'; duplicate ids are rejected",
        min_length=1,
        max_length=200,
    )
    due_at: str | None = Field(default=None, description="Deadline as ISO 8601, e.g. 2026-07-10T18:00:00Z")
    reminder_at: str | None = Field(default=None, description="Reminder time as ISO 8601")

    @field_validator("due_at", "reminder_at")
    @classmethod
    def _check_datetimes(cls, value: str | None) -> str | None:
        return _validate_iso_datetime(value)


class UpdateTaskInput(BaseModel):
    """Input for partially updating a task; only provided fields change."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    task_id: int = Field(..., description="Numeric task id, e.g. 42", ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240, description="New title")
    description: str | None = Field(default=None, description="New description")
    status: TaskStatusValue | None = Field(default=None, description="New status")
    assignee: TaskAssigneeValue | None = Field(default=None, description="New owner")
    priority: int | None = Field(default=None, ge=1, le=5, description="New priority, 1 (urgent) to 5")
    due_at: str | None = Field(default=None, description="New deadline as ISO 8601")
    reminder_at: str | None = Field(default=None, description="New reminder time as ISO 8601")

    @field_validator("due_at", "reminder_at")
    @classmethod
    def _check_datetimes(cls, value: str | None) -> str | None:
        return _validate_iso_datetime(value)


class TaskIdInput(BaseModel):
    """Input identifying one task."""

    model_config = ConfigDict(extra="forbid")

    task_id: int = Field(..., description="Numeric task id, e.g. 42", ge=1)


class IngestTaskItem(BaseModel):
    """One task extracted from source context."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    title: str = Field(..., description="Short actionable task title", min_length=1, max_length=240)
    description: str | None = Field(default=None, description="Longer context for the task")
    assignee: TaskAssigneeValue = Field(default=TaskAssigneeValue.unassigned, description="Owner")
    status: TaskStatusValue = Field(default=TaskStatusValue.backlog, description="Initial status")
    priority: int = Field(default=3, description="1 (urgent) to 5 (someday)", ge=1, le=5)
    external_id: str | None = Field(
        default=None, description="Stable source id for deduplication", min_length=1, max_length=200
    )
    due_at: str | None = Field(default=None, description="Deadline as ISO 8601")
    reminder_at: str | None = Field(default=None, description="Reminder time as ISO 8601")

    @field_validator("due_at", "reminder_at")
    @classmethod
    def _check_datetimes(cls, value: str | None) -> str | None:
        return _validate_iso_datetime(value)


class IngestContextInput(BaseModel):
    """Input for turning source context into a batch of tasks."""

    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    origin: TaskOriginValue = Field(..., description="Source system: telegram, slack, email, codex, manual, other")
    source_name: str | None = Field(default=None, description="Source label, e.g. 'Gmail thread from Alice'")
    source_url: str | None = Field(default=None, description="Link back to the source")
    source_context: str | None = Field(default=None, description="Raw text the tasks were extracted from")
    tasks: list[IngestTaskItem] = Field(..., description="Normalized tasks extracted from the context", min_length=1)


class EmptyInput(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid")


def _client() -> httpx.AsyncClient:
    api_key = os.getenv("TASK_TRACKER_API_KEY", "").strip()
    if not api_key or api_key == "change-me":
        raise RuntimeError(
            "TASK_TRACKER_API_KEY is not configured. Set it to the API key of the "
            "Personal Task Assistant instance (see its .env) and restart the MCP server."
        )
    base_url = os.getenv("TASK_TRACKER_URL", DEFAULT_TRACKER_URL).rstrip("/")
    return httpx.AsyncClient(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=REQUEST_TIMEOUT,
    )


async def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict | list:
    async with _client() as client:
        response = await client.request(method, path, params=params, json=body)
        response.raise_for_status()
        return response.json()


def _error_text(exc: Exception) -> str:
    if isinstance(exc, RuntimeError):
        return f"Error: {exc}"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        try:
            detail = exc.response.json().get("detail", "")
        except ValueError:
            detail = exc.response.text[:200]
        if status_code == 401:
            return (
                "Error: The task tracker rejected the API key. Check that TASK_TRACKER_API_KEY "
                "matches the key configured in the instance's .env."
            )
        if status_code == 404:
            return f"Error: Not found. {detail or 'Check that the task id exists (use task_assistant_list_tasks).'}"
        if status_code == 409:
            return f"Error: Conflict. {detail or 'A task with this external_id already exists.'}"
        if status_code == 422:
            return f"Error: The task tracker rejected the request: {detail}"
        return f"Error: Task tracker request failed with HTTP {status_code}. {detail}"
    if isinstance(exc, httpx.ConnectError):
        url = os.getenv("TASK_TRACKER_URL", DEFAULT_TRACKER_URL)
        return (
            f"Error: Cannot reach the task tracker at {url}. Start it first "
            "(run-local.command or uvicorn app.main:app) or fix TASK_TRACKER_URL."
        )
    if isinstance(exc, httpx.TimeoutException):
        return "Error: The task tracker did not respond in time. Try again."
    return f"Error: {type(exc).__name__}: {exc}"


def _task_json(task: dict) -> str:
    return json.dumps(task, indent=2, ensure_ascii=False)


def _task_line(task: dict) -> str:
    due = task.get("due_at") or "no DD"
    parts = [
        f"- #{task['id']} [P{task['priority']}] {task['title']}",
        f"{task['assignee']}/{task['status']}",
        f"DD {due}",
    ]
    if task.get("source_name"):
        parts.append(f"from {task['source_name']}")
    return " — ".join(parts)


def _tasks_markdown(tasks: list[dict], heading: str) -> str:
    if not tasks:
        return f"{heading}\n\nNo tasks."
    lines = [heading, ""]
    lines.extend(_task_line(task) for task in tasks)
    return "\n".join(lines)


def _summary_markdown(summary: dict) -> str:
    return (
        f"Active: {summary['active']} | overdue: {summary['overdue']} | due soon: {summary['due_soon']} | "
        f"codex-ready: {summary['codex_ready']} | needs human: {summary['human_input']} | "
        f"review: {summary['review']} | blocked: {summary['blocked']} | unassigned: {summary['unassigned']}"
    )


@mcp.tool(
    name="task_assistant_get_queue",
    annotations={
        "title": "Get Agent Queue",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_get_queue(params: GetQueueInput) -> str:
    """Read the shared human/AI task queue with summary metrics.

    Returns queue metrics (active, overdue, codex-ready, needs-human, review,
    blocked, unassigned counts) plus the tasks selected by the filters, ranked
    by the requested sort. 'smart' sort puts overdue and urgent work first.
    Read-only: to take a task for execution use task_assistant_claim_task.

    Args:
        params (GetQueueInput):
            - status (optional): backlog | in_progress | waiting_review | blocked | done | cancelled
            - assignee (optional): me | codex | unassigned
            - sort: smart (default) | due | created | updated | priority | owner
            - limit: 1-250 (default 20)
            - include_done: include finished tasks (default false)
            - response_format: markdown (default) | json

    Returns:
        str: Markdown (summary line + one bullet per task) or JSON
        {"summary": {...}, "tasks": [...]} when response_format="json".
    """
    try:
        data = await _request(
            "GET",
            "/api/agent/queue",
            params={
                "sort": params.sort.value,
                "limit": params.limit,
                "include_done": params.include_done,
                **({"status": params.status.value} if params.status else {}),
                **({"assignee": params.assignee.value} if params.assignee else {}),
            },
        )
    except Exception as exc:
        return _error_text(exc)
    if params.response_format == ResponseFormat.json:
        return json.dumps(data, indent=2, ensure_ascii=False)
    return _summary_markdown(data["summary"]) + "\n\n" + _tasks_markdown(data["tasks"], "## Queue")


@mcp.tool(
    name="task_assistant_queue_summary",
    annotations={
        "title": "Get Queue Summary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_queue_summary(params: EmptyInput) -> str:
    """Get only the queue metrics, without the task list.

    The cheapest way to poll the tracker: one line with active, overdue,
    due-soon, codex-ready, needs-human, review, blocked, and unassigned counts.
    Use task_assistant_get_queue when you need the tasks themselves.

    Args:
        params (EmptyInput): No parameters.

    Returns:
        str: One markdown line with the eight counters.
    """
    try:
        summary = await _request("GET", "/api/agent/queue/summary")
    except Exception as exc:
        return _error_text(exc)
    return _summary_markdown(summary)


@mcp.tool(
    name="task_assistant_claim_task",
    annotations={
        "title": "Claim Next Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def task_assistant_claim_task(params: EmptyInput) -> str:
    """Atomically claim the next agent task and start working on it.

    Takes the top-ranked codex-owned backlog task, moves it to in_progress,
    and returns it. The transition is atomic on the server, so parallel agents
    calling this tool never receive the same task. When you finish the work,
    hand it back with task_assistant_finish_task.

    Args:
        params (EmptyInput): No parameters.

    Returns:
        str: JSON of the claimed task (id, title, description, priority,
        due_at, source fields), or "No claimable task..." when the codex
        backlog is empty — that is a normal outcome, not an error.
    """
    try:
        task = await _request("POST", "/api/agent/claim")
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            return (
                "No claimable task: the codex backlog is empty. "
                "Check task_assistant_get_queue for review or blocked work."
            )
        return _error_text(exc)
    except Exception as exc:
        return _error_text(exc)
    return _task_json(task)


@mcp.tool(
    name="task_assistant_finish_task",
    annotations={
        "title": "Finish Task (Send to Review)",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_finish_task(params: TaskIdInput) -> str:
    """Hand finished agent work back to the human by moving it to waiting_review.

    This is the normal end of the claim -> work -> review loop: the human
    stays in control and closes the task after checking the result. Use
    task_assistant_update_task with status="done" only when the human has
    explicitly said the task is complete.

    Args:
        params (TaskIdInput):
            - task_id (int): The task to finish, e.g. 42.

    Returns:
        str: JSON of the updated task with status "waiting_review".
    """
    try:
        task = await _request("PATCH", f"/api/tasks/{params.task_id}", body={"status": "waiting_review"})
    except Exception as exc:
        return _error_text(exc)
    return _task_json(task)


@mcp.tool(
    name="task_assistant_create_task",
    annotations={
        "title": "Create Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def task_assistant_create_task(params: CreateTaskInput) -> str:
    """Create one task in the tracker.

    When due_at is omitted the server estimates a deadline from priority
    (P1 ~1 day ... P5 ~30 days). Set assignee="codex" for agent-executable
    work and assignee="me" for work only the human can do. Pass external_id
    when the task mirrors an item in another system so retries cannot create
    duplicates. For batches extracted from one source text, prefer
    task_assistant_ingest_context.

    Args:
        params (CreateTaskInput): title (required), description, status,
            assignee, origin, priority 1-5, source_name/source_url/
            source_context, external_id, due_at/reminder_at (ISO 8601).

    Returns:
        str: JSON of the created task including its id, or an error message
        (409 conflict when the external_id already exists).
    """
    try:
        task = await _request("POST", "/api/agent/tasks", body=params.model_dump(exclude_none=True, mode="json"))
    except Exception as exc:
        return _error_text(exc)
    return _task_json(task)


@mcp.tool(
    name="task_assistant_update_task",
    annotations={
        "title": "Update Task",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_update_task(params: UpdateTaskInput) -> str:
    """Update fields of an existing task; only the provided fields change.

    Use for reprioritizing, reassigning, changing deadlines, or explicit
    status transitions (blocked, done, cancelled). Setting status="done"
    stamps completed_at; moving away from done clears it. For the routine
    "agent finished, human should check" transition prefer
    task_assistant_finish_task.

    Args:
        params (UpdateTaskInput):
            - task_id (int, required)
            - title, description, status, assignee, priority, due_at,
              reminder_at: any subset to change.

    Returns:
        str: JSON of the updated task, or an error message when the task id
        is unknown or no fields were provided.
    """
    updates = params.model_dump(exclude_none=True, mode="json")
    updates.pop("task_id", None)
    if not updates:
        return "Error: No fields to update. Provide at least one of title, description, status, assignee, priority, due_at, reminder_at."
    try:
        task = await _request("PATCH", f"/api/tasks/{params.task_id}", body=updates)
    except Exception as exc:
        return _error_text(exc)
    return _task_json(task)


@mcp.tool(
    name="task_assistant_list_tasks",
    annotations={
        "title": "List Tasks",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_list_tasks(params: ListTasksInput) -> str:
    """List tasks with filters and pagination, in board order.

    Ordered by priority, then nearest deadline. Done and cancelled tasks are
    hidden unless include_done=true. Use offset to page through large boards;
    if exactly `limit` tasks come back, request the next page to check for
    more. For urgency-ranked output with metrics use task_assistant_get_queue.

    Args:
        params (ListTasksInput):
            - status, assignee: optional filters
            - include_done: default false
            - limit: 1-500 (default 20), offset: default 0
            - response_format: markdown (default) | json

    Returns:
        str: Markdown bullets ("#id [P] title — owner/status — DD ...") with a
        pagination note, or a JSON array of full task objects.
    """
    try:
        tasks = await _request(
            "GET",
            "/api/tasks",
            params={
                "include_done": params.include_done,
                "limit": params.limit,
                "offset": params.offset,
                **({"status": params.status.value} if params.status else {}),
                **({"assignee": params.assignee.value} if params.assignee else {}),
            },
        )
    except Exception as exc:
        return _error_text(exc)
    if params.response_format == ResponseFormat.json:
        return json.dumps(tasks, indent=2, ensure_ascii=False)
    heading = f"## Tasks (showing {len(tasks)} from offset {params.offset})"
    body = _tasks_markdown(tasks, heading)
    if len(tasks) == params.limit:
        body += f"\n\nThere may be more: repeat with offset={params.offset + params.limit}."
    return body


@mcp.tool(
    name="task_assistant_ingest_context",
    annotations={
        "title": "Ingest Tasks From Context",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_ingest_context(params: IngestContextInput) -> str:
    """Turn source context (chat, email, meeting notes) into a batch of tasks.

    The intended flow: you read messy source text, extract concrete tasks with
    owners and priorities, and submit them together with the original context.
    Give each task an external_id derived from the source (e.g.
    "telegram:<chat>:<message>:<n>") to make the call idempotent: resubmitting
    the same batch reports duplicates instead of creating copies.

    Args:
        params (IngestContextInput):
            - origin (required): telegram | slack | email | codex | manual | other
            - source_name, source_url, source_context: shared source metadata
            - tasks: 1+ items (title required; assignee, status, priority,
              external_id, due_at, reminder_at optional)

    Returns:
        str: Markdown report: how many tasks were created (with ids) and how
        many were duplicates matched by external_id.
    """
    try:
        result = await _request(
            "POST", "/api/agent/ingest/context", body=params.model_dump(exclude_none=True, mode="json")
        )
    except Exception as exc:
        return _error_text(exc)
    created = result.get("created", [])
    duplicates = result.get("duplicates", [])
    lines = [f"Created {len(created)} task(s), {len(duplicates)} duplicate(s) skipped."]
    if created:
        lines.append("")
        lines.append("Created:")
        lines.extend(_task_line(task) for task in created)
    if duplicates:
        lines.append("")
        lines.append("Already existed (matched by external_id):")
        lines.extend(_task_line(task) for task in duplicates)
    return "\n".join(lines)


@mcp.tool(
    name="task_assistant_due_reminders",
    annotations={
        "title": "Get Due Reminders",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def task_assistant_due_reminders(params: EmptyInput) -> str:
    """List active tasks whose deadline or reminder time has passed.

    Use to nudge the human about missed work or to decide what to surface
    first. Tasks already reminded after their target time are excluded.

    Args:
        params (EmptyInput): No parameters.

    Returns:
        str: Markdown bullets of overdue tasks, or "No tasks need a reminder."
    """
    try:
        tasks = await _request("GET", "/api/reminders/due")
    except Exception as exc:
        return _error_text(exc)
    if not tasks:
        return "No tasks need a reminder."
    return _tasks_markdown(tasks, "## Due reminders")


if __name__ == "__main__":
    mcp.run()
