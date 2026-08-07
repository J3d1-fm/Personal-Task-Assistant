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


STRINGS = {
    "en": {
        "title": "Daily task digest",
        "counts": "{active} active · {overdue} overdue · {review} in review · {blocked} blocked · {triage} to triage · {agent} agent-ready",
        "needs_you": "## Needs you",
        "overdue": ("Overdue", "Nothing overdue. ✅"),
        "review": ("Waiting for your review", "Nothing waiting on review."),
        "blocked": ("Blocked — need your unblock", "Nothing blocked."),
        "triage": ("Unassigned — need an owner", "Everything is routed."),
        "due_soon": ("Due within 24h", "Nothing due in the next day."),
        "agent_side": "## Agent side",
        "agent_queue": ("Agent-ready queue", "Agent queue is empty."),
        "no_work_run": "_Agent work loop was not run this cycle._",
        "worked": "Worked this cycle",
        "nothing_worked": "Nothing to work.",
        "still_ready": ("Still agent-ready", "Agent queue is empty."),
        "due": "DD",
        "no_due": "no DD",
        "from": "from",
    },
    "ru": {
        "title": "Дневной дайджест задач",
        "counts": "{active} активных · {overdue} просрочено · {review} на ревью · {blocked} заблокировано · {triage} без владельца · {agent} готовы для агента",
        "needs_you": "## Нужен ты",
        "overdue": ("Просрочено", "Ничего не просрочено. ✅"),
        "review": ("Ждут твоего ревью", "Ничего не ждёт ревью."),
        "blocked": ("Заблокировано — нужен твой разблок", "Ничего не заблокировано."),
        "triage": ("Без владельца — нужно назначить", "Всё распределено."),
        "due_soon": ("Дедлайн в ближайшие 24 часа", "В ближайшие сутки дедлайнов нет."),
        "agent_side": "## Сторона агента",
        "agent_queue": ("Очередь агента", "Очередь агента пуста."),
        "no_work_run": "_Рабочий цикл агента в этот раз не запускался._",
        "worked": "Сделано за цикл",
        "nothing_worked": "Нечего было делать.",
        "still_ready": ("Ещё в очереди агента", "Очередь агента пуста."),
        "due": "срок",
        "no_due": "без срока",
        "from": "из",
    },
}


def assistant_lang() -> str:
    return "ru" if os.getenv("ASSISTANT_LANG", "").strip().lower().startswith("ru") else "en"


def _line(task: dict, t: dict) -> str:
    due = (task.get("due_at") or "")[:10]
    due_text = f"{t['due']} {due}" if due else t["no_due"]
    src = f" — {t['from']} {task['source_name']}" if task.get("source_name") else ""
    return f"- #{task['id']} [P{task['priority']}] {task['title']} ({task['assignee']}/{task['status']}, {due_text}){src}"


def _section(pair: tuple[str, str], tasks: list[dict], t: dict) -> list[str]:
    title, empty = pair
    if not tasks:
        return [f"### {title}", empty, ""]
    return [f"### {title} ({len(tasks)})", *[_line(task, t) for task in tasks], ""]


def render_digest(situation: Situation, worked: list[dict] | None = None, lang: str = "en") -> str:
    """Human-facing daily digest. Ordered by what needs the human first."""
    s = situation
    t = STRINGS.get(lang, STRINGS["en"])
    lines = [
        f"# {t['title']} — {s.now.astimezone().strftime('%Y-%m-%d %H:%M %Z')}",
        "",
        t["counts"].format(
            active=s.total_active,
            overdue=len(s.overdue),
            review=len(s.waiting_review),
            blocked=len(s.blocked),
            triage=len(s.needs_triage),
            agent=len(s.agent_ready),
        ),
        "",
        t["needs_you"],
        "",
    ]
    lines += _section(t["overdue"], s.overdue, t)
    lines += _section(t["review"], s.waiting_review, t)
    lines += _section(t["blocked"], s.blocked, t)
    lines += _section(t["triage"], s.needs_triage, t)
    lines += _section(t["due_soon"], s.due_soon, t)

    lines += [t["agent_side"], ""]
    if worked is None:
        lines += _section(t["agent_queue"], s.agent_ready, t)
        lines += [t["no_work_run"], ""]
    else:
        lines += [f"### {t['worked']} ({len(worked)})"]
        lines += ([_line(task, t) + " → waiting_review" for task in worked] if worked else [t["nothing_worked"]])
        lines += ["", *(_section(t["still_ready"], s.agent_ready, t) if s.agent_ready else [])]
    return "\n".join(lines).rstrip() + "\n"


