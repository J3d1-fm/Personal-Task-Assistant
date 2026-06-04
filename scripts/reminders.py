#!/usr/bin/env python3
import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone

import requests


def main() -> int:
    parser = argparse.ArgumentParser(description="Send due task reminders through Personal Codex notify.")
    parser.add_argument("--base-url", default=os.getenv("TASK_TRACKER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--api-key", default=os.getenv("TASK_TRACKER_API_KEY"))
    parser.add_argument(
        "--notify",
        default=os.getenv("TASK_REMINDER_NOTIFY_COMMAND", "personal-codex-notify"),
        help="Notification command to run for each due reminder.",
    )
    args = parser.parse_args()

    if not args.api_key:
        print("TASK_TRACKER_API_KEY is required", file=sys.stderr)
        return 2

    response = requests.get(
        f"{args.base_url.rstrip('/')}/api/reminders/due",
        headers={"Authorization": f"Bearer {args.api_key}"},
        timeout=20,
    )
    response.raise_for_status()
    tasks = response.json()

    for task in tasks:
        title = f"Task reminder: {task['title'][:80]}"
        message = task.get("description") or f"Status: {task['status']}, assignee: {task['assignee']}"
        try:
            subprocess.run(
                [args.notify, "--level", "important", "--title", title, message],
                check=False,
            )
        except FileNotFoundError:
            print(f"notification command not found: {args.notify}", file=sys.stderr)
            return 3
        requests.patch(
            f"{args.base_url.rstrip('/')}/api/tasks/{task['id']}",
            headers={"Authorization": f"Bearer {args.api_key}"},
            json={"reminder_last_sent_at": datetime.now(timezone.utc).isoformat()},
            timeout=20,
        ).raise_for_status()

    print(f"sent {len(tasks)} reminders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
