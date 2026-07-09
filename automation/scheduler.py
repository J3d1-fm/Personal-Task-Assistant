#!/usr/bin/env python3
"""In-process daily scheduler — the container equivalent of the launchd job.

Runs forever: sleeps until RITUAL_TIME (HH:MM, local to $TZ) each day, then
executes daily_ritual.py as a subprocess and goes back to sleep. Used as the
`ritual` service command in docker-compose.yml, where launchd/cron/systemd are
not available. A failed ritual run is logged and does not kill the scheduler —
the next day still happens.

Environment:
    RITUAL_TIME   when to run, HH:MM 24h (default 15:00)
    RITUAL_ARGS   extra args for daily_ritual.py, e.g. "--max-tasks 5"
    plus everything daily_ritual.py itself reads (TASK_TRACKER_URL, keys, ...).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_NAP = 300  # wake at least every 5 minutes: keeps sleep interruptible and TZ-change tolerant


def parse_ritual_time(raw: str) -> tuple[int, int]:
    hour, _, minute = raw.strip().partition(":")
    parsed = (int(hour), int(minute or 0))
    if not (0 <= parsed[0] <= 23 and 0 <= parsed[1] <= 59):
        raise ValueError(f"RITUAL_TIME must be HH:MM 24h, got {raw!r}")
    return parsed


def seconds_until_next_run(now: datetime, hour: int, minute: int) -> float:
    """Seconds from `now` (naive, local) to the next daily HH:MM occurrence."""
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def run_ritual(extra_args: list[str]) -> int:
    command = [sys.executable, str(ROOT / "automation" / "daily_ritual.py"), *extra_args]
    print(f"[scheduler] running: {' '.join(command)}", flush=True)
    completed = subprocess.run(command)
    print(f"[scheduler] ritual finished with status {completed.returncode}", flush=True)
    return completed.returncode


def main() -> int:
    hour, minute = parse_ritual_time(os.getenv("RITUAL_TIME", "15:00"))
    extra_args = shlex.split(os.getenv("RITUAL_ARGS", ""))
    print(f"[scheduler] daily ritual scheduled at {hour:02d}:{minute:02d} local time", flush=True)

    while True:
        remaining = seconds_until_next_run(datetime.now(), hour, minute)
        print(f"[scheduler] next run in {remaining / 3600:.1f}h", flush=True)
        while remaining > 0:
            time.sleep(min(remaining, MAX_NAP))
            remaining = seconds_until_next_run(datetime.now(), hour, minute)
            # crossed the boundary when the next run flipped to ~24h away
            if remaining > 23.5 * 3600:
                break
        run_ritual(extra_args)


if __name__ == "__main__":
    raise SystemExit(main())
