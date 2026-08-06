#!/usr/bin/env python3
"""Daily task ritual for Personal Task Assistant.

Runs once a day (via launchd, see automation/README.md). Two layers:

1. Deterministic, always-runs: health-check the tracker, read the board over
   REST, build a situation report, and write a dated digest so the human
   always gets "here is your board today" even if nothing else runs.
2. Optional agent work loop: if enabled and an ANTHROPIC_API_KEY is present,
   an agent works the codex queue (claim -> do what it safely can -> finish
   to waiting_review) via the MCP server. See automation/work_loop.py.

The deterministic layer never mutates the board; the agent layer only ever
advances tasks to waiting_review, never to done — the human closes tasks.

Usage:
    python automation/daily_ritual.py                 # digest, + work loop if enabled
    python automation/daily_ritual.py --no-work       # digest only
    python automation/daily_ritual.py --max-tasks 3   # cap agent work loop
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_URL = os.getenv("TASK_TRACKER_URL", "http://127.0.0.1:8000")
LOG_DIR = ROOT / "automation" / "logs"


def _normalize(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _targets(task: dict) -> list[datetime]:
    return [t for t in (_normalize(task.get("due_at")), _normalize(task.get("reminder_at"))) if t]


def _is_active(task: dict) -> bool:
    return task["status"] not in ("done", "cancelled")


@dataclass
class Situation:
    now: datetime
    total_active: int
    overdue: list[dict] = field(default_factory=list)
    due_soon: list[dict] = field(default_factory=list)
    blocked: list[dict] = field(default_factory=list)
    waiting_review: list[dict] = field(default_factory=list)
    needs_triage: list[dict] = field(default_factory=list)  # active + unassigned
    agent_ready: list[dict] = field(default_factory=list)  # codex-owned, claimable


def build_situation(tasks: list[dict], now: datetime) -> Situation:
    """Pure classification of the board. No I/O, fully testable."""
    active = [t for t in tasks if _is_active(t)]
    soon = now + timedelta(days=1)

    def overdue(task: dict) -> bool:
        return any(target < now for target in _targets(task))

    def due_soon(task: dict) -> bool:
        return any(now <= target <= soon for target in _targets(task))

    return Situation(
        now=now,
        total_active=len(active),
        overdue=[t for t in active if overdue(t)],
        due_soon=[t for t in active if due_soon(t) and not overdue(t)],
        blocked=[t for t in active if t["status"] == "blocked"],
        waiting_review=[t for t in active if t["status"] == "waiting_review"],
        needs_triage=[t for t in active if t["assignee"] == "unassigned"],
        agent_ready=[
            t for t in active if t["assignee"] == "codex" and t["status"] in ("backlog", "in_progress")
        ],
    )


def _line(task: dict) -> str:
    due = (task.get("due_at") or "")[:10] or "no DD"
    src = f" — from {task['source_name']}" if task.get("source_name") else ""
    return f"- #{task['id']} [P{task['priority']}] {task['title']} ({task['assignee']}/{task['status']}, DD {due}){src}"


def _section(title: str, tasks: list[dict], empty: str) -> list[str]:
    if not tasks:
        return [f"### {title}", empty, ""]
    return [f"### {title} ({len(tasks)})", *[_line(t) for t in tasks], ""]


def render_digest(situation: Situation, worked: list[dict] | None = None) -> str:
    """Human-facing daily digest. Ordered by what needs the human first."""
    s = situation
    lines = [
        f"# Daily task digest — {s.now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        (
            f"{s.total_active} active · {len(s.overdue)} overdue · {len(s.waiting_review)} in review · "
            f"{len(s.blocked)} blocked · {len(s.needs_triage)} to triage · {len(s.agent_ready)} agent-ready"
        ),
        "",
        "## Needs you",
        "",
    ]
    lines += _section("Overdue", s.overdue, "Nothing overdue. ✅")
    lines += _section("Waiting for your review", s.waiting_review, "Nothing waiting on review.")
    lines += _section("Blocked — need your unblock", s.blocked, "Nothing blocked.")
    lines += _section("Unassigned — need an owner", s.needs_triage, "Everything is routed.")
    lines += _section("Due within 24h", s.due_soon, "Nothing due in the next day.")

    lines += ["## Agent side", ""]
    if worked is None:
        lines += _section("Agent-ready queue", s.agent_ready, "Agent queue is empty.")
        lines += ["_Agent work loop was not run this cycle._", ""]
    else:
        lines += [f"### Worked this cycle ({len(worked)})"]
        lines += ([_line(t) + " → waiting_review" for t in worked] if worked else ["Nothing to work."])
        lines += ["", *(_section("Still agent-ready", s.agent_ready, "Agent queue is empty.") if s.agent_ready else [])]
    return "\n".join(lines).rstrip() + "\n"


def render_spoken(situation: Situation, worked: list[dict] | None = None) -> str:
    """Short natural-language script for the voice digest — counts and the few
    hottest items by title, no ids or urls, comfortably under a minute."""
    s = situation

    def plural(count: int, noun: str) -> str:
        return f"{count} {noun}{'' if count == 1 else 's'}"

    sentences = [f"Here is your board. {plural(s.total_active, 'active task')}."]
    if s.overdue:
        titles = ", ".join(t["title"] for t in s.overdue[:3])
        sentences.append(f"{plural(len(s.overdue), 'task')} overdue, starting with: {titles}.")
    else:
        sentences.append("Nothing is overdue.")
    if s.waiting_review:
        titles = ", ".join(t["title"] for t in s.waiting_review[:3])
        sentences.append(f"Waiting for your review: {titles}.")
    if s.blocked:
        sentences.append(f"{plural(len(s.blocked), 'task')} blocked and waiting on you.")
    if s.needs_triage:
        sentences.append(f"{plural(len(s.needs_triage), 'task')} still need an owner.")
    if s.due_soon:
        sentences.append(f"{plural(len(s.due_soon), 'task')} due within a day.")
    if worked:
        titles = ", ".join(t["title"] for t in worked[:3])
        sentences.append(f"The agent finished {plural(len(worked), 'task')} to review: {titles}.")
    elif s.agent_ready:
        sentences.append(f"{plural(len(s.agent_ready), 'task')} ready for the agent.")
    return " ".join(sentences)


def notify_digest(digest: str, spoken: str, now: datetime) -> None:
    """Deliver the digest to Telegram when configured; never raises."""
    try:
        from notify import load_notify_config, send_audio, send_message, synthesize_speech
    except ImportError:
        sys.path.insert(0, str(ROOT / "automation"))
        from notify import load_notify_config, send_audio, send_message, synthesize_speech

    config = load_notify_config()
    if config is None:
        return
    if send_message(config, digest):
        print("digest sent to Telegram.")
    if config.voice_enabled:
        stamp = now.astimezone().strftime("%Y-%m-%d")
        audio = synthesize_speech(spoken, LOG_DIR / f"digest-{stamp}.m4a", voice=config.voice_name)
        if audio is not None and send_audio(config, audio, title=f"Board digest {stamp}"):
            print("spoken digest sent to Telegram.")


def fetch_board(url: str, api_key: str) -> list[dict]:
    headers = {"Authorization": f"Bearer {api_key}"}
    with httpx.Client(base_url=url.rstrip("/"), headers=headers, timeout=20.0) as client:
        client.get("/readyz").raise_for_status()
        response = client.get("/api/tasks", params={"include_done": True, "limit": 500})
        response.raise_for_status()
        return response.json()


def write_digest(text: str, now: datetime) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"digest-{now.astimezone().strftime('%Y-%m-%d')}.md"
    path.write_text(text, encoding="utf-8")
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the daily task ritual.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Task tracker base URL")
    parser.add_argument("--api-key", default=os.getenv("TASK_TRACKER_API_KEY", ""), help="Tracker API key")
    parser.add_argument("--no-work", action="store_true", help="Digest only; skip the agent work loop")
    parser.add_argument("--no-notify", action="store_true", help="Skip Telegram delivery of the digest")
    parser.add_argument("--max-tasks", type=int, default=3, help="Max tasks the agent work loop may claim")
    parser.add_argument("--now", default=None, help="Override 'now' as ISO 8601 (testing)")
    args = parser.parse_args()

    if not args.api_key or args.api_key == "change-me":
        print("ERROR: TASK_TRACKER_API_KEY is not set. Point it at the tracker's key.", file=sys.stderr)
        return 2

    now = _normalize(args.now) or datetime.now(timezone.utc)

    try:
        tasks = fetch_board(args.url, args.api_key)
    except httpx.HTTPError as exc:
        print(f"ERROR: cannot reach the tracker at {args.url}: {exc}", file=sys.stderr)
        print("Start Local Mode (run-local.command) or fix TASK_TRACKER_URL, then retry.", file=sys.stderr)
        return 3

    situation = build_situation(tasks, now)

    worked: list[dict] | None = None
    work_enabled = not args.no_work and os.getenv("DAILY_RITUAL_WORK", "").lower() in ("1", "true", "yes", "on")
    if work_enabled:
        try:
            from work_runner import run_work  # local import: only needed when enabled
        except ImportError:
            sys.path.insert(0, str(ROOT / "automation"))
            from work_runner import run_work
        worked = run_work(args.url, args.api_key, max_tasks=args.max_tasks)
        # Re-fetch so the digest reflects post-work state.
        situation = build_situation(fetch_board(args.url, args.api_key), now)

    digest = render_digest(situation, worked=worked)
    path = write_digest(digest, now)
    print(digest)
    print(f"\n(wrote {path})")
    if not args.no_notify:
        notify_digest(digest, render_spoken(situation, worked=worked), now)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
