#!/usr/bin/env python3
"""Reference Telegram polling adapter for Personal Task Assistant.

The adapter intentionally uses a simple prefix grammar instead of an LLM. This
keeps the reference path cheap, deterministic, and safe for non-technical users.
More advanced users can replace parse_tasks_from_text with their own AI parser.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as datetime_time
from pathlib import Path
from typing import Any

import requests

TASK_PREFIX_RE = re.compile(
    r"^\s*(?:(p[1-5])\s+)?(codex|agent|ai|me|human|review|blocked|todo)\s*:\s*(.+?)\s*$",
    re.IGNORECASE,
)
DUE_RE = re.compile(r"\b(?:dd|due):(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)
TELEGRAM_TOKEN_URL_RE = re.compile(r"(https://api\.telegram\.org/bot)[^/\s]+")

# Inline-button actions on review requests sent by automation/notify.py. The
# button press is the human's decision arriving through Telegram — this is the
# one path in the adapter that may close a task, and it acts only on explicit
# callback data from an allowed chat. Confirmation texts follow ASSISTANT_LANG;
# the callback_data protocol never changes.
CALLBACK_ACTION_RE = re.compile(r"^task:(done|rework|block):(\d+)$")
CALLBACK_ACTIONS: dict[str, tuple[dict[str, str], dict[str, str]]] = {
    "done": (
        {"status": "done"},
        {"en": "✅ Approved — task closed.", "ru": "✅ Принято — задача закрыта."},
    ),
    "rework": (
        {"status": "in_progress"},
        {"en": "🔁 Sent back to in progress.", "ru": "🔁 Возвращена в работу."},
    ),
    "block": (
        {"status": "blocked"},
        {"en": "✋ Blocked — add the reason in the tracker.", "ru": "✋ Заблокирована — причину добавь в трекере."},
    ),
}
CALLBACK_MESSAGES: dict[str, dict[str, str]] = {
    "unsupported": {"en": "Unsupported action.", "ru": "Неизвестное действие."},
    "failed": {"en": "Failed to update #{task_id} — see adapter logs.", "ru": "Не получилось обновить #{task_id} — смотри логи адаптера."},
}


def assistant_lang() -> str:
    return "ru" if os.getenv("ASSISTANT_LANG", "").strip().lower().startswith("ru") else "en"


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
    source_id: str
    source_name: str
    health_report_interval: float
    max_retry_interval: float
    dry_run: bool
    once: bool


@dataclass
class RunMetrics:
    items_checked: int = 0
    candidates: int = 0
    created: int = 0
    duplicates: int = 0
    ignored: int = 0
    suppressed: int = 0

    def add(self, other: RunMetrics) -> None:
        self.items_checked += other.items_checked
        self.candidates += other.candidates
        self.created += other.created
        self.duplicates += other.duplicates
        self.ignored += other.ignored
        self.suppressed += other.suppressed

    def as_dict(self) -> dict[str, int]:
        return {
            "items_checked": self.items_checked,
            "candidates": self.candidates,
            "created": self.created,
            "duplicates": self.duplicates,
            "ignored": self.ignored,
            "suppressed": self.suppressed,
        }


class SourceReadError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        health_status: str,
        error_code: str,
        retry_after: float | None = None,
    ):
        super().__init__(message)
        self.health_status = health_status
        self.error_code = error_code
        self.retry_after = retry_after


class TrackerRequestError(RuntimeError):
    pass


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
    default_source_id = telegram_source_id(token)
    return AdapterConfig(
        telegram_bot_token=token,
        task_tracker_url=tracker_url,
        task_tracker_api_key=tracker_key,
        allowed_chat_ids=allowed_chat_ids,
        allow_all_chats=allow_all_chats,
        state_path=Path(args.state_path),
        poll_timeout=int(os.getenv("TELEGRAM_POLL_TIMEOUT", "25")),
        poll_interval=float(os.getenv("TELEGRAM_POLL_INTERVAL", "2")),
        source_id=os.getenv("TELEGRAM_SOURCE_ID", default_source_id).strip() or default_source_id,
        source_name=os.getenv("TELEGRAM_SOURCE_NAME", "Telegram Bot").strip() or "Telegram Bot",
        health_report_interval=max(30.0, float(os.getenv("TELEGRAM_HEALTH_REPORT_INTERVAL", "300"))),
        max_retry_interval=max(1.0, float(os.getenv("TELEGRAM_MAX_RETRY_INTERVAL", "300"))),
        dry_run=args.dry_run,
        once=args.once,
    )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value or value == "change-me":
        raise SystemExit(f"{name} must be set")
    return value


def telegram_source_id(token: str) -> str:
    bot_id, separator, _ = token.partition(":")
    if not separator or not bot_id.isdigit():
        raise SystemExit("TELEGRAM_BOT_TOKEN must use the standard <bot_id>:<secret> format")
    return f"telegram:bot:{bot_id}"


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
        "allowed_updates": json.dumps(["message", "edited_message", "callback_query"]),
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
        raise SourceReadError(
            f"Telegram getUpdates request failed: {sanitize_error(exc, config)}",
            health_status="unavailable",
            error_code="telegram_request_failed",
        ) from None
    if not response.ok:
        detail = sanitize_error(response.text[:300], config)
        health_status = "failed"
        retry_after = None
        if response.status_code in {401, 403, 404}:
            health_status = "reauth_required"
        elif response.status_code == 429:
            health_status = "rate_limited"
            try:
                retry_after = float((response.json().get("parameters") or {}).get("retry_after") or 0) or None
            except (TypeError, ValueError):
                retry_after = None
        raise SourceReadError(
            f"Telegram getUpdates failed with HTTP {response.status_code}: {detail}",
            health_status=health_status,
            error_code=f"telegram_http_{response.status_code}",
            retry_after=retry_after,
        )
    try:
        payload = response.json()
    except ValueError as exc:
        raise SourceReadError(
            "Telegram getUpdates returned invalid JSON",
            health_status="failed",
            error_code="telegram_invalid_json",
        ) from exc
    if not payload.get("ok"):
        raise SourceReadError(
            f"Telegram getUpdates failed: {sanitize_error(payload, config)}",
            health_status="failed",
            error_code="telegram_api_error",
        )
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


def build_ingest_payload(
    message: dict[str, Any],
    tasks: list[dict[str, Any]],
    *,
    source_id: str = "telegram",
) -> dict[str, Any]:
    chat = message.get("chat", {})
    chat_id = chat.get("id")
    chat_title = chat.get("title") or chat.get("username") or chat.get("first_name") or chat_id
    text = message.get("text") or ""
    message_id = message.get("message_id")
    source_namespace = hashlib.sha256(source_id.encode("utf-8")).hexdigest()[:16]
    occurrences: dict[str, int] = {}
    normalized_tasks = []
    for task in tasks:
        identity = hashlib.sha256(
            json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        occurrence = occurrences.get(identity, 0)
        occurrences[identity] = occurrence + 1
        normalized_tasks.append(
            {
                **task,
                "description": f"{task['description']} Telegram message {message_id}.",
                "external_id": f"telegram:{source_namespace}:{chat_id}:{message_id}:{identity}:{occurrence}",
            }
        )
    return {
        "origin": "telegram",
        "source_name": f"Telegram {chat_title}",
        "source_url": build_source_url(message),
        "source_context": text,
        "tasks": normalized_tasks,
    }


def _tracker_request(config: AdapterConfig, method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    try:
        response = requests.request(
            method,
            f"{config.task_tracker_url}{path}",
            headers={
                "Authorization": f"Bearer {config.task_tracker_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        raise TrackerRequestError(f"Task Assistant request to {path} failed: {exc}") from None
    if not response.ok:
        raise TrackerRequestError(
            f"Task Assistant request to {path} failed with HTTP {response.status_code}: {response.text[:300]}"
        )
    try:
        return dict(response.json())
    except ValueError as exc:
        raise TrackerRequestError(f"Task Assistant request to {path} returned invalid JSON") from exc


def tracker_post(config: AdapterConfig, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _tracker_request(config, "POST", path, payload)


def tracker_patch(config: AdapterConfig, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _tracker_request(config, "PATCH", path, payload)


def post_to_task_assistant(config: AdapterConfig, payload: dict[str, Any]) -> dict[str, Any]:
    if config.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return {"created": [], "duplicates": []}
    result = tracker_post(config, "/api/agent/ingest/context", payload)
    print(f"created {len(result.get('created', []))} task(s) from {payload['source_name']}")
    return result


def source_item_identity(message: dict[str, Any]) -> tuple[str, str]:
    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = "\n".join(line.rstrip() for line in str(message.get("text") or "").strip().splitlines())
    fingerprint = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return f"telegram:{chat_id}:{message_id}", fingerprint


def should_process_item(config: AdapterConfig, source_item_id: str, content_fingerprint: str) -> bool:
    result = tracker_post(
        config,
        "/api/agent/ingestion/check",
        {
            "source_id": config.source_id,
            "items": [
                {
                    "source_item_id": source_item_id,
                    "content_fingerprint": content_fingerprint,
                }
            ],
        },
    )
    items = result.get("items") or []
    if len(items) != 1:
        raise TrackerRequestError("Task Assistant ingestion check returned an unexpected item count")
    return bool(items[0].get("should_process"))


def record_decision(
    config: AdapterConfig,
    *,
    source_item_id: str,
    content_fingerprint: str,
    decision: str,
    reason: str,
    task_id: int | None = None,
) -> None:
    tracker_post(
        config,
        "/api/agent/ingestion/decisions",
        {
            "source_id": config.source_id,
            "source_item_id": source_item_id,
            "content_fingerprint": content_fingerprint,
            "decision": decision,
            "reason": reason,
            "task_id": task_id,
            "decided_by": "adapter",
        },
    )


def report_source_health(
    config: AdapterConfig,
    *,
    status: str,
    metrics: RunMetrics,
    error_code: str | None = None,
    error_message: str | None = None,
) -> None:
    tracker_post(
        config,
        "/api/agent/sources/report",
        {
            "source_id": config.source_id,
            "source_name": config.source_name,
            "adapter_type": "telegram_polling",
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            **metrics.as_dict(),
        },
    )


def telegram_post(config: AdapterConfig, method: str, payload: dict[str, Any]) -> bool:
    """Call the Telegram Bot API; sanitized failure never crashes the loop."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{config.telegram_bot_token}/{method}",
            json=payload,
            timeout=20,
        )
    except requests.RequestException as exc:
        print(f"telegram {method} failed: {sanitize_error(exc, config)}", file=sys.stderr)
        return False
    if not response.ok:
        detail = sanitize_error(response.text[:300], config)
        print(f"telegram {method} failed with HTTP {response.status_code}: {detail}", file=sys.stderr)
        return False
    return True


