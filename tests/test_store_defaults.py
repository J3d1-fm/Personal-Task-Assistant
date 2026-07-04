from datetime import datetime, timedelta, timezone

from app.models import TaskStatus
from app.schemas import TaskCreate
from app.store import apply_create_defaults, encode_task_data

NOW = datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc)


def test_due_defaults_by_priority():
    for priority, days in [(1, 1), (2, 3), (3, 7), (4, 14), (5, 30)]:
        payload = apply_create_defaults(TaskCreate(title="t", priority=priority), now=NOW)
        assert payload.due_at == NOW + timedelta(days=days)


def test_explicit_due_not_overwritten():
    explicit = NOW + timedelta(days=90)
    payload = apply_create_defaults(TaskCreate(title="t", due_at=explicit), now=NOW)
    assert payload.due_at == explicit


def test_encode_task_data_unwraps_enums():
    encoded = encode_task_data({"status": TaskStatus.backlog, "priority": 3})
    assert encoded == {"status": "backlog", "priority": 3}
