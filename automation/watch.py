#!/usr/bin/env python3
"""Watch loop — the assistant's reflexes between daily rituals.

Polls the tracker every WATCH_INTERVAL seconds and reacts to three things the
once-a-day ritual is too slow for:

1. **Due reminders** — pushes each due reminder to Telegram, then stamps
   `reminder_last_sent_at` so the tracker's own debounce (`is_reminder_due`)
   does not re-fire until the next target. Without Telegram configured the
   stamp is never written, so nothing is silently swallowed.
2. **Tasks entering waiting_review** — sends a review request with inline
   ✅/🔁/✋ buttons. Button presses come back through the polling adapter (the
   bot's single getUpdates consumer), not through this loop.
3. **New agent-ready backlog** — with WATCH_WORK enabled (plus
   ANTHROPIC_API_KEY), kicks the same work loop the daily ritual uses, so a
   task assigned to the agent is picked up within one interval instead of at
   the next 15:00. All work-loop safety rails apply unchanged.

Which review requests were already announced is remembered in
`automation/.watch_state.json`; a task that leaves review and re-enters it is
announced again. The loop is deliberately tolerant: a tracker or Telegram
outage logs and retries next tick, it never crashes the process (KeepAlive
supervisors would otherwise thrash).

Usage:
    python automation/watch.py            # loop until stopped
    python automation/watch.py --once     # single tick (manual check / tests)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "automation"))

from notify import (  # noqa: E402
    NotifyConfig,
    assistant_lang,
    load_notify_config,
    review_reply_markup,
    send_message,
)

DEFAULT_URL = os.getenv("TASK_TRACKER_URL", "http://127.0.0.1:8000")
DEFAULT_STATE = ROOT / "automation" / ".watch_state.json"

WATCH_STRINGS = {
    "en": {"review": "👀 Review needed", "reminder": "⏰ Reminder", "due": "due"},
    "ru": {"review": "👀 Нужно ревью", "reminder": "⏰ Напоминание", "due": "срок"},
}


@dataclass(frozen=True)
class WatchConfig:
    url: str
    api_key: str
    interval: float
    state_path: Path
    work_enabled: bool
    work_max_tasks: int
    notify: NotifyConfig | None
    lang: str = "en"


def _bool_env(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "y", "on"}


def load_watch_config(args: argparse.Namespace) -> WatchConfig:
    api_key = args.api_key or os.getenv("TASK_TRACKER_API_KEY", "")
    if not api_key or api_key == "change-me":
        raise SystemExit("TASK_TRACKER_API_KEY must be set")
    return WatchConfig(
        url=args.url.rstrip("/"),
        api_key=api_key,
        interval=max(5.0, float(os.getenv("WATCH_INTERVAL", "30"))),
        state_path=Path(os.getenv("WATCH_STATE", str(DEFAULT_STATE))),
        work_enabled=_bool_env("WATCH_WORK"),
        work_max_tasks=max(1, int(os.getenv("WATCH_WORK_MAX_TASKS", "3"))),
        notify=load_notify_config(),
        lang=assistant_lang(),
    )


# ---------- pure planning (unit-tested, no I/O) ----------


def plan_review_notifications(
    tasks: list[dict], notified: dict[str, str]
) -> tuple[list[dict], dict[str, str]]:
    """Return (tasks to announce, retained state for tasks still in review).

    State maps task id -> updated_at at announcement time. Tasks that left
    review are pruned, so re-entering review gets announced again. Successful
    announcements are added to the retained state by the caller.
    """
    in_review = [t for t in tasks if t.get("status") == "waiting_review"]
    review_ids = {str(t["id"]) for t in in_review}
    retained = {task_id: seen for task_id, seen in notified.items() if task_id in review_ids}
    to_announce = [t for t in in_review if str(t["id"]) not in notified]
    return to_announce, retained


def agent_backlog(tasks: list[dict]) -> list[dict]:
    return [t for t in tasks if t.get("assignee") == "codex" and t.get("status") == "backlog"]


def format_review_request(task: dict, lang: str = "en") -> str:
    t = WATCH_STRINGS.get(lang, WATCH_STRINGS["en"])
    lines = [
        f"{t['review']}: #{task['id']} {task['title']}",
        f"P{task['priority']} · {task['assignee']} · waiting_review",
    ]
    description = (task.get("description") or "").strip()
    if description:
        snippet = description[:400] + ("…" if len(description) > 400 else "")
        lines += ["", snippet]
    if task.get("source_url"):
        lines += ["", str(task["source_url"])]
    return "\n".join(lines)


def format_reminder(task: dict, lang: str = "en") -> str:
    t = WATCH_STRINGS.get(lang, WATCH_STRINGS["en"])
    due = (task.get("due_at") or task.get("reminder_at") or "")[:16].replace("T", " ")
    lines = [
        f"{t['reminder']}: #{task['id']} {task['title']}",
        f"P{task['priority']} · {task['assignee']}/{task['status']}" + (f" · {t['due']} {due}" if due else ""),
    ]
    return "\n".join(lines)


# ---------- state ----------


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"review_notified": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"review_notified": {}}
    if not isinstance(data.get("review_notified"), dict):
        data["review_notified"] = {}
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


# ---------- one tick ----------


def tick(
    config: WatchConfig,
    state: dict,
    *,
    now: datetime | None = None,
    client: httpx.Client | None = None,
) -> dict:
    current = now or datetime.now(timezone.utc)
    headers = {"Authorization": f"Bearer {config.api_key}"}
    own_client = client is None
    if client is None:
        client = httpx.Client(base_url=config.url, headers=headers, timeout=20.0)
    try:
        tasks = client.get("/api/tasks", params={"limit": 500}).raise_for_status().json()

        if config.notify is not None:
            reminders = client.get("/api/reminders/due").raise_for_status().json()
            for task in reminders:
                if send_message(config.notify, format_reminder(task, config.lang)):
                    client.patch(
                        f"/api/tasks/{task['id']}",
                        json={"reminder_last_sent_at": current.isoformat()},
                    ).raise_for_status()
                    print(f"reminder sent for #{task['id']} {task['title']}")

            to_announce, retained = plan_review_notifications(tasks, state.get("review_notified", {}))
            for task in to_announce:
                markup = review_reply_markup(int(task["id"]), config.lang)
                if send_message(config.notify, format_review_request(task, config.lang), reply_markup=markup):
                    retained[str(task["id"])] = str(task.get("updated_at") or "")
                    print(f"review request sent for #{task['id']} {task['title']}")
            state["review_notified"] = retained
    finally:
        if own_client:
            client.close()

    if config.work_enabled and agent_backlog(tasks):
        from work_runner import backend_ready, run_work

        ready, reason = backend_ready()
        if not ready:
            if not state.get("work_warned"):
                print(f"WATCH_WORK is on but {reason}", file=sys.stderr)
                state["work_warned"] = True
        else:
            state.pop("work_warned", None)
            worked = run_work(config.url, config.api_key, max_tasks=config.work_max_tasks)
            for task in worked:
                print(f"agent finished #{task.get('id')} {task.get('title')} → waiting_review")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the tracker watch loop.")
    parser.add_argument("--url", default=DEFAULT_URL, help="Task tracker base URL")
    parser.add_argument("--api-key", default="", help="Tracker API key (default: TASK_TRACKER_API_KEY)")
    parser.add_argument("--once", action="store_true", help="Run a single tick and exit")
    args = parser.parse_args()

    config = load_watch_config(args)
    state = load_state(config.state_path)
    if config.notify is None:
        print(
            "Telegram is not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_NOTIFY_CHAT_ID); "
            "reminders and review requests stay in the tracker only.",
            file=sys.stderr,
        )

    while True:
        try:
            state = tick(config, state)
            save_state(config.state_path, state)
        except httpx.HTTPError as exc:
            print(f"tick failed: tracker unreachable at {config.url}: {exc}", file=sys.stderr)
        if args.once:
            return 0
        time.sleep(config.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