def parse_callback_action(data: str) -> tuple[str, int] | None:
    match = CALLBACK_ACTION_RE.match(data or "")
    if not match:
        return None
    return match.group(1), int(match.group(2))


def handle_callback(config: AdapterConfig, callback: dict[str, Any]) -> None:
    message = callback.get("message") or {}
    if not message or not is_allowed_chat(config, message):
        return
    lang = assistant_lang()
    parsed = parse_callback_action(str(callback.get("data") or ""))
    answer: dict[str, Any] = {"callback_query_id": callback.get("id")}
    if parsed is None:
        telegram_post(config, "answerCallbackQuery", {**answer, "text": CALLBACK_MESSAGES["unsupported"][lang]})
        return
    action, task_id = parsed
    payload, confirmations = CALLBACK_ACTIONS[action]
    confirmation = confirmations[lang]
    if config.dry_run:
        print(f"dry-run: would PATCH /api/tasks/{task_id} with {json.dumps(payload)}")
        return
    try:
        tracker_patch(config, f"/api/tasks/{task_id}", payload)
    except TrackerRequestError as exc:
        print(f"callback for task {task_id} failed: {exc}", file=sys.stderr)
        telegram_post(
            config,
            "answerCallbackQuery",
            {**answer, "text": CALLBACK_MESSAGES["failed"][lang].format(task_id=task_id), "show_alert": True},
        )
        return
    telegram_post(config, "answerCallbackQuery", {**answer, "text": f"#{task_id}: {confirmation}"})
    original_text = str(message.get("text") or "")
    if original_text and message.get("message_id"):
        telegram_post(
            config,
            "editMessageText",
            {
                "chat_id": message["chat"]["id"],
                "message_id": message["message_id"],
                "text": f"{original_text}\n\n{confirmation}",
            },
        )


