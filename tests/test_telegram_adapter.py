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


# ---------- review-request callback buttons ----------


def test_parse_callback_action():
    assert telegram_adapter.parse_callback_action("task:done:12") == ("done", 12)
    assert telegram_adapter.parse_callback_action("task:rework:3") == ("rework", 3)
    assert telegram_adapter.parse_callback_action("task:block:7") == ("block", 7)
    assert telegram_adapter.parse_callback_action("task:delete:7") is None
    assert telegram_adapter.parse_callback_action("garbage") is None
    assert telegram_adapter.parse_callback_action("") is None


def _callback(action="done", task_id=12, chat_id=1):
    return {
        "id": "cb-1",
        "data": f"task:{action}:{task_id}",
        "message": {
            "message_id": 99,
            "chat": {"id": chat_id},
            "text": "👀 Review needed: #12 fix the export",
        },
    }


def test_callback_done_patches_task_and_confirms(monkeypatch):
    config = _config(dry_run=False)
    patched = []
    telegram_calls = []
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_patch",
        lambda _config, path, payload: patched.append((path, payload)) or {},
    )
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda _config, method, payload: telegram_calls.append((method, payload)) or True,
    )
    metrics = process_update(config, {"update_id": 1, "callback_query": _callback("done")})
    assert patched == [("/api/tasks/12", {"status": "done"})]
    assert metrics.items_checked == 0
    methods = [method for method, _ in telegram_calls]
    assert methods == ["answerCallbackQuery", "editMessageText"]
    edit_payload = telegram_calls[1][1]
    assert edit_payload["message_id"] == 99
    assert "✅" in edit_payload["text"]


def test_callback_rework_and_block_map_to_statuses(monkeypatch):
    config = _config(dry_run=False)
    patched = []
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_patch",
        lambda _config, path, payload: patched.append((path, payload)) or {},
    )
    monkeypatch.setattr(telegram_adapter, "telegram_post", lambda *_args: True)
    process_update(config, {"update_id": 1, "callback_query": _callback("rework", task_id=3)})
    process_update(config, {"update_id": 2, "callback_query": _callback("block", task_id=7)})
    assert patched == [
        ("/api/tasks/3", {"status": "in_progress"}),
        ("/api/tasks/7", {"status": "blocked"}),
    ]


def test_callback_from_disallowed_chat_is_ignored(monkeypatch):
    config = _config(dry_run=False)
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_patch",
        lambda *_args: pytest.fail("must not touch the tracker"),
    )
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda *_args: pytest.fail("must not answer"),
    )
    process_update(config, {"update_id": 1, "callback_query": _callback(chat_id=666)})


def test_callback_with_unknown_data_answers_without_patching(monkeypatch):
    config = _config(dry_run=False)
    answers = []
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_patch",
        lambda *_args: pytest.fail("must not touch the tracker"),
    )
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda _config, method, payload: answers.append((method, payload)) or True,
    )
    callback = _callback()
    callback["data"] = "task:explode:12"
    process_update(config, {"update_id": 1, "callback_query": callback})
    assert answers[0][0] == "answerCallbackQuery"
    assert "Unsupported" in answers[0][1]["text"]


def test_callback_tracker_failure_alerts_without_editing(monkeypatch):
    config = _config(dry_run=False)
    telegram_calls = []

    def failing_patch(*_args):
        raise telegram_adapter.TrackerRequestError("HTTP 404")

    monkeypatch.setattr(telegram_adapter, "tracker_patch", failing_patch)
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda _config, method, payload: telegram_calls.append((method, payload)) or True,
    )
    process_update(config, {"update_id": 1, "callback_query": _callback()})
    assert [method for method, _ in telegram_calls] == ["answerCallbackQuery"]
    assert telegram_calls[0][1]["show_alert"] is True


def test_callback_confirmation_localized_to_russian(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LANG", "ru")
    config = _config(dry_run=False)
    telegram_calls = []
    monkeypatch.setattr(telegram_adapter, "tracker_patch", lambda *_args: {})
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda _config, method, payload: telegram_calls.append((method, payload)) or True,
    )
    process_update(config, {"update_id": 1, "callback_query": _callback("done")})
    answer = telegram_calls[0][1]
    assert "Принято" in answer["text"]
    edit = telegram_calls[1][1]
    assert "задача закрыта" in edit["text"]


# ---------- reply commands (reply to a bot message = act on that task) ----------


