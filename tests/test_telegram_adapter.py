import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

from telegram_polling_adapter import (  # noqa: E402
    AdapterConfig,
    build_ingest_payload,
    parse_tasks_from_text,
    sanitize_error,
)


def _config(**overrides) -> AdapterConfig:
    values = {
        "telegram_bot_token": "123456:SECRET-TOKEN",
        "task_tracker_url": "http://127.0.0.1:8000",
        "task_tracker_api_key": "key",
        "allowed_chat_ids": {1},
        "allow_all_chats": False,
        "state_path": Path(".adapter_state/test.json"),
        "poll_timeout": 1,
        "poll_interval": 0.1,
        "dry_run": True,
        "once": True,
    }
    values.update(overrides)
    return AdapterConfig(**values)


def test_parse_prefixes_map_to_assignee_and_status():
    tasks = parse_tasks_from_text(
        "codex: build the export\n"
        "me: call the bank\n"
        "review: check the deck\n"
        "blocked: waiting on api keys\n"
        "todo: sort inbox\n"
        "random chatter line"
    )
    assert [(task["assignee"], task["status"]) for task in tasks] == [
        ("codex", "backlog"),
        ("me", "backlog"),
        ("me", "waiting_review"),
        ("unassigned", "blocked"),
        ("unassigned", "backlog"),
    ]


def test_parse_priority_token_and_due_date():
    (task,) = parse_tasks_from_text("p1 me: call bank due:2026-07-10")
    assert task["priority"] == 1
    assert task["title"] == "call bank"
    assert task["due_at"] == "2026-07-10T18:00:00Z"


def test_parse_skips_empty_titles_and_bad_due():
    assert parse_tasks_from_text("todo: due:2026-07-10") == []
    (task,) = parse_tasks_from_text("todo: fix due:2026-99-99")
    assert "due_at" not in task


def test_build_ingest_payload_sets_external_ids():
    message = {
        "message_id": 42,
        "chat": {"id": -100123, "title": "Ops"},
        "text": "codex: a\ncodex: b",
    }
    tasks = parse_tasks_from_text(message["text"])
    payload = build_ingest_payload(message, tasks)
    assert payload["origin"] == "telegram"
    assert [task["external_id"] for task in payload["tasks"]] == [
        "telegram:-100123:42:0",
        "telegram:-100123:42:1",
    ]


def test_sanitize_error_hides_bot_token():
    config = _config()
    message = "request to https://api.telegram.org/bot123456:SECRET-TOKEN/getUpdates failed: 123456:SECRET-TOKEN"
    sanitized = sanitize_error(message, config)
    assert "SECRET-TOKEN" not in sanitized
    assert "<redacted>" in sanitized
