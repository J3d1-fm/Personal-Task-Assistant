#!/usr/bin/env python3
"""Local work runner: the agent leg with no API billing.

Runs the same claim -> work -> finish-to-review loop as work_loop.py, but
through the locally installed Claude Code CLI (`claude -p`) — the human's
existing subscription pays for it; no ANTHROPIC_API_KEY, no per-token cost.

The safety envelope is enforced twice, in code, not in the prompt:

- The headless session is given ONLY the task-assistant MCP server
  (--strict-mcp-config ignores every other configured server) and only that
  server's tools are approved (--allowed-tools "mcp__task-assistant");
  unapproved tools are denied in print mode, so the session has no shell,
  repo, or web access — the same envelope as the API loop.
- The MCP server itself runs in worker mode (TASK_MCP_WORKER_MODE=1 with
  TASK_MCP_CLAIM_BUDGET=max_tasks): it refuses status=done/cancelled and
  stops handing out claims over budget, so the rails hold no matter what the
  model asks for.

Configuration (environment):
- WORK_CLAUDE_BIN: absolute path to the claude binary. Set it for launchd,
  whose PATH is minimal; default is `claude` resolved from PATH.
- WORK_TIMEOUT: seconds before the session is cut off (default 900).
- WORK_MODEL: optional --model override; unset uses the CLI's default.

The session runs in a throwaway empty directory so no project CLAUDE.md or
skills leak into the worker's context. Tasks moved to review are detected by
diffing board snapshots taken before and after the run, so even a timed-out
session reports what it actually finished.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

from work_loop import FINAL_INSTRUCTION, system_prompt  # noqa: E402

MCP_SERVER_NAME = "task-assistant"
DEFAULT_TIMEOUT = 900.0


def claude_binary() -> str | None:
    configured = os.getenv("WORK_CLAUDE_BIN", "").strip()
    if configured:
        return configured if os.access(configured, os.X_OK) else None
    return shutil.which("claude")


def build_mcp_config(url: str, api_key: str, max_tasks: int) -> dict:
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": [str(ROOT / "adapters" / "task_assistant_mcp.py")],
                "env": {
                    "TASK_TRACKER_URL": url,
                    "TASK_TRACKER_API_KEY": api_key,
                    "TASK_MCP_WORKER_MODE": "1",
                    "TASK_MCP_CLAIM_BUDGET": str(max_tasks),
                },
            }
        }
    }


def build_command(binary: str, config_path: str, *, model: str | None = None) -> list[str]:
    command = [
        binary,
        "-p",
        FINAL_INSTRUCTION,
        "--mcp-config",
        config_path,
        "--strict-mcp-config",
        "--allowed-tools",
        f"mcp__{MCP_SERVER_NAME}",
        "--append-system-prompt",
        system_prompt(),
        "--output-format",
        "json",
        "--permission-mode",
        "default",
    ]
    if model:
        command += ["--model", model]
    return command


def snapshot_board(url: str, api_key: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=20.0) as client:
        return client.get("/api/tasks", params={"limit": 500}).raise_for_status().json()


def worked_between(before: list[dict], after: list[dict]) -> list[dict]:
    """Tasks that entered waiting_review during the run, by board diff."""
    previously_in_review = {t["id"] for t in before if t.get("status") == "waiting_review"}
    return [
        t
        for t in after
        if t.get("status") == "waiting_review" and t["id"] not in previously_in_review
    ]


def _result_text(stdout: str) -> str:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return stdout.strip()
    if isinstance(payload, dict):
        return str(payload.get("result") or "").strip()
    return stdout.strip()


def run_claude_work(url: str, api_key: str, *, max_tasks: int = 3, model: str | None = None) -> list[dict]:
    """Sync entry point mirroring work_loop.run_work_loop. Returns tasks moved to review."""
    binary = claude_binary()
    if binary is None:
        print(
            "local work runner skipped: claude CLI not found. Install it "
            "(npm install -g @anthropic-ai/claude-code) or set WORK_CLAUDE_BIN.",
            file=sys.stderr,
        )
        return []
    model = model or os.getenv("WORK_MODEL", "").strip() or None
    timeout = float(os.getenv("WORK_TIMEOUT", str(DEFAULT_TIMEOUT)))

    try:
        before = snapshot_board(url, api_key)
    except httpx.HTTPError as exc:
        print(f"local work runner skipped: tracker unreachable: {exc}", file=sys.stderr)
        return []

    with tempfile.TemporaryDirectory(prefix="pta-work-") as workdir:
        config_path = Path(workdir) / "mcp-config.json"
        config_path.write_text(json.dumps(build_mcp_config(url, api_key, max_tasks)), encoding="utf-8")
        command = build_command(binary, str(config_path), model=model)
        try:
            completed = subprocess.run(
                command,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"local work runner: session cut off after {timeout:.0f}s.", file=sys.stderr)
            completed = None
        except OSError as exc:
            print(f"local work runner failed to start {binary}: {exc}", file=sys.stderr)
            return []

    if completed is not None:
        summary = _result_text(completed.stdout)
        if completed.returncode != 0:
            detail = summary or completed.stderr.strip()[:300]
            print(f"local work runner: claude exited {completed.returncode}: {detail}", file=sys.stderr)
        elif summary:
            print(f"local work runner summary: {summary[:600]}")

    try:
        after = snapshot_board(url, api_key)
    except httpx.HTTPError as exc:
        print(f"local work runner: cannot confirm results, tracker unreachable: {exc}", file=sys.stderr)
        return []
    return worked_between(before, after)