def process_update(config: AdapterConfig, update: dict[str, Any]) -> RunMetrics:
    metrics = RunMetrics()
    callback = update.get("callback_query")
    if callback is not None:
        handle_callback(config, callback)
        return metrics
    message = update.get("message") or update.get("edited_message") or {}
    if not message or not is_allowed_chat(config, message):
        return metrics
    metrics.items_checked = 1
    text = message.get("text") or ""
    source_item_id, content_fingerprint = source_item_identity(message)
    if not config.dry_run and not should_process_item(config, source_item_id, content_fingerprint):
        metrics.suppressed = 1
        return metrics
    tasks = parse_tasks_from_text(text)
    metrics.candidates = len(tasks)
    if config.dry_run:
        if tasks:
            post_to_task_assistant(config, build_ingest_payload(message, tasks, source_id=config.source_id))
        return metrics
    if not tasks:
        record_decision(
            config,
            source_item_id=source_item_id,
            content_fingerprint=content_fingerprint,
            decision="ignored",
            reason="No supported task prefix produced an actionable task",
        )
        metrics.ignored = 1
        return metrics
    result = post_to_task_assistant(config, build_ingest_payload(message, tasks, source_id=config.source_id))
    created = list(result.get("created") or [])
    duplicates = list(result.get("duplicates") or [])
    metrics.created = len(created)
    metrics.duplicates = len(duplicates)
    decision = "created" if created else "duplicate"
    task_records = created or duplicates
    record_decision(
        config,
        source_item_id=source_item_id,
        content_fingerprint=content_fingerprint,
        decision=decision,
        reason=f"Normalized {len(tasks)} candidate(s): {len(created)} created, {len(duplicates)} duplicate",
        task_id=task_records[0].get("id") if task_records else None,
    )
    return metrics


