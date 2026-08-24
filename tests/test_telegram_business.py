import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "adapters"))

import telegram_business  # noqa: E402
import telegram_polling_adapter as telegram_adapter  # noqa: E402
from telegram_business import BusinessConfig  # noqa: E402
from telegram_polling_adapter import AdapterConfig  # noqa: E402


def _business_config(**overrides) -> BusinessConfig:
    values = {
        "notify_chat_id": "99",
        "state_path": Path(".adapter_state/test_business.json"),
        "analyze_lull": 60.0,
        "analyze_max_wait": 600.0,
        "context_messages": 5,
        "buffer_limit": 6,
        "text_limit": 300,
        "max_candidates": 3,
        "claude_bin": None,
        "model": None,
        "analyze_timeout": 30.0,
        "voice_max_seconds": 60,
        "min_confidence": 0.6,
        "ignore_chat_ids": frozenset(),
    }
    values.update(overrides)
    return BusinessConfig(**values)


def _config(**overrides) -> AdapterConfig:
    values = {
        "telegram_bot_token": "123456:SECRET-TOKEN",
        "task_tracker_url": "http://127.0.0.1:8000",
        "task_tracker_api_key": "key",
        "allowed_chat_ids": {99},
        "allow_all_chats": False,
        "state_path": Path(".adapter_state/test.json"),
        "poll_timeout": 1,
        "poll_interval": 0.1,
        "source_id": "telegram:test",
        "source_name": "Test Telegram",
        "health_report_interval": 300,
        "max_retry_interval": 300,
        "dry_run": False,
        "once": True,
        "business": _business_config(),
    }
    values.update(overrides)
    return AdapterConfig(**values)


def _fresh_state() -> dict:
    return {"version": 1, "connections": {}, "chats": {}, "candidates": {}}


def _business_message(chat_id: int, message_id: int, text: str, *, sender_id: int | None = None) -> dict:
    return {
        "business_connection_id": "conn-1",
        "message_id": message_id,
        "chat": {"id": chat_id, "type": "private", "first_name": "Ivan", "last_name": "Petrov"},
        "from": {"id": sender_id if sender_id is not None else chat_id},
        "text": text,
    }


@pytest.fixture(autouse=True)
def _clean_lang(monkeypatch):
    monkeypatch.delenv("ASSISTANT_LANG", raising=False)


@pytest.fixture
def telegram_calls(monkeypatch):
    calls: list[tuple[str, dict]] = []

    def fake_call(config, method, payload, **kwargs):
        calls.append((method, payload))
        if method == "sendMessage":
            return {"message_id": 555, "chat": {"id": 99}}
        return {"value": True}

    monkeypatch.setattr(telegram_business, "_telegram_call", fake_call)
    return calls


class _TrackerCalls(list):
    """Recorded (path, payload) pairs plus per-path canned responses."""

    def __init__(self):
        super().__init__()
        self.responses = {
            "/api/agent/ingestion/check": {"items": [{"should_process": True}]},
            "/api/agent/ingestion/decisions": {},
            "/api/agent/ingest/context": {"created": [{"id": 42}], "duplicates": []},
        }


@pytest.fixture
def tracker_calls(monkeypatch):
    calls = _TrackerCalls()

    def fake_call(config, method, path, payload):
        calls.append((path, payload))
        return dict(calls.responses[path])

    monkeypatch.setattr(telegram_business, "_tracker_call", fake_call)
    return calls


def test_business_config_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BUSINESS_TASKS", raising=False)
    assert telegram_business.load_business_config() is None


