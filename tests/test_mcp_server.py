import asyncio
import json
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

import task_assistant_mcp as mcp_mod  # noqa: E402

from app.main import app as fastapi_app  # noqa: E402

ORIGINAL_CLIENT_FACTORY = mcp_mod._client

EXPECTED_TOOLS = {
    "task_assistant_get_queue",
    "task_assistant_queue_summary",
    "task_assistant_claim_task",
    "task_assistant_finish_task",
    "task_assistant_create_task",
    "task_assistant_update_task",
    "task_assistant_list_tasks",
    "task_assistant_ingest_context",
    "task_assistant_due_reminders",
}


@pytest.fixture(autouse=True)
def wire_mcp_to_app(monkeypatch, client):
    def make_client():
        return AsyncClient(
            transport=ASGITransport(app=fastapi_app),
            base_url="http://testserver",
            headers={"Authorization": "Bearer test-api-key"},
        )

    monkeypatch.setattr(mcp_mod, "_client", make_client)


def run(coro):
    return asyncio.run(coro)


def test_all_tools_registered():
    tools = run(mcp_mod.mcp.list_tools())
    assert {tool.name for tool in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.description, f"{tool.name} has no description"


def test_create_and_list_tasks():
    created = json.loads(
        run(
            mcp_mod.task_assistant_create_task(
                mcp_mod.CreateTaskInput(title="Ship MCP server", assignee="codex", priority=2)
            )
        )
    )
    assert created["id"] > 0
    assert created["origin"] == "codex"
    assert created["due_at"] is not None

    listing = run(mcp_mod.task_assistant_list_tasks(mcp_mod.ListTasksInput()))
    assert "Ship MCP server" in listing
    assert f"#{created['id']}" in listing

    as_json = run(mcp_mod.task_assistant_list_tasks(mcp_mod.ListTasksInput(response_format="json")))
    assert json.loads(as_json)[0]["title"] == "Ship MCP server"


def test_claim_and_finish_flow():
    run(mcp_mod.task_assistant_create_task(mcp_mod.CreateTaskInput(title="agent job", assignee="codex", priority=1)))

    claimed = json.loads(run(mcp_mod.task_assistant_claim_task(mcp_mod.EmptyInput())))
    assert claimed["title"] == "agent job"
    assert claimed["status"] == "in_progress"

    empty = run(mcp_mod.task_assistant_claim_task(mcp_mod.EmptyInput()))
    assert empty.startswith("No claimable task")

    finished = json.loads(run(mcp_mod.task_assistant_finish_task(mcp_mod.TaskIdInput(task_id=claimed["id"]))))
    assert finished["status"] == "waiting_review"


def test_queue_and_summary():
    run(
        mcp_mod.task_assistant_create_task(
            mcp_mod.CreateTaskInput(title="overdue thing", assignee="codex", due_at="2020-01-01T00:00:00Z")
        )
    )
    queue = run(mcp_mod.task_assistant_get_queue(mcp_mod.GetQueueInput()))
    assert "overdue: 1" in queue
    assert "overdue thing" in queue

    summary = run(mcp_mod.task_assistant_queue_summary(mcp_mod.EmptyInput()))
    assert "codex-ready: 1" in summary


def test_update_task_and_validation():
    created = json.loads(run(mcp_mod.task_assistant_create_task(mcp_mod.CreateTaskInput(title="tweak me"))))

    updated = json.loads(
        run(
            mcp_mod.task_assistant_update_task(
                mcp_mod.UpdateTaskInput(task_id=created["id"], priority=1, assignee="me")
            )
        )
    )
    assert updated["priority"] == 1
    assert updated["assignee"] == "me"

    no_fields = run(mcp_mod.task_assistant_update_task(mcp_mod.UpdateTaskInput(task_id=created["id"])))
    assert no_fields.startswith("Error: No fields to update")

    missing = run(mcp_mod.task_assistant_update_task(mcp_mod.UpdateTaskInput(task_id=99999, priority=1)))
    assert missing.startswith("Error: Not found")

    with pytest.raises(ValueError):
        mcp_mod.CreateTaskInput(title="bad due", due_at="tomorrow")


def test_ingest_context_dedupes():
    payload = mcp_mod.IngestContextInput(
        origin="telegram",
        source_name="Telegram Ops",
        tasks=[
            mcp_mod.IngestTaskItem(title="from chat", assignee="codex", external_id="telegram:1:7:0"),
            mcp_mod.IngestTaskItem(title="loose note"),
        ],
    )
    first = run(mcp_mod.task_assistant_ingest_context(payload))
    assert "Created 2 task(s), 0 duplicate(s) skipped." in first

    second = run(mcp_mod.task_assistant_ingest_context(payload))
    assert "Created 1 task(s), 1 duplicate(s) skipped." in second
    assert "from chat" in second


def test_duplicate_external_id_is_actionable():
    run(mcp_mod.task_assistant_create_task(mcp_mod.CreateTaskInput(title="one", external_id="jira:X-1")))
    conflict = run(mcp_mod.task_assistant_create_task(mcp_mod.CreateTaskInput(title="two", external_id="jira:X-1")))
    assert conflict.startswith("Error: Conflict")


def test_due_reminders():
    assert run(mcp_mod.task_assistant_due_reminders(mcp_mod.EmptyInput())) == "No tasks need a reminder."
    run(mcp_mod.task_assistant_create_task(mcp_mod.CreateTaskInput(title="late", due_at="2020-01-01T00:00:00Z")))
    reminders = run(mcp_mod.task_assistant_due_reminders(mcp_mod.EmptyInput()))
    assert "late" in reminders


def test_missing_api_key_is_actionable(monkeypatch):
    monkeypatch.setattr(mcp_mod, "_client", ORIGINAL_CLIENT_FACTORY)
    monkeypatch.delenv("TASK_TRACKER_API_KEY", raising=False)
    result = run(mcp_mod.task_assistant_queue_summary(mcp_mod.EmptyInput()))
    assert "TASK_TRACKER_API_KEY is not configured" in result
