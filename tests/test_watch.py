"""Tests for the watch loop (automation/watch.py).

The planning functions are pure; the tick is exercised end to end against an
httpx.MockTransport standing in for the tracker, with Telegram sends
monkeypatched. The invariant that matters most: reminder_last_sent_at is
stamped only after a reminder was actually delivered, so a Telegram outage
never swallows a reminder.
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "automation"))

import watch  # noqa: E402
from notify import NotifyConfig  # noqa: E402
from watch import (  # noqa: E402
    WatchConfig,
    agent_backlog,
    load_state,
    plan_review_notifications,
    save_state,
    tick,
)


def _task(task_id, *, status="backlog", assignee="me", title="task", updated_at="2026-08-06T10:00:00Z"):
    return {
        "id": task_id,
        "title": title,
        "status": status,
        "assignee": assignee,
        "priority": 2,
        "description": "",
        "source_url": None,
        "updated_at": updated_at,
        "due_at": None,
        "reminder_at": None,
    }


def test_plan_review_notifications_announces_once_and_reannounces_on_reentry():
    tasks = [_task(1, status="waiting_review"), _task(2, status="waiting_review")]
    to_announce, retained = plan_review_notifications(tasks, {})
    assert [t["id"] for t in to_announce] == [1, 2]

    notified = {"1": "seen", "2": "seen"}
    to_announce, retained = plan_review_notifications(tasks, notified)
    assert to_announce == []
    assert set(retained) == {"1", "2"}

    # Task 1 leaves review -> pruned; when it re-enters, it is announced again.
    tasks_after = [_task(1, status="in_progress"), _task(2, status="waiting_review")]
    to_announce, retained = plan_review_notifications(tasks_after, notified)
    assert to_announce == []
    assert set(retained) == {"2"}
    tasks_reentered = [_task(1, status="waiting_review"), _task(2, status="waiting_review")]
    to_announce, _ = plan_review_notifications(tasks_reentered, retained)
    assert [t["id"] for t in to_announce] == [1]


def test_agent_backlog_filters_codex_backlog_only():
    tasks = [
        _task(1, assignee="codex", status="backlog"),
        _task(2, assignee="codex", status="in_progress"),
        _task(3, assignee="me", status="backlog"),
    ]
    assert [t["id"] for t in agent_backlog(tasks)] == [1]


def test_state_roundtrip_and_corrupt_recovery(tmp_path):
    path = tmp_path / "state.json"
    save_state(path, {"review_notified": {"1": "x"}})
    assert load_state(path)["review_notified"] == {"1": "x"}
    path.write_text("not json", encoding="utf-8")
    assert load_state(path) == {"review_notified": {}}


def _watch_config(tmp_path, notify=None) -> WatchConfig:
    return WatchConfig(
        url="http://tracker.test",
        api_key="key",
        interval=30,
        state_path=tmp_path / "state.json",
        work_enabled=False,
        work_max_tasks=3,
        notify=notify,
    )


def _mock_tracker(tasks, reminders, patches):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tasks":
            return httpx.Response(200, json=tasks)
        if request.url.path == "/api/reminders/due":
            return httpx.Response(200, json=reminders)
        if request.method == "PATCH" and request.url.path.startswith("/api/tasks/"):
            patches.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json=_task(0))
        return httpx.Response(404)

    return httpx.Client(base_url="http://tracker.test", transport=httpx.MockTransport(handler))


def test_tick_stamps_reminder_only_after_successful_delivery(monkeypatch, tmp_path):
    notify_config = NotifyConfig(bot_token="t", chat_id="1")
    reminder = _task(5, title="pay rent")
    patches = []
    now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(watch, "send_message", lambda *a, **k: True)
    with _mock_tracker([reminder], [reminder], patches) as client:
        tick(_watch_config(tmp_path, notify_config), {"review_notified": {}}, now=now, client=client)
    assert patches == [("/api/tasks/5", {"reminder_last_sent_at": now.isoformat()})]

    patches.clear()
    monkeypatch.setattr(watch, "send_message", lambda *a, **k: False)
    with _mock_tracker([reminder], [reminder], patches) as client:
        tick(_watch_config(tmp_path, notify_config), {"review_notified": {}}, now=now, client=client)
    assert patches == []


def test_tick_records_review_announcements_in_state(monkeypatch, tmp_path):
    notify_config = NotifyConfig(bot_token="t", chat_id="1")
    review_task = _task(9, status="waiting_review", title="check deck")
    sent = []

    def fake_send(config, text, reply_markup=None):
        sent.append((text, reply_markup))
        return True

    monkeypatch.setattr(watch, "send_message", fake_send)
    with _mock_tracker([review_task], [], []) as client:
        state = tick(_watch_config(tmp_path, notify_config), {"review_notified": {}}, client=client)
    assert "9" in state["review_notified"]
    assert any("check deck" in text and markup is not None for text, markup in sent)

    # Second tick: already announced, nothing sent.
    sent.clear()
    with _mock_tracker([review_task], [], []) as client:
        tick(_watch_config(tmp_path, notify_config), state, client=client)
    assert sent == []


def test_tick_without_notify_leaves_reminders_unstamped(tmp_path):
    reminder = _task(5)
    patches = []
    with _mock_tracker([reminder], [reminder], patches) as client:
        tick(_watch_config(tmp_path, notify=None), {"review_notified": {}}, client=client)
    assert patches == []


def test_tick_triggers_work_backend_when_ready(monkeypatch, tmp_path):
    import work_runner

    backlog_task = _task(4, assignee="codex", status="backlog")
    calls = []
    monkeypatch.setattr(work_runner, "backend_ready", lambda: (True, ""))
    monkeypatch.setattr(
        work_runner, "run_work", lambda url, key, max_tasks: calls.append((url, max_tasks)) or []
    )
    config = WatchConfig(
        url="http://tracker.test",
        api_key="key",
        interval=30,
        state_path=tmp_path / "state.json",
        work_enabled=True,
        work_max_tasks=2,
        notify=None,
    )
    with _mock_tracker([backlog_task], [], []) as client:
        tick(config, {"review_notified": {}}, client=client)
    assert calls == [("http://tracker.test", 2)]


def test_tick_warns_once_when_backend_not_ready(monkeypatch, tmp_path, capsys):
    import work_runner

    backlog_task = _task(4, assignee="codex", status="backlog")
    monkeypatch.setattr(work_runner, "backend_ready", lambda: (False, "no backend"))
    monkeypatch.setattr(
        work_runner, "run_work", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
    )
    config = WatchConfig(
        url="http://tracker.test",
        api_key="key",
        interval=30,
        state_path=tmp_path / "state.json",
        work_enabled=True,
        work_max_tasks=2,
        notify=None,
    )
    state = {"review_notified": {}}
    with _mock_tracker([backlog_task], [], []) as client:
        state = tick(config, state, client=client)
        state = tick(config, state, client=client)
    assert capsys.readouterr().err.count("no backend") == 1
    assert state["work_warned"] is True