def test_business_config_requires_notify_chat(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BUSINESS_TASKS", "1")
    monkeypatch.delenv("TELEGRAM_NOTIFY_CHAT_ID", raising=False)
    with pytest.raises(SystemExit):
        telegram_business.load_business_config()


def test_business_config_reads_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BUSINESS_TASKS", "true")
    monkeypatch.setenv("TELEGRAM_NOTIFY_CHAT_ID", "77")
    monkeypatch.setenv("BUSINESS_IGNORE_CHAT_IDS", "5, 6")
    config = telegram_business.load_business_config()
    assert config is not None
    assert config.notify_chat_id == "77"
    assert config.ignore_chat_ids == frozenset({5, 6})


def test_is_business_update():
    assert telegram_business.is_business_update({"business_message": {}})
    assert telegram_business.is_business_update({"deleted_business_messages": {}})
    assert not telegram_business.is_business_update({"message": {}})


def test_buffer_records_direction():
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 1, "привет")})
    telegram_business.handle_business_update(
        config, state, {"business_message": _business_message(777, 2, "ок, сделаю", sender_id=111)}
    )
    chat = state["chats"]["777"]
    assert chat["title"] == "Ivan Petrov"
    assert [(m["id"], m["out"]) for m in chat["messages"]] == [(1, False), (2, True)]
    assert telegram_business.consume_dirty(state) is True


def test_buffer_ignores_configured_chats_and_groups():
    config = _config(business=_business_config(ignore_chat_ids=frozenset({777})))
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 1, "hi")})
    group = _business_message(888, 1, "hi")
    group["chat"]["type"] = "group"
    telegram_business.handle_business_update(config, state, {"business_message": group})
    assert state["chats"] == {}


def test_edited_message_respects_watermark():
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 5, "old")})
    state["chats"]["777"]["analyzed_through"] = 5
    telegram_business.handle_business_update(
        config, state, {"edited_business_message": _business_message(777, 5, "new")}
    )
    assert state["chats"]["777"]["messages"][0]["text"] == "old"
    telegram_business.handle_business_update(
        config, state, {"edited_business_message": _business_message(777, 6, "fresh")}
    )
    assert [m["id"] for m in state["chats"]["777"]["messages"]] == [5, 6]


def test_buffer_trims_to_limit():
    config = _config()
    state = _fresh_state()
    for message_id in range(1, 9):
        telegram_business.handle_business_update(
            config, state, {"business_message": _business_message(777, message_id, f"msg {message_id}")}
        )
    messages = state["chats"]["777"]["messages"]
    assert len(messages) == 6
    assert messages[0]["id"] == 3


def test_deleted_messages_dropped():
    config = _config()
    state = _fresh_state()
    for message_id in (1, 2, 3):
        telegram_business.handle_business_update(
            config, state, {"business_message": _business_message(777, message_id, f"msg {message_id}")}
        )
    telegram_business.handle_business_update(
        config,
        state,
        {"deleted_business_messages": {"chat": {"id": 777, "type": "private"}, "message_ids": [1, 3]}},
    )
    assert [m["id"] for m in state["chats"]["777"]["messages"]] == [2]


def test_media_markers_without_stt():
    config = _config()
    state = _fresh_state()
    photo = _business_message(777, 1, "")
    photo.pop("text")
    photo["photo"] = [{"file_id": "x"}]
    photo["caption"] = "чек за оборудование"
    telegram_business.handle_business_update(config, state, {"business_message": photo})
    assert state["chats"]["777"]["messages"][0]["text"] == "[photo] чек за оборудование"


def test_connection_notification_warns_about_rights(telegram_calls):
    config = _config()
    state = _fresh_state()
    connection = {
        "id": "conn-1",
        "user": {"id": 111, "first_name": "Phil"},
        "is_enabled": True,
        "rights": {"can_reply": True, "can_read_messages": True, "can_delete_all_messages": False},
    }
    telegram_business.handle_business_update(config, state, {"business_connection": connection})
    assert state["connections"]["conn-1"]["is_enabled"] is True
    assert state["connections"]["conn-1"]["extra_rights"] == ["can_reply"]
    assert len(telegram_calls) == 1
    method, payload = telegram_calls[0]
    assert method == "sendMessage"
    assert payload["chat_id"] == "99"
    assert "can_reply" in payload["text"]