def _bot_reply_message(reply_text="закрыто, пароль сменили девайсы отключили", replied="⏰ Напоминание: #267 Secure account\nP1", chat_id=1, author_id=123456):
    return {
        "message_id": 500,
        "chat": {"id": chat_id},
        "text": reply_text,
        "reply_to_message": {"message_id": 400, "from": {"id": author_id, "is_bot": True}, "text": replied},
    }


def test_parse_reply_command_targets_bot_messages_only():
    parse = telegram_adapter.parse_reply_command
    assert parse(_bot_reply_message(), 123456) == (267, "закрыто, пароль сменили девайсы отключили")
    assert parse(_bot_reply_message(author_id=999), 123456) is None
    assert parse(_bot_reply_message(replied="no task number here"), 123456) is None
    assert parse({"text": "no reply", "chat": {"id": 1}}, 123456) is None


def test_classify_reply_action_first_word():
    classify = telegram_adapter.classify_reply_action
    assert classify("закрыто, пароль сменили девайсы отключили") == "done"
    assert classify("Готово!") == "done"
    assert classify("done") == "done"
    assert classify("Доработай: добавь тест") == "rework"
    assert classify("блок — жду доступ") == "block"
    assert classify("пароль сменили, наблюдаю") == "comment"


def test_reply_command_appends_note_and_closes(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LANG", "ru")
    config = _config(dry_run=False, bot_id=123456)
    patched, sent = [], []
    monkeypatch.setattr(
        telegram_adapter, "tracker_get", lambda _c, path: {"id": 267, "description": "Старое описание"}
    )
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_patch",
        lambda _c, path, payload: patched.append((path, payload)) or {},
    )
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_post",
        lambda _c, method, payload: sent.append((method, payload)) or True,
    )
    metrics = process_update(config, {"update_id": 1, "message": _bot_reply_message()})
    assert metrics.items_checked == 0  # command traffic, not ingest
    path, payload = patched[0]
    assert path == "/api/tasks/267"
    assert payload["status"] == "done"
    assert payload["description"].startswith("Старое описание\n\n— ")
    assert "закрыто, пароль сменили" in payload["description"]
    assert "(Telegram)" in payload["description"]
    assert sent[0][0] == "sendMessage"
    assert "закрыта" in sent[0][1]["text"]
    assert sent[0][1]["reply_to_message_id"] == 500


def test_reply_comment_keeps_status(monkeypatch):
    config = _config(dry_run=False, bot_id=123456)
    patched = []
    monkeypatch.setattr(telegram_adapter, "tracker_get", lambda _c, path: {"id": 267, "description": None})
    monkeypatch.setattr(
        telegram_adapter, "tracker_patch", lambda _c, path, payload: patched.append(payload) or {}
    )
    monkeypatch.setattr(telegram_adapter, "telegram_post", lambda *_a: True)
    message = _bot_reply_message(reply_text="пока жду ответа поддержки")
    process_update(config, {"update_id": 1, "message": message})
    assert "status" not in patched[0]
    assert patched[0]["description"].startswith("— ")


