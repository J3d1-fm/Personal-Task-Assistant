"""Shared parser for agent work reports embedded in task descriptions.

The MCP server's finish_task appends a mandatory hand-off report to the task
description as a dated note:

    — 2026-08-15 07:30 (work): <report text, possibly multi-line>

This module extracts the latest such note so the daily digest and the watch
loop's review announcements can show the human WHAT was done and HOW without
a schema change. The report is always the tail of the description because
notes are append-only.
"""

from __future__ import annotations

import re

WORK_NOTE_RE = re.compile(r"^— \d{4}-\d{2}-\d{2} \d{2}:\d{2} \(work\): ", re.MULTILINE)


def latest_work_report(description: str | None) -> str | None:
    """Return the text of the last (work) note, or None when there is none."""
    matches = list(WORK_NOTE_RE.finditer(description or ""))
    if not matches:
        return None
    report = (description or "")[matches[-1].end() :].strip()
    return report or None