def test_parse_candidates_validates_everything():
    raw = (
        "```json\n"
        + json.dumps(
            {
                "candidates": [
                    {"source_message_id": 5, "kind": "request", "title": "Отправить отчёт", "confidence": 0.9},
                    {"source_message_id": 5, "kind": "request", "title": "Дубль", "confidence": 0.9},
                    {"source_message_id": 6, "kind": "chatter", "title": "Не тот вид", "confidence": 0.9},
                    {"source_message_id": 7, "kind": "request", "title": "Слабый", "confidence": 0.2},
                    {"source_message_id": 99, "kind": "request", "title": "Чужой id", "confidence": 0.9},
                    {
                        "source_message_id": 8,
                        "kind": "self_commitment",
                        "title": "Оплатить хостинг",
                        "due": "2026-09-01",
                        "confidence": 0.8,
                    },
                    {
                        "source_message_id": 9,
                        "kind": "request",
                        "title": "Кривая дата",
                        "due": "завтра",
                        "confidence": 0.8,
                    },
                ]
            },
            ensure_ascii=False,
        )
        + "\n```"
    )
    result = telegram_business.parse_candidates(raw, {5, 6, 7, 8, 9})
    assert [(c["source_message_id"], c["kind"], c["due"]) for c in result] == [
        (5, "request", None),
        (8, "self_commitment", "2026-09-01"),
        (9, "request", None),
    ]
    assert telegram_business.parse_candidates("не json", {1}) == []
    assert telegram_business.parse_candidates("", {1}) == []


def test_tick_waits_for_lull():
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 1, "привет")})

    def engine(config, prompt):  # pragma: no cover - must not run
        raise AssertionError("analysis must wait for the lull")

    now = state["chats"]["777"]["last_activity"]
    counts = telegram_business.tick(config, state, now=now + 10, run_analysis=engine)
    assert counts == {"items_checked": 0, "candidates": 0, "suppressed": 0}


def test_tick_analyzes_and_sends_card(telegram_calls, tracker_calls):
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(
        config, state, {"business_message": _business_message(777, 1, "Привет! Скинь договор до пятницы")}
    )
    prompts: list[str] = []

    def engine(config, prompt):
        prompts.append(prompt)
        return json.dumps(
            {
                "candidates": [
                    {
                        "source_message_id": 1,
                        "kind": "request",
                        "title": "Скинуть договор",
                        "details": "Иван ждёт договор до пятницы.",
                        "due": None,
                        "confidence": 0.9,
                    }
                ]
            },
            ensure_ascii=False,
        )

    now = state["chats"]["777"]["last_activity"]
    counts = telegram_business.tick(config, state, now=now + 120, run_analysis=engine)
    assert counts["candidates"] == 1
    assert counts["items_checked"] == 1
    assert len(prompts) == 1
    assert "Скинь договор" in prompts[0]
    chat = state["chats"]["777"]
    assert chat["analyzed_through"] == 1
    assert chat["suggested"] == [1]
    card = state["candidates"]["777:1"]
    assert card["card_message_id"] == 555
    assert card["quote"] == "Привет! Скинь договор до пятницы"
    sent = [payload for method, payload in telegram_calls if method == "sendMessage"]
    assert len(sent) == 1
    assert "Скинуть договор" in sent[0]["text"]
    assert sent[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "cand:take:777:1"
    ledger_paths = [path for path, _ in tracker_calls]
    assert ledger_paths == ["/api/agent/ingestion/check", "/api/agent/ingestion/decisions"]
    decision = tracker_calls[1][1]
    assert decision["decision"] == "needs_review"
    assert decision["source_item_id"] == "telegram:business:777:1"


def test_tick_suppressed_by_ledger(telegram_calls, tracker_calls):
    tracker_calls.responses["/api/agent/ingestion/check"] = {"items": [{"should_process": False}]}
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 1, "напомни")})

    def engine(config, prompt):
        return json.dumps(
            {"candidates": [{"source_message_id": 1, "kind": "request", "title": "Напомнить", "confidence": 0.9}]}
        )

    now = state["chats"]["777"]["last_activity"]
    counts = telegram_business.tick(config, state, now=now + 120, run_analysis=engine)
    assert counts == {"items_checked": 1, "candidates": 0, "suppressed": 1}
    assert state["candidates"] == {}
    assert state["chats"]["777"]["suggested"] == [1]
    assert [method for method, _ in telegram_calls] == []


