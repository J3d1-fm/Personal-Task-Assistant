#!/usr/bin/env python3
"""Seed the deterministic evaluation board.

Creates a fresh SQLite database with a fixed set of tasks and fixed timestamps
(June 2026, all deadlines in the past), so every question in
evals/evaluation.xml has a stable, verifiable answer no matter when the
evaluation runs. Re-running the script always recreates the identical board.

Usage:
    .venv/bin/python evals/seed_eval_board.py [--db evals/eval_board.db]

Then serve it:
    DATABASE_URL=sqlite:///evals/eval_board.db TASK_TRACKER_API_KEY=eval-key \\
        .venv/bin/uvicorn app.main:app --port 8877
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.migrate import run_migrations  # noqa: E402
from app.models import Task  # noqa: E402

DEFAULT_DB_PATH = ROOT / "evals" / "eval_board.db"


def _dt(day: int, hour: int = 9, month: int = 6) -> datetime:
    return datetime(2026, month, day, hour, 0, tzinfo=timezone.utc)


# One coherent June-2026 story: shipping a playable release, raising a round,
# and a bit of personal life. Every datetime is fixed and in the past.
EVAL_TASKS: list[dict] = [
    # --- finished work (stable history) ---
    dict(
        title="Fix playable export size overflow for Mintegral build",
        assignee="codex", status="done", priority=1, origin="codex",
        source_name="Playable Forge CI",
        created_at=_dt(10), due_at=_dt(11, 18), completed_at=_dt(11, 15),
    ),
    dict(
        title="Prepare Drive Zone playable spec review",
        assignee="me", status="done", priority=2, origin="telegram",
        source_name="Telegram Ops", external_id="telegram:-100500:41:0",
        created_at=_dt(10, 10), due_at=_dt(13, 18), completed_at=_dt(12, 12),
    ),
    dict(
        title="Review Telegram adapter security",
        assignee="me", status="done", priority=1, origin="codex",
        source_name="Personal Codex",
        created_at=_dt(11), due_at=_dt(15, 18), completed_at=_dt(15, 9),
    ),
    dict(
        title="Collect retention metrics for seed deck",
        assignee="codex", status="done", priority=2, origin="slack",
        source_name="Slack #metrics",
        created_at=_dt(12), due_at=_dt(17, 18), completed_at=_dt(18, 11),
    ),
    dict(
        title="Migrate analytics to new endpoint",
        assignee="codex", status="cancelled", priority=3, origin="codex",
        created_at=_dt(12, 10),
    ),
    # --- active board (all deadlines already in the past => stable) ---
    dict(
        title="Sync YouTrack issues into assistant",
        assignee="unassigned", status="backlog", priority=3, origin="other",
        created_at=_dt(5, 8), due_at=_dt(30, 18),
    ),
    dict(
        title="Draft seed deck market slide",
        assignee="codex", status="waiting_review", priority=2, origin="codex",
        created_at=_dt(14), due_at=_dt(20, 18),
    ),
    dict(
        title="Approve final deck design",
        assignee="me", status="blocked", priority=1, origin="email",
        source_name="Gmail thread from designer",
        created_at=_dt(15), due_at=_dt(21, 18), reminder_at=_dt(20, 8),
    ),
    dict(
        title="Wait for AppLovin campaign approval",
        assignee="me", status="blocked", priority=2, origin="email",
        source_name="AppLovin support",
        created_at=_dt(16), due_at=_dt(26, 18),
    ),
    dict(
        title="Fix Night Run traffic spawn bug",
        assignee="codex", status="in_progress", priority=1, origin="slack",
        source_name="Slack #nightrun",
        created_at=_dt(16, 12), due_at=_dt(19, 18),
    ),
    dict(
        title="Prepare investor FAQ answers",
        assignee="me", status="in_progress", priority=2, origin="email",
        source_name="Gmail thread from Alex",
        created_at=_dt(17), due_at=_dt(22, 18),
    ),
    dict(
        title="Refactor task ranking tests",
        assignee="codex", status="backlog", priority=3, origin="codex",
        created_at=_dt(17, 10), due_at=_dt(25, 18),
    ),
    dict(
        title="Update Genhood site hero copy",
        assignee="codex", status="backlog", priority=4, origin="manual",
        created_at=_dt(18), due_at=_dt(27, 18),
    ),
    dict(
        title="Book dentist appointment",
        assignee="me", status="backlog", priority=5, origin="telegram",
        source_name="Telegram Ops", external_id="telegram:-100500:52:0",
        created_at=_dt(18, 12), due_at=_dt(28, 18),
    ),
    dict(
        title="Write launch post for Product Hunt",
        assignee="me", status="waiting_review", priority=2, origin="slack",
        source_name="Slack #launch",
        created_at=_dt(19), due_at=_dt(24, 18),
    ),
    dict(
        title="Archive old playable builds",
        assignee="unassigned", status="backlog", priority=5, origin="manual",
        created_at=_dt(19, 10), due_at=_dt(29, 18),
    ),
]


def seed(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    url = f"sqlite:///{db_path}"
    run_migrations(url)
    engine = create_engine(url)
    with Session(engine) as session:
        for spec in EVAL_TASKS:
            task = Task(**spec)
            task.updated_at = spec.get("completed_at") or spec["created_at"]
            session.add(task)
        session.commit()
    engine.dispose()
    active = sum(1 for spec in EVAL_TASKS if spec["status"] not in {"done", "cancelled"})
    print(f"Seeded {len(EVAL_TASKS)} tasks ({active} active) into {db_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed the deterministic evaluation board.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="SQLite file path for the eval board")
    args = parser.parse_args()
    seed(Path(args.db).resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