def ru_plural(count: int, one: str, few: str, many: str) -> str:
    """Russian plural agreement: 1 задача, 2-4 задачи, 5+ задач (teens -> many)."""
    tail, teens = count % 10, count % 100
    if tail == 1 and teens != 11:
        return f"{count} {one}"
    if tail in (2, 3, 4) and teens not in (12, 13, 14):
        return f"{count} {few}"
    return f"{count} {many}"


def _render_spoken_ru(s: Situation, worked: list[dict] | None) -> str:
    tasks_of = lambda items: ", ".join(t["title"] for t in items[:3])  # noqa: E731
    sentences = [f"Привет. На доске {ru_plural(s.total_active, 'активная задача', 'активные задачи', 'активных задач')}."]
    if s.overdue:
        sentences.append(f"Просрочено: {len(s.overdue)}. В первую очередь: {tasks_of(s.overdue)}.")
    else:
        sentences.append("Просроченного ничего нет.")
    if s.waiting_review:
        sentences.append(f"Твоего ревью ждут: {tasks_of(s.waiting_review)}.")
    if s.blocked:
        sentences.append(f"Заблокировано и ждёт тебя: {len(s.blocked)}.")
    if s.needs_triage:
        sentences.append(f"Без владельца: {len(s.needs_triage)}.")
    if s.due_soon:
        sentences.append(f"Дедлайн в ближайшие сутки у {ru_plural(len(s.due_soon), 'задачи', 'задач', 'задач')}.")
    if worked:
        sentences.append(f"Агент отправил в ревью: {tasks_of(worked)}.")
    elif s.agent_ready:
        sentences.append(f"Для агента готово: {len(s.agent_ready)}.")
    return " ".join(sentences)


def render_spoken(situation: Situation, worked: list[dict] | None = None, lang: str = "en") -> str:
    """Short natural-language script for the voice digest — counts and the few
    hottest items by title, no ids or urls, comfortably under a minute."""
    s = situation
    if lang == "ru":
        return _render_spoken_ru(s, worked)

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


def notify_digest(digest: str, spoken: str, now: datetime, lang: str = "en") -> None:
    """Deliver the digest to Telegram when configured; never raises."""
    try:
        from notify import load_notify_config, send_audio, send_message, synthesize_speech, voice_candidates
    except ImportError:
        sys.path.insert(0, str(ROOT / "automation"))
        from notify import load_notify_config, send_audio, send_message, synthesize_speech, voice_candidates

    config = load_notify_config()
    if config is None:
        return
    if send_message(config, digest):
        print("digest sent to Telegram.")
    if config.voice_enabled:
        stamp = now.astimezone().strftime("%Y-%m-%d")
        title = f"Дайджест доски {stamp}" if lang == "ru" else f"Board digest {stamp}"
        audio = synthesize_speech(
            spoken,
            LOG_DIR / f"digest-{stamp}.m4a",
            voices=voice_candidates(config.voice_name, lang),
            rate=config.voice_rate,
        )
        if audio is not None and send_audio(config, audio, title=title):
            print("spoken digest sent to Telegram.")
            audio.unlink(missing_ok=True)  # delivered — keep no audio around


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

    lang = assistant_lang()
    digest = render_digest(situation, worked=worked, lang=lang)
    path = write_digest(digest, now)
    print(digest)
    print(f"\n(wrote {path})")
    if not args.no_notify:
        notify_digest(digest, render_spoken(situation, worked=worked, lang=lang), now, lang=lang)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