def test_analysis_failure_retries_then_gives_up():
    config = _config()
    state = _fresh_state()
    telegram_business.handle_business_update(config, state, {"business_message": _business_message(777, 1, "hi")})

    def engine(config, prompt):
        return None

    chat = state["chats"]["777"]
    now = chat["last_activity"]
    for expected_failures in (1, 2):
        telegram_business.tick(config, state, now=now + 120, run_analysis=engine)
        assert chat["analyzed_through"] == 0
        assert chat["failures"] == expected_failures
    telegram_business.tick(config, state, now=now + 120, run_analysis=engine)
    assert chat["analyzed_through"] == 1
    assert chat["failures"] == 0


def test_callback_take_creates_task(telegram_calls, tracker_calls):
    config = _config()
    state = _fresh_state()
    state["candidates"]["777:5"] = {
        "source_message_id": 5,
        "kind": "request",
        "title": "Скинуть договор",
        "details": "Иван ждёт договор.",
        "due": "2026-08-29",
        "chat_id": "777",
        "chat_title": "Ivan Petrov",
        "quote": "Скинь договор до пятницы",
        "card_message_id": 555,
        "created_at": "2026-08-24T10:00:00+00:00",
    }
    callback = {
        "id": "cb-1",
        "data": "cand:take:777:5",
        "message": {"message_id": 555, "chat": {"id": 99}, "text": "📥 Кандидат в задачи"},
    }
    counts = telegram_business.handle_business_callback(config, state, callback)
    assert counts["created"] == 1
    assert state["candidates"] == {}
    ingest_payload = dict(tracker_calls)["/api/agent/ingest/context"]
    task = ingest_payload["tasks"][0]
    assert task["title"] == "Скинуть договор"
    assert task["assignee"] == "me"
    assert task["due_at"] == "2026-08-29T18:00:00Z"
    assert task["external_id"].startswith("telegram:")
    assert ":business:777:5:" in task["external_id"]
    decision = dict(tracker_calls)["/api/agent/ingestion/decisions"]
    assert decision["decision"] == "created"
    assert decision["decided_by"] == "human"
    assert decision["task_id"] == 42
    edits = [payload for method, payload in telegram_calls if method == "editMessageText"]
    assert len(edits) == 1
    assert "#42" in edits[0]["text"]


def test_callback_skip_records_ignored(telegram_calls, tracker_calls):
    config = _config()
    state = _fresh_state()
    state["candidates"]["777:5"] = {
        "source_message_id": 5,
        "kind": "request",
        "title": "Скинуть договор",
        "chat_id": "777",
        "chat_title": "Ivan Petrov",
        "quote": "Скинь договор",
        "created_at": "2026-08-24T10:00:00+00:00",
    }
    callback = {"id": "cb-1", "data": "cand:skip:777:5", "message": {"message_id": 555, "chat": {"id": 99}, "text": "x"}}
    counts = telegram_business.handle_business_callback(config, state, callback)
    assert counts["ignored"] == 1
    assert state["candidates"] == {}
    decision = dict(tracker_calls)["/api/agent/ingestion/decisions"]
    assert decision["decision"] == "ignored"
    assert decision["decided_by"] == "human"
    paths = [path for path, _ in tracker_calls]
    assert "/api/agent/ingest/context" not in paths


