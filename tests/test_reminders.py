from datetime import datetime, timezone

from app.schemas import TaskRead
from app.store import is_reminder_due

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def _task(**overrides) -> TaskRead:
    data = {
        "id": 1,
        "title": "t",
        "status": "backlog",
        "assignee": "me",
        "origin": "manual",
        "priority": 3,
        "created_at": NOW,
        "updated_at": NOW,
    }
    data.update(overrides)
    return TaskRead.model_validate(data)


def test_not_due_without_targets():
    assert not is_reminder_due(_task(), NOW)


def test_due_when_past_and_never_sent():
    assert is_reminder_due(_task(due_at=datetime(2026, 7, 1, tzinfo=timezone.utc)), NOW)


def test_not_due_when_target_in_future():
    assert not is_reminder_due(_task(due_at=datetime(2026, 7, 10, tzinfo=timezone.utc)), NOW)


def test_not_due_after_reminder_sent():
    task = _task(
        due_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        reminder_last_sent_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert not is_reminder_due(task, NOW)


def test_done_tasks_never_due():
    assert not is_reminder_due(_task(status="done", due_at=datetime(2026, 7, 1, tzinfo=timezone.utc)), NOW)


def test_due_reminders_endpoint(client, api_headers, make_task):
    make_task(title="overdue", due_at="2020-01-01T00:00:00Z")
    make_task(title="future", due_at="2035-01-01T00:00:00Z")

    titles = [task["title"] for task in client.get("/api/reminders/due", headers=api_headers).json()]
    assert titles == ["overdue"]

    shifted = client.get(
        "/api/reminders/due", params={"now": "2036-01-01T00:00:00Z"}, headers=api_headers
    ).json()
    assert {task["title"] for task in shifted} == {"overdue", "future"}


def test_future_target_does_not_rearm_after_send():
    """Regression: past reminder_at + future due_at must NOT stay due after
    the reminder is sent — it resent on every poll until the future date."""
    task = _task(
        reminder_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        due_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        reminder_last_sent_at=NOW,
    )
    assert not is_reminder_due(task, NOW)
    # ...but once the future due date passes, it fires exactly once more.
    after_due = datetime(2026, 8, 21, tzinfo=timezone.utc)
    assert is_reminder_due(task, after_due)
    task_resent = _task(
        reminder_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        due_at=datetime(2026, 8, 20, tzinfo=timezone.utc),
        reminder_last_sent_at=after_due,
    )
    assert not is_reminder_due(task_resent, after_due)


def test_past_target_newer_than_last_send_still_fires():
    task = _task(
        reminder_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        due_at=datetime(2026, 7, 3, tzinfo=timezone.utc),
        reminder_last_sent_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
    )
    assert is_reminder_due(task, NOW)