def test_reply_to_missing_task_answers_not_found(monkeypatch):
    config = _config(dry_run=False, bot_id=123456)
    sent = []

    def failing_get(*_args):
        raise telegram_adapter.TrackerRequestError("HTTP 404: not found")

    monkeypatch.setattr(telegram_adapter, "tracker_get", failing_get)
    monkeypatch.setattr(
        telegram_adapter, "tracker_patch", lambda *_a: pytest.fail("must not patch")
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    process_update(config, {"update_id": 1, "message": _bot_reply_message()})
    assert "не найдена" in sent[0]["text"] or "not found" in sent[0]["text"]


def test_voice_message_without_stt_answers_honestly_and_skips_ledger(monkeypatch):
    import speech_to_text

    config = _config(dry_run=False, bot_id=123456)
    sent = []
    monkeypatch.setattr(speech_to_text, "available", lambda: (False, "ffmpeg not found"))
    monkeypatch.setattr(
        telegram_adapter,
        "should_process_item",
        lambda *_a: pytest.fail("voice must not hit the ledger"),
    )
    monkeypatch.setattr(
        telegram_adapter,
        "telegram_file_path",
        lambda *_a: pytest.fail("must not download when STT is unavailable"),
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    message = {"message_id": 7, "chat": {"id": 1}, "voice": {"file_id": "x", "duration": 3}}
    metrics = process_update(config, {"update_id": 1, "message": message})
    assert metrics.items_checked == 0
    assert sent and "ffmpeg not found" in sent[0]["text"]
    assert "недоступна" in sent[0]["text"] or "not available" in sent[0]["text"]


# ---------- voice messages (local STT) ----------


def _voice_message(duration=5, reply_to=None):
    message = {
        "message_id": 600,
        "chat": {"id": 1},
        "voice": {"file_id": "voice-file-id", "duration": duration},
    }
    if reply_to is not None:
        message["reply_to_message"] = reply_to
    return message


def _wire_voice(monkeypatch, transcript="Добавь задачу проверить оплату в Google Play"):
    import speech_to_text

    monkeypatch.setattr(speech_to_text, "available", lambda: (True, ""))
    monkeypatch.setattr(speech_to_text, "transcribe", lambda path, lang=None: transcript)
    monkeypatch.setattr(telegram_adapter, "telegram_file_path", lambda _c, _f: "voice/file_1.oga")
    monkeypatch.setattr(
        telegram_adapter, "download_telegram_file", lambda _c, _p, dest: dest.write_bytes(b"x") or True
    )


def test_voice_message_creates_task_with_transcript(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LANG", "ru")
    config = _config(dry_run=False, bot_id=123456)
    created, sent = [], []
    _wire_voice(monkeypatch)
    monkeypatch.setattr(
        telegram_adapter,
        "tracker_post",
        lambda _c, path, payload: created.append((path, payload)) or {"id": 301},
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    metrics = process_update(config, {"update_id": 1, "message": _voice_message()})
    assert metrics.items_checked == 0
    path, payload = created[0]
    assert path == "/api/agent/tasks"
    assert payload["title"].startswith("Добавь задачу проверить оплату")
    assert payload["origin"] == "telegram"
    assert "(Telegram, голосовое)" in payload["description"]
    assert "Создал #301" in sent[0]["text"]
    assert "🎙" in sent[0]["text"]


def test_voice_reply_acts_on_task(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LANG", "ru")
    config = _config(dry_run=False, bot_id=123456)
    patched, sent = [], []
    _wire_voice(monkeypatch, transcript="Закрыто, всё проверил")
    monkeypatch.setattr(telegram_adapter, "tracker_get", lambda _c, path: {"id": 267, "description": ""})
    monkeypatch.setattr(
        telegram_adapter, "tracker_patch", lambda _c, path, payload: patched.append((path, payload)) or {}
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    reply_to = {"message_id": 400, "from": {"id": 123456, "is_bot": True}, "text": "⏰ Напоминание: #267 Secure"}
    process_update(config, {"update_id": 1, "message": _voice_message(reply_to=reply_to)})
    assert patched[0][0] == "/api/tasks/267"
    assert patched[0][1]["status"] == "done"
    assert "🎙 «Закрыто, всё проверил»" in sent[0]["text"]
    assert "закрыта" in sent[0]["text"]


def test_voice_transcription_failure_answers_honestly(monkeypatch):
    import speech_to_text

    config = _config(dry_run=False, bot_id=123456)
    sent = []
    monkeypatch.setattr(speech_to_text, "available", lambda: (True, ""))
    monkeypatch.setattr(speech_to_text, "transcribe", lambda path, lang=None: None)
    monkeypatch.setattr(telegram_adapter, "telegram_file_path", lambda _c, _f: "voice/file_1.oga")
    monkeypatch.setattr(
        telegram_adapter, "download_telegram_file", lambda _c, _p, dest: dest.write_bytes(b"x") or True
    )
    monkeypatch.setattr(
        telegram_adapter, "tracker_post", lambda *_a: pytest.fail("must not create tasks")
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    process_update(config, {"update_id": 1, "message": _voice_message()})
    assert "Не смог расшифровать" in sent[0]["text"] or "Could not transcribe" in sent[0]["text"]


def test_voice_too_long_is_refused(monkeypatch):
    import speech_to_text

    config = _config(dry_run=False, bot_id=123456)
    sent = []
    monkeypatch.setattr(
        speech_to_text, "available", lambda: pytest.fail("must not reach STT")
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_file_path", lambda *_a: pytest.fail("must not download")
    )
    monkeypatch.setattr(
        telegram_adapter, "telegram_post", lambda _c, m, payload: sent.append(payload) or True
    )
    process_update(config, {"update_id": 1, "message": _voice_message(duration=999)})
    assert "слишком длинное" in sent[0]["text"] or "too long" in sent[0]["text"]


def test_voice_task_title_cuts_at_word_boundary():
    long = "слово " * 40
    title = telegram_adapter.voice_task_title(long.strip())
    assert len(title) <= 121
    assert title.endswith("…")
    assert not title[:-1].endswith(" ")