def test_callback_stale_candidate(telegram_calls, tracker_calls):
    config = _config()
    state = _fresh_state()
    callback = {"id": "cb-1", "data": "cand:take:777:5", "message": {"message_id": 5, "chat": {"id": 99}, "text": "x"}}
    counts = telegram_business.handle_business_callback(config, state, callback)
    assert counts == {"created": 0, "duplicates": 0, "ignored": 0}
    assert tracker_calls == []
    assert [method for method, _ in telegram_calls] == ["answerCallbackQuery"]


def test_prompt_builder_marks_directions_and_sections(monkeypatch):
    monkeypatch.setenv("ASSISTANT_LANG", "ru")
    context = [{"id": 1, "out": False, "text": "старое сообщение"}]
    pending = [{"id": 2, "out": True, "text": "ок, отправлю вечером"}]
    prompt = telegram_business.build_analysis_prompt(
        "Ivan Petrov", context, pending, suggested=[9], lang="ru", min_confidence=0.6, today="2026-08-24"
    )
    assert "«Ivan Petrov»" in prompt
    assert "#1 Собеседник: старое сообщение" in prompt
    assert "#2 Я: ок, отправлю вечером" in prompt
    assert "#9" in prompt
    assert "2026-08-24" in prompt
    assert prompt.index("контекст") < prompt.index("новые сообщения")


def test_state_roundtrip(tmp_path):
    path = tmp_path / "business.json"
    state = telegram_business.load_state(path)
    state["chats"]["777"] = {"title": "Ivan", "messages": [], "analyzed_through": 0}
    telegram_business.mark_dirty(state)
    assert telegram_business.consume_dirty(state) is True
    assert telegram_business.consume_dirty(state) is False
    telegram_business.save_state(path, state)
    restored = telegram_business.load_state(path)
    assert restored["chats"]["777"]["title"] == "Ivan"
    assert "_dirty" not in restored


def test_process_update_routes_business(monkeypatch):
    seen: list[dict] = []
    monkeypatch.setattr(
        telegram_business, "handle_business_update", lambda config, state, update: seen.append(update) or {"created": 1}
    )
    config = _config()
    state = _fresh_state()
    update = {"update_id": 1, "business_message": _business_message(777, 1, "hi")}
    metrics = telegram_adapter.process_update(config, update, state)
    assert len(seen) == 1
    assert metrics.created == 1


def test_process_update_skips_business_in_dry_run(monkeypatch):
    def explode(config, state, update):  # pragma: no cover - must not run
        raise AssertionError("business handler must not run in dry-run")

    monkeypatch.setattr(telegram_business, "handle_business_update", explode)
    config = _config(dry_run=True)
    update = {"update_id": 1, "business_message": _business_message(777, 1, "hi")}
    metrics = telegram_adapter.process_update(config, update, _fresh_state())
    assert metrics.items_checked == 0
    metrics = telegram_adapter.process_update(_config(), update, None)
    assert metrics.items_checked == 0


def test_process_update_routes_candidate_callbacks(monkeypatch):
    routed: list[str] = []
    monkeypatch.setattr(
        telegram_business,
        "handle_business_callback",
        lambda config, state, callback: routed.append(str(callback["data"])) or {"ignored": 1},
    )
    monkeypatch.setattr(
        telegram_adapter, "handle_callback", lambda config, callback: routed.append("legacy:" + str(callback["data"]))
    )
    config = _config()
    state = _fresh_state()
    candidate_update = {
        "update_id": 1,
        "callback_query": {"id": "cb", "data": "cand:skip:777:5", "message": {"chat": {"id": 99}, "message_id": 1}},
    }
    metrics = telegram_adapter.process_update(config, candidate_update, state)
    assert metrics.ignored == 1
    legacy_update = {
        "update_id": 2,
        "callback_query": {"id": "cb", "data": "task:done:5", "message": {"chat": {"id": 99}, "message_id": 1}},
    }
    telegram_adapter.process_update(config, legacy_update, state)
    assert routed == ["cand:skip:777:5", "legacy:task:done:5"]
