import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

import telegram_polling_adapter as telegram_adapter  # noqa: E402
from telegram_polling_adapter import (  # noqa: E402
    AdapterConfig,
    build_ingest_payload,
    parse_tasks_from_text,
    process_update,
    report_source_health,
    sanitize_error,
    source_item_identity,
    telegram_source_id,
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
        "source_id": "telegram:test",
        "source_name": "Test Telegram",
        "health_report_interval": 300,
        "max_retry_interval": 300,
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
    external_ids = [task["external_id"] for task in payload["tasks"]]
    assert len(set(external_ids)) == 2
    assert all(external_id.startswith("telegram:") for external_id in external_ids)
    assert all(":-100123:42:" in external_id for external_id in external_ids)

    reordered = build_ingest_payload(message, list(reversed(tasks)))
    by_title = {task["title"]: task["external_id"] for task in payload["tasks"]}
    reordered_by_title = {task["title"]: task["external_id"] for task in reordered["tasks"]}
    assert reordered_by_title == by_title

    other_bot = build_ingest_payload(message, tasks, source_id="telegram:another-bot")
    assert {task["external_id"] for task in other_bot["tasks"]}.isdisjoint(external_ids)


def test_sanitize_error_hides_bot_token():
    config = _config()
    message = "request to https://api.telegram.org/bot123456:SECRET-TOKEN/getUpdates failed: 123456:SECRET-TOKEN"
    sanitized = sanitize_error(message, config)
    assert "SECRET-TOKEN" not in sanitized
    assert "<redacted>" in sanitized


def test_default_source_id_survives_secret_rotation():
    assert telegram_source_id("123456:first-secret") == "telegram:bot:123456"
    assert telegram_source_id("123456:rotated-secret") == "telegram:bot:123456"
    with pytest.raises(SystemExit):
        telegram_source_id("not-a-telegram-token")


def test_source_item_identity_changes_only_when_content_changes():
    message = {"message_id": 42, "chat": {"id": 1}, "text": "todo: first  \nline"}
    item_id, fingerprint = source_item_identity(message)
    assert item_id == "telegram:1:42"
    assert source_item_identity({**message, "text": "todo: first\nline"}) == (item_id, fingerprint)
    assert source_item_identity({**message, "text": "todo: changed"}) != (item_id, fingerprint)


def test_process_update_records_ignored_non_actionable_item(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_adapter, "should_process_item", lambda *_: True)
    monkeypatch.setattr(telegram_adapter, "record_decision", lambda *args, **kwargs: captured.append(kwargs))
    metrics = process_update(
        _config(dry_run=False),
        {"update_id": 7, "message": {"message_id": 42, "chat": {"id": 1}, "text": "just chatting"}},
    )
    assert metrics.items_checked == 1
    assert metrics.ignored == 1
    assert captured[0]["decision"] == "ignored"


def test_process_update_suppresses_known_item(monkeypatch):
    monkeypatch.setattr(telegram_adapter, "should_process_item", lambda *_: False)
    metrics = process_update(
        _config(dry_run=False),
        {"update_id": 7, "message": {"message_id": 42, "chat": {"id": 1}, "text": "todo: repeat"}},
    )
    assert metrics.candidates == 0
    assert metrics.suppressed == 1
    assert metrics.created == 0


def test_process_update_accepts_edited_message(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_adapter, "should_process_item", lambda *_: True)
    monkeypatch.setattr(
        telegram_adapter,
        "post_to_task_assistant",
        lambda *_: {"created": [{"id": 10}], "duplicates": []},
    )
    monkeypatch.setattr(telegram_adapter, "record_decision", lambda *args, **kwargs: captured.append(kwargs))
    metrics = process_update(
        _config(dry_run=False),
        {
            "update_id": 8,
            "edited_message": {"message_id": 42, "chat": {"id": 1}, "text": "todo: changed request"},
        },
    )
    assert metrics.created == 1
    assert captured[0]["source_item_id"] == "telegram:1:42"


def test_edited_message_adds_only_new_content_stable_tasks_end_to_end(client, api_headers, monkeypatch):
    config = _config(dry_run=False)

    def local_post(_config, path, payload):
        response = client.post(path, json=payload, headers=api_headers)
        assert response.status_code == 200, response.text
        return response.json()

    monkeypatch.setattr(telegram_adapter, "tracker_post", local_post)

    original = {
        "message_id": 42,
        "chat": {"id": 1, "title": "Test"},
        "text": "todo: first request\ntodo: second request",
    }
    first = process_update(config, {"update_id": 7, "message": original})
    assert first.created == 2

    edited = {**original, "text": "todo: inserted request\ntodo: second request\ntodo: first request"}
    second = process_update(config, {"update_id": 8, "edited_message": edited})
    assert second.created == 1
    assert second.duplicates == 2

    tasks = client.get("/api/tasks?include_done=true", headers=api_headers).json()
    assert len(tasks) == 3
    assert {task["title"] for task in tasks} == {"first request", "second request", "inserted request"}
    assert len({task["external_id"] for task in tasks}) == 3

    first_task = next(task for task in tasks if task["title"] == "first request")
    moved = client.patch(
        f"/api/tasks/{first_task['id']}",
        json={"status": "in_progress"},
        headers=api_headers,
    )
    assert moved.status_code == 200

    removed = {**original, "text": "todo: second request\ntodo: inserted request"}
    third = process_update(config, {"update_id": 9, "edited_message": removed})
    assert third.created == 0
    assert third.duplicates == 2
    after_removal = client.get("/api/tasks?include_done=true", headers=api_headers).json()
    preserved = next(task for task in after_removal if task["title"] == "first request")
    assert preserved["status"] == "in_progress"

    replay = process_update(config, {"update_id": 10, "edited_message": removed})
    assert replay.suppressed == 1
    assert len(client.get("/api/tasks?include_done=true", headers=api_headers).json()) == 3


def test_process_update_records_created_task(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_adapter, "should_process_item", lambda *_: True)
    monkeypatch.setattr(
        telegram_adapter,
        "post_to_task_assistant",
        lambda *_: {"created": [{"id": 9}], "duplicates": []},
    )
    monkeypatch.setattr(telegram_adapter, "record_decision", lambda *args, **kwargs: captured.append(kwargs))
    metrics = process_update(
        _config(dry_run=False),
        {"update_id": 7, "message": {"message_id": 42, "chat": {"id": 1}, "text": "todo: ship it"}},
    )
    assert metrics.created == 1
    assert captured[0]["decision"] == "created"
    assert captured[0]["task_id"] == 9


def test_health_report_uses_normalized_contract(monkeypatch):
    captured = []
    monkeypatch.setattr(telegram_adapter, "tracker_post", lambda config, path, payload: captured.append((path, payload)))
    report_source_health(
        _config(dry_run=False),
        status="reauth_required",
        metrics=telegram_adapter.RunMetrics(items_checked=3, ignored=2),
        error_code="telegram_http_401",
        error_message="Unauthorized",
    )
    path, payload = captured[0]
    assert path == "/api/agent/sources/report"
    assert payload["source_id"] == "telegram:test"
    assert payload["status"] == "reauth_required"
    assert payload["items_checked"] == 3
    assert payload["ignored"] == 2


def test_long_running_adapter_retries_transient_source_failure(monkeypatch):
    config = _config(dry_run=False, once=False, poll_interval=2, max_retry_interval=60)
    transient = telegram_adapter.SourceReadError(
        "rate limited",
        health_status="rate_limited",
        error_code="telegram_http_429",
        retry_after=90,
    )
    outcomes = iter([transient, [], KeyboardInterrupt()])

    def fake_get_updates(_config, _offset):
        outcome = next(outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    reports = []
    sleeps = []
    monkeypatch.setattr(telegram_adapter, "load_config", lambda: config)
    monkeypatch.setattr(telegram_adapter, "read_offset", lambda _path: None)
    monkeypatch.setattr(telegram_adapter, "get_updates", fake_get_updates)
    monkeypatch.setattr(
        telegram_adapter,
        "report_source_health",
        lambda _config, **kwargs: reports.append(kwargs),
    )
    monkeypatch.setattr(telegram_adapter.time, "sleep", lambda delay: sleeps.append(delay))
    monkeypatch.setattr(telegram_adapter.time, "monotonic", lambda: 0.0)

    with pytest.raises(KeyboardInterrupt):
        telegram_adapter.main()
    assert reports[0]["status"] == "rate_limited"
    assert sleeps[:2] == [90, 2]
