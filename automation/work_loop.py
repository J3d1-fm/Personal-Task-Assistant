#!/usr/bin/env python3
"""Agent work loop for the daily ritual (opt-in, requires ANTHROPIC_API_KEY).

Gives an Anthropic model ONLY the Personal Task Assistant MCP tools and the
task-board-worker instructions, then lets it work the codex queue:
claim -> do what it can safely do -> finish to waiting_review. Hard safety
rails, enforced here rather than trusted to the prompt:

- The model never gets an API key beyond the tracker's, and its only tools are
  the task_assistant_* MCP tools — it cannot touch the shell, the repo, or the
  network.
- It may advance tasks only to waiting_review (via finish_task). Any attempt to
  set status="done" through update_task is rejected — closing a task is the
  human's decision.
- It may claim at most `max_tasks` tasks per run; after that the claim tool
  refuses, so an unattended run cannot run away.

This module is imported by daily_ritual.py only when DAILY_RITUAL_WORK is
enabled. Run it manually once before trusting it on a schedule.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = os.getenv("DAILY_RITUAL_MODEL", "claude-sonnet-5")
MAX_AGENT_TURNS = 60

# Reuse the vendored MCP connection helper from the eval harness.
sys.path.insert(0, str(ROOT / "evals"))
from connections import MCPConnectionStdio  # noqa: E402

SYSTEM_PROMPT = """You are the autonomous daily worker for the Personal Task \
Assistant board. You act as the human's second worker: they decide, you \
execute, and everything you finish comes back to them for review.

Work loop, repeat until the codex backlog is empty or your claim budget is \
spent:
1. Call task_assistant_queue_summary to see the board.
2. Call task_assistant_claim_task to atomically take the next codex task.
   "No claimable task" means you are done — stop and summarize.
3. Do only what you can genuinely and safely complete from within these tools:
   triage, drafting text into the task, deciding an owner, splitting or
   clarifying a task, or updating priority/dates. You have NO shell, repo, or
   web access — if a task needs real code or outside action you cannot do
   here, do not fake it: leave a note via task_assistant_update_task (add to
   the description what remains) and either finish it to review with your
   findings or mark it blocked with the reason.
4. Call task_assistant_finish_task to send your result to waiting_review.

Never set status to "done" — that is the human's decision. Never inflate \
priority. Be concise. When you stop, write a short plain-text summary of what \
you claimed and what state you left each task in."""

FINAL_INSTRUCTION = (
    "Work the codex queue now. Claim tasks one at a time, act, and finish each to "
    "waiting_review. Stop when the queue is empty or your claim budget is spent, then "
    "summarize what you did."
)


def _text(content_blocks) -> str:
    parts = []
    for block in content_blocks:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(text)
    return "\n".join(parts)


async def _run(url: str, api_key: str, max_tasks: int, model: str) -> list[dict]:
    from anthropic import Anthropic

    client = Anthropic()  # reads ANTHROPIC_API_KEY
    env = {
        "TASK_TRACKER_URL": url,
        "TASK_TRACKER_API_KEY": api_key,
        "PATH": os.getenv("PATH", ""),
    }
    connection = MCPConnectionStdio(
        command=sys.executable,
        args=[str(ROOT / "adapters" / "task_assistant_mcp.py")],
        env=env,
    )

    worked: list[dict] = []
    claims = 0
    last_claim: dict | None = None

    async with connection as conn:
        tools = await conn.list_tools()
        anthropic_tools = [
            {"name": t["name"], "description": t["description"], "input_schema": t["input_schema"]}
            for t in tools
        ]
        messages = [{"role": "user", "content": FINAL_INSTRUCTION}]

        for _ in range(MAX_AGENT_TURNS):
            response = client.messages.create(
                model=model,
                max_tokens=2048,
                system=SYSTEM_PROMPT,
                tools=anthropic_tools,
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason != "tool_use":
                break

            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                name, params = block.name, (block.input or {}).get("params", block.input or {})
                result_text, claimed = await _dispatch(conn, name, params, claims, max_tasks)
                if claimed is not None:
                    claims += 1
                    last_claim = claimed
                if name == "task_assistant_finish_task" and last_claim is not None:
                    worked.append(last_claim)
                    last_claim = None
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": block.id, "content": result_text}
                )
            messages.append({"role": "user", "content": tool_results})

    return worked


async def _dispatch(conn, name: str, params: dict, claims: int, max_tasks: int) -> tuple[str, dict | None]:
    """Execute one tool call with the safety rails enforced here."""
    if name == "task_assistant_claim_task" and claims >= max_tasks:
        return (
            f"Claim budget reached ({max_tasks} tasks this run). Do not claim more; "
            "finish any task you are holding and stop.",
            None,
        )
    if name == "task_assistant_update_task" and str(params.get("status")) == "done":
        return (
            "Refused: only the human may set a task to done. Use finish_task to send it "
            "to waiting_review, or set status=blocked with a reason.",
            None,
        )

    content = await conn.call_tool(name, {"params": params})
    text = content[0].text if content else ""

    claimed = None
    if name == "task_assistant_claim_task" and text.strip().startswith("{"):
        try:
            claimed = json.loads(text)
        except json.JSONDecodeError:
            claimed = None
    return text, claimed


def run_work_loop(url: str, api_key: str, *, max_tasks: int = 3, model: str | None = None) -> list[dict]:
    """Sync entry point used by daily_ritual. Returns the tasks moved to review.

    Requires ANTHROPIC_API_KEY and the `anthropic` package. Returns [] and logs
    a clear message if either is missing, so the deterministic digest still runs.
    """
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("work loop skipped: ANTHROPIC_API_KEY is not set.", file=sys.stderr)
        return []
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("work loop skipped: `pip install anthropic` first.", file=sys.stderr)
        return []
    return asyncio.run(_run(url, api_key, max_tasks, model or DEFAULT_MODEL))
