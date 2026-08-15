#!/usr/bin/env python3
"""Deterministic ground-truth check for evals/evaluation.xml.

Connects to the real MCP server (adapters/task_assistant_mcp.py) over stdio —
exactly like Claude Code would — solves every question in evaluation.xml using
only read-only tools, and compares the computed answers against the expected
ones. This validates the seeded board, the questions, and the MCP tool
surface without spending LLM tokens; the vendored evaluation.py harness then
measures whether an LLM can do the same.

Usage (with the eval board served on port 8877):
    .venv/bin/python evals/seed_eval_board.py
    DATABASE_URL=sqlite:///evals/eval_board.db TASK_TRACKER_API_KEY=eval-key \\
        .venv/bin/uvicorn app.main:app --port 8877 &
    .venv/bin/python evals/verify_answers.py

    --write-loop additionally exercises claim -> finish; re-seed afterwards.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import get_default_environment, stdio_client

ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOOL_COUNT = 9


def parse_expected(eval_file: Path) -> list[tuple[str, str]]:
    root = ET.parse(eval_file).getroot()
    return [
        (pair.findtext("question", "").strip(), pair.findtext("answer", "").strip())
        for pair in root.findall("qa_pair")
    ]


def compute_answers(all_tasks: list[dict], smart_codex_backlog: list[dict]) -> list[str]:
    active = [task for task in all_tasks if task["status"] not in ("done", "cancelled")]
    done = [task for task in all_tasks if task["status"] == "done"]
    blocked = [task for task in active if task["status"] == "blocked"]

    last_done = max(done, key=lambda task: task["completed_at"])

    human_input = [
        task for task in active if task["assignee"] == "me" or task["status"] in ("waiting_review", "blocked")
    ]

    telegram_active = [task for task in active if task["origin"] == "telegram" and task.get("external_id")]
    assert len(telegram_active) == 1, f"expected 1 active telegram task with external_id, got {len(telegram_active)}"

    completed_in_week = [
        task for task in done if "2026-06-09" <= (task["completed_at"] or "")[:10] <= "2026-06-15"
    ]

    earliest_blocked = min(blocked, key=lambda task: task["due_at"])

    oldest_active = min(active, key=lambda task: task["created_at"])

    origin_counts = Counter(task["origin"] for task in active).most_common(2)
    assert origin_counts[0][1] > origin_counts[1][1], f"origin counts must have a unique winner: {origin_counts}"

    with_reminder = [task for task in active if task.get("reminder_at")]
    assert len(with_reminder) == 1, f"expected exactly 1 active task with reminder_at, got {len(with_reminder)}"

    investor_faq = next(task for task in all_tasks if "investor FAQ" in task["title"])
    deck_approval = next(task for task in all_tasks if "final deck design" in task["title"])

    return [
        last_done["title"],
        str(len(human_input)),
        telegram_active[0]["external_id"],
        smart_codex_backlog[0]["title"],
        str(len(completed_in_week)),
        earliest_blocked["due_at"][:10],
        str(oldest_active["priority"]),
        origin_counts[0][0],
        str(with_reminder[0]["priority"]),
        "True" if investor_faq["created_at"] < deck_approval["created_at"] else "False",
    ]


async def run(args: argparse.Namespace) -> int:
    env = {
        **get_default_environment(),
        "TASK_TRACKER_URL": args.url,
        "TASK_TRACKER_API_KEY": args.api_key,
    }
    server = StdioServerParameters(command=sys.executable, args=[str(ROOT / args.server)], env=env)

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print(f"Connected over stdio; server exposes {len(tools.tools)} tools.")
            assert len(tools.tools) == EXPECTED_TOOL_COUNT, "unexpected tool count"

            async def call(name: str, params: dict | None = None) -> str:
                result = await session.call_tool(name, {"params": params or {}})
                assert not result.isError, f"{name} returned an error: {result.content}"
                return result.content[0].text

            all_tasks = json.loads(
                await call(
                    "task_assistant_list_tasks",
                    {"include_done": True, "limit": 500, "response_format": "json"},
                )
            )
            smart_queue = json.loads(
                await call(
                    "task_assistant_get_queue",
                    {"assignee": "codex", "status": "backlog", "sort": "smart", "response_format": "json"},
                )
            )
            summary = await call("task_assistant_queue_summary")
            reminders = await call("task_assistant_due_reminders")

            active_count = sum(1 for task in all_tasks if task["status"] not in ("done", "cancelled"))
            print(f"Board: {len(all_tasks)} tasks total, {active_count} active.")
            print(f"Summary tool: {summary}")
            reminder_lines = [line for line in reminders.splitlines() if line.startswith("- #")]
            print(f"Reminder tool: {len(reminder_lines)} tasks due.")

            computed = compute_answers(all_tasks, smart_queue["tasks"])
            expected = parse_expected(ROOT / args.eval_file)
            assert len(expected) == len(computed), "question count mismatch"

            failures = 0
            for index, ((question, want), got) in enumerate(zip(expected, computed, strict=False), start=1):
                ok = want == got
                failures += 0 if ok else 1
                mark = "OK " if ok else "FAIL"
                print(f"[{mark}] Q{index}: computed={got!r} expected={want!r}")
                if not ok:
                    print(f"       {question}")

            if args.write_loop:
                print("\nWrite loop: claim -> finish")
                claimed = json.loads(await call("task_assistant_claim_task"))
                print(f"  claimed #{claimed['id']} {claimed['title']!r} -> {claimed['status']}")
                assert claimed["status"] == "in_progress"
                finished = json.loads(
                    await call(
                        "task_assistant_finish_task",
                        {
                            "task_id": claimed["id"],
                            "report": (
                                "Write-loop check: claimed the top task and finished it "
                                "back to review; verify the status flip only."
                            ),
                        },
                    )
                )
                print(f"  finished #{finished['id']} -> {finished['status']}")
                assert finished["status"] == "waiting_review"
                assert "(work):" in (finished.get("description") or "")
                print("  Board mutated: re-run evals/seed_eval_board.py before the next evaluation.")

            if failures:
                print(f"\n{failures} answer(s) diverged from evaluation.xml")
                return 1
            print("\nAll answers verified against evaluation.xml.")
            return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify evaluation answers through the real MCP server.")
    parser.add_argument("--url", default="http://127.0.0.1:8877", help="Running task tracker base URL")
    parser.add_argument("--api-key", default="eval-key", help="API key of the running instance")
    parser.add_argument("--server", default="adapters/task_assistant_mcp.py", help="MCP server script path")
    parser.add_argument("--eval-file", default="evals/evaluation.xml", help="Evaluation XML path")
    parser.add_argument("--write-loop", action="store_true", help="Also exercise claim -> finish (mutates board)")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
