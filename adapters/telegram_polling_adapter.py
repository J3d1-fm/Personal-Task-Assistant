#!/usr/bin/env python3
"""Reference Telegram polling adapter for Personal Task Assistant.

The adapter intentionally uses a simple prefix grammar instead of an LLM. This
keeps the reference path cheap, deterministic, and safe for non-technical users.
More advanced users can replace parse_tasks_from_text with their own AI parser.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timezone
from pathlib import Path
from typing import Any

import requests


TASK_PREFIX_RE = re.compile(
    r"^\s*(?:(p[1-5])\s+)?(codex|agent|ai|me|human|review|blocked|todo)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
DUE_RE = re.compile(r"\b(?:dd|due):(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
TELEGRAM_TOKEN_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+")


@dataclass(frozen=True)
class AdapterConfig:
    telegram_bot_token: str
    task_tracker_url: str
    task_tracker_api_key: str
    allowed_chat_ids: set[int]
    allow_all_chats: bool
    state_path: Path
    poll_timeout: int
    poll_interval: float
    dry_run: bool
    once: bool


def load_config() -> AdapterConfig:
    parser = argparse.ArgumentParser(description="Poll Telegram and create Personal Task Assistant tasks.")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--dry-run", action="store_true", help="print payloads without writing tasks")
    parser.add_argument(
        "--state-path",
        default=os.getenv("TELEGRAM_ADAPTER_STATE", ".adapter_state/telegram_polling_state.json"),
        help="path for Telegram update offset state",
    )
    args = parser.parse_args()

    token = required_env("TELEGRAM_BOT_TOKEN")
    tracker_url = os.getenv("TASK_TRACKER_URL", "http://127.0.0.1:8000").rstrip("/")
    tracker_key = required_env("TASK_TRACKER_API_KEY")
    allowed_chat_ids = parse_allowed_chat_ids(os.getenv("TELEGRAM_ALLOWED_CHAT_IDS", ""))
    allow_all_chats = parse_bool_env("TELEGRAM_ALLOW_ALL_CHATS")
    if allowed_chat_ids and allow_all_chats:
        raise SystemExit("Set either TELEGRAM_ALLOWED_CHAT_IDS or TELEGRAM_ALLOW_ALL_CHATS, not both")
    if not allowed_chat_ids and not allow_all_chats:
        raise SystemExit(
            "TELEGRAM_ALLOWED_CHAT_IDS must be set. To intentionally accept every chat, set "
            "TELEGRAM_ALLOW_ALL_CHATS=true."
        )
    return AdapterConfig(
        telegram_bot_token=token,
        task_tracker_url=tracker_url,
        task_tracker_api_key=tracker_key,
        allowed_chat_ids=allowed_chat_ids,
        allow_all_chats=allow_all_chats,
        state_path=Path(args.state_path),
        poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25")),
        poll_interval=float(os.getenv("TELEGRAM_POLL_INTERVAL", "2")),
        dry_run=args.dry_run,
        once=args.once,
    )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "change-me":
        raise SystemExit(f"{name} must be set")
    return value


def parse_allowed_chat_ids(raw_value: str) -> set[int]:
    values = {item.strip() for item in raw_value.split(",") if item.strip()}
    if not values:
        return set()
    try:
        return {int(item) for item in values}
    except ValueError as exc:
        raise SystemExit("TELEGRAM_ALLOWED_CHAT_IDS must contain comma-separated numeric chat ids") from exc


def parse_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "y", "on"}:
        return True
    if value in {"0", "false", "no", "n", "off"}:
        return False
    raise SystemExit(f"{name} must be true or false")


def read_offset(path: Path) -> int | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    offset = data.get("offset")
    return int(offset) if offset is not None else None


def write_offset(path: Path, offset: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump({"offset": offset, "updated_at": datetime.now(timezone.utc).isoformat()}, handle, indent=2)
        handle.write("\n")


def get_updates(config: AdapterConfig, offset: int | None) -> list[dict[str, Any]]:
    params: dict[str, Any] = {
        "timeout": config.poll_timeout,
        "allowed_updates": json.dumps(["message"]),
    }
    if offset is not None:
        params["offset"] = offset
    try:
        response = requests.get(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/getUpdates",
            params=params,
            timeout=config.poll_timeout + 10,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram getUpdates request failed: {sanitize_error(exc, config)}") from None
    if not response.ok:
        detail = sanitize_error(response.text[:300], config)
        raise RuntimeError(f"Telegram getUpdates failed with HTTP {response.status_code}: {detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("Telegram getUpdates returned invalid JSON") from exc
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {sanitize_error(payload, config)}")
    return list(payload.get("result", []))


def sanitize_error(value: object, config: AdapterConfig) -> str:
    text = str(value)
    text = text.replace(config.telegram_bot_token, "<redacted>")
    return TELEGRAM_TOKEN_URL_RE.sub(r"\1<redacted>", text)


def is_allowed_chat(config: AdapterConfig, message: dict[str, Any]) -> bool:
    chat_id = int(message.get("chat", {}).get("id", 0))
    return config.allow_all_chats or chat_id in config.allowed_chat_ids


def parse_tasks_from_text(text: str) -> list[dict[str, Any]]:
    tasks = []
    for line in text.splitlines():
        match = TASK_PREFIX_RE.match(line)
        if not match:
            continue
        priority_token, prefix, title = match.groups()
        due_at = extract_due_at(title)
        clean_title = DUE_RE.sub("", title).strip(" -")
        if not clean_title:
            continue
        tasks.append(
            {
                "title": clean_title[:240],
                "description": f"Extracted from Telegram prefix `{prefix.lower()}:`.",
                "status": status_for_prefix(prefix),
                "assignee": assignee_for_prefix(prefix),
                "priority": priority_for_prefix(prefix, priority_token),
                **({"due_at": due_at} if due_at else {}),
            }
        )
    return tasks


def extract_due_at(text: str) -> str | None:
    match = DUE_RE.search(text)
    if not match:
        return None
    try:
        date_value = datetime.strptime(match.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None
    due_at = datetime.combine(date_value, datetime_time(hour=18), tzinfo=timezone.utc)
    return due_at.isoformat().replace("+00:00", "Z")


def assignee_for_prefix(prefix: str) -> str:
    normalized = prefix.lower()
    if normalized in {"codex", "agent", "ai"}:
        return "codex"
    if normalized in {"me", "human", "review"}:
        return "me"
    return "unassigned"


def status_for_prefix(prefix: str) -> str:
    normalized = prefix.lower()
    if normalized == "review":
        return "waiting_review"
    if normalized == "blocked":
        return "blocked"
    return "backlog"


def priority_for_prefix(prefix: str, explicit_priority: str | None) -> int:
    if explicit_priority:
        return int(explicit_priority[1])
    normalized = prefix.lower()
    if normalized in {"codex", "agent", "ai", "me", "human", "review"}:
        return 2
    return 3


def build_source_url(message: dict[str, Any]) -> str | None:
    chat = message.get("chat", {})
    username = chat.get("username")
    message_id = message.get("message_id")
    if username and message_id:
        return f"https://t.me/{username}/{message_id}"
    return None


def build_ingest_payload(message: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    chat = message.get("chat", {})
    chat_title = chat.get("title") or chat.get("username") or chat.get("first_name") or chat.get("id")
    text = message.get("text") or ""
    message_id = message.get("message_id")
    return {
        "origin": "telegram",
        "source_name": f"Telegram {chat_title}",
        "source_url": build_source_url(message),
        "source_context": text,
        "tasks": [
            {
                **task,
                "description": f"{task['description']} Telegram message {message_id}.",
            }
            for task in tasks
        ],
    }


def post_to_task_assistant(config: AdapterConfig, payload: dict[str, Any]) -> None:
    if config.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    try:
        response = requests.post(
            f"{config.task_tracker_url}/api/agent/ingest/context",
            headers={
                "Authorization": f"Bearer {config.task_tracker_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Task Assistant ingest request failed: {exc}") from None
    if not response.ok:
        raise RuntimeError(f"Task Assistant ingest failed with HTTP {response.status_code}: {response.text[:300]}")
    created = response.json().get("created", [])
    print(f"created {len(created)} task(s) from {payload['source_name']}")


def process_update(config: AdapterConfig, update: dict[str, Any]) -> None:
    message = update.get("message") or {}
    if not message or not is_allowed_chat(config, message):
        return
    text = message.get("text") or ""
    tasks = parse_tasks_from_text(text)
    if not tasks:
        return
    post_to_task_assistant(config, build_ingest_payload(message, tasks))


def main() -> int:
    config = load_config()
    offset = read_offset(config.state_path)
    while True:
        updates = get_updates(config, offset)
        for update in updates:
            process_update(config, update)
            offset = int(update["update_id"]) + 1
            if not config.dry_run:
                write_offset(config.state_path, offset)
        if config.once:
            return 0
        time.sleep(config.poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
    except RuntimeError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        raise SystemExit(1)
