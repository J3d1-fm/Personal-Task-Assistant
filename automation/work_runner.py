#!/usr/bin/env python3
"""Backend selector for the agent work loop.

Both the daily ritual and the watch loop run the same claim -> work ->
finish-to-review leg; WORK_RUNNER picks how the model is paid for:

- "claude" (also "claude-code" / "local") — headless Claude Code CLI on the
  human's existing subscription; no API key, no per-token billing. See
  automation/claude_work.py.
- "api" or unset — the Anthropic-API loop (automation/work_loop.py), which
  needs ANTHROPIC_API_KEY.

Every backend returns the same shape: the list of tasks it moved to
waiting_review, so digests and watch logs render identically either way.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

CLAUDE_BACKENDS = {"claude", "claude-code", "local"}


def backend_name() -> str:
    return "claude" if os.getenv("WORK_RUNNER", "").strip().lower() in CLAUDE_BACKENDS else "api"


def backend_ready() -> tuple[bool, str]:
    """Cheap pre-flight so callers can warn once instead of spamming retries."""
    if backend_name() == "claude":
        from claude_work import claude_binary

        if claude_binary() is None:
            return False, (
                "WORK_RUNNER=claude but the claude CLI is not available — install it "
                "(npm install -g @anthropic-ai/claude-code) or set WORK_CLAUDE_BIN."
            )
        return True, ""
    if not os.getenv("ANTHROPIC_API_KEY"):
        return False, (
            "work loop needs ANTHROPIC_API_KEY (or set WORK_RUNNER=claude to use the "
            "local Claude Code subscription instead)."
        )
    return True, ""


def run_work(url: str, api_key: str, *, max_tasks: int = 3) -> list[dict]:
    if backend_name() == "claude":
        from claude_work import run_claude_work

        return run_claude_work(url, api_key, max_tasks=max_tasks)
    from work_loop import run_work_loop

    return run_work_loop(url, api_key, max_tasks=max_tasks)
