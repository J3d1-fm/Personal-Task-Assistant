"""Tests for the daily work loop's safety rails.

The agent loop itself needs an API key and is verified manually (see
automation/README.md), but the guardrails in _dispatch — the claim budget and
the refusal to close tasks — are enforced in code here, not in the prompt, so
they are unit-tested without any LLM.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automation"))

import work_loop  # noqa: E402


class _FakeContent:
    def __init__(self, text):
        self.text = text


class _FakeConn:
    """Records tool calls and returns a canned payload."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return [_FakeContent(self.payload)]


def _run(coro):
    return asyncio.run(coro)


def test_claim_budget_blocks_without_calling_the_tool():
    conn = _FakeConn("{}")
    text, claimed = _run(work_loop._dispatch(conn, "task_assistant_claim_task", {}, claims=3, max_tasks=3))
    assert "budget reached" in text.lower()
    assert claimed is None
    assert conn.calls == []  # the tracker was never hit


def test_claim_under_budget_parses_the_task():
    task = {"id": 7, "title": "do it", "status": "in_progress"}
    conn = _FakeConn(json.dumps(task))
    text, claimed = _run(work_loop._dispatch(conn, "task_assistant_claim_task", {}, claims=0, max_tasks=3))
    assert claimed == task
    assert conn.calls[0][0] == "task_assistant_claim_task"


def test_update_to_done_is_refused_without_calling_the_tool():
    conn = _FakeConn("{}")
    text, claimed = _run(
        work_loop._dispatch(conn, "task_assistant_update_task", {"task_id": 1, "status": "done"}, 0, 3)
    )
    assert "only the human" in text.lower()
    assert conn.calls == []


def test_update_to_blocked_is_allowed():
    conn = _FakeConn('{"id": 1, "status": "blocked"}')
    text, claimed = _run(
        work_loop._dispatch(conn, "task_assistant_update_task", {"task_id": 1, "status": "blocked"}, 0, 3)
    )
    assert conn.calls[0][0] == "task_assistant_update_task"


def test_run_work_loop_skips_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert work_loop.run_work_loop("http://x", "k") == []