def main() -> int:
    config = load_config()
    offset = read_offset(config.state_path)
    pending_metrics = RunMetrics()
    last_health_report_at = 0.0
    retry_delay = max(1.0, config.poll_interval)
    while True:
        try:
            updates = get_updates(config, offset)
        except SourceReadError as exc:
            if not config.dry_run:
                try:
                    report_source_health(
                        config,
                        status=exc.health_status,
                        metrics=pending_metrics,
                        error_code=exc.error_code,
                        error_message=sanitize_error(exc, config)[:2000],
                    )
                except TrackerRequestError as report_error:
                    print(f"health report failed: {report_error}", file=sys.stderr)
            if config.once or exc.health_status not in {"unavailable", "rate_limited", "failed"}:
                raise
            delay = (
                max(1.0, exc.retry_after)
                if exc.retry_after is not None
                else min(config.max_retry_interval, max(1.0, retry_delay))
            )
            time.sleep(delay)
            retry_delay = min(config.max_retry_interval, retry_delay * 2)
            continue
        retry_delay = max(1.0, config.poll_interval)
        for update in updates:
            pending_metrics.add(process_update(config, update))
            offset = int(update["update_id"]) + 1
            if not config.dry_run:
                write_offset(config.state_path, offset)
        now = time.monotonic()
        if not config.dry_run and (config.once or now - last_health_report_at >= config.health_report_interval):
            report_source_health(config, status="healthy", metrics=pending_metrics)
            pending_metrics = RunMetrics()
            last_health_report_at = now
        if config.once:
            return 0
        time.sleep(config.poll_interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except RuntimeError as exc:
        print(f"adapter error: {exc}", file=sys.stderr)
        raise SystemExit(1) from None
