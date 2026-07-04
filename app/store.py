from datetime import datetime, timedelta, timezone
from typing import Protocol

from google.cloud import firestore
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Task, TaskStatus, utc_now
from app.schemas import TaskCreate, TaskRead, TaskUpdate

DEFAULT_DUE_DAYS_BY_PRIORITY = {
    1: 1,
    2: 3,
    3: 7,
    4: 14,
    5: 30,
}


class DuplicateExternalIdError(Exception):
    def __init__(self, external_id: str):
        super().__init__(f"Task with external_id {external_id!r} already exists")
        self.external_id = external_id


class TaskStore(Protocol):
    def get_task(self, task_id: int) -> TaskRead | None:
        ...

    def get_task_by_external_id(self, external_id: str) -> TaskRead | None:
        ...

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assignee: str | None = None,
        include_done: bool = False,
    ) -> list[TaskRead]:
        ...

    def create_task(self, payload: TaskCreate) -> TaskRead:
        ...

    def update_task(self, task_id: int, payload: TaskUpdate) -> TaskRead | None:
        ...

    def try_claim(self, task_id: int) -> TaskRead | None:
        ...

    def delete_task(self, task_id: int) -> bool:
        ...

    def due_reminders(self, *, now: datetime | None = None) -> list[TaskRead]:
        ...


def task_to_read(task: Task) -> TaskRead:
    return TaskRead.model_validate(task)


def apply_create_defaults(payload: TaskCreate, *, now: datetime | None = None) -> TaskCreate:
    if payload.due_at is not None:
        return payload
    current = now or utc_now()
    estimate_days = DEFAULT_DUE_DAYS_BY_PRIORITY.get(payload.priority, DEFAULT_DUE_DAYS_BY_PRIORITY[3])
    return payload.model_copy(update={"due_at": current + timedelta(days=estimate_days)})


class SqliteTaskStore:
    def __init__(self, db: Session):
        self.db = db

    def get_task(self, task_id: int) -> TaskRead | None:
        task = self.db.get(Task, task_id)
        if task is None:
            return None
        return task_to_read(task)

    def get_task_by_external_id(self, external_id: str) -> TaskRead | None:
        task = self.db.scalars(select(Task).where(Task.external_id == external_id)).first()
        if task is None:
            return None
        return task_to_read(task)

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assignee: str | None = None,
        include_done: bool = False,
    ) -> list[TaskRead]:
        statement = select(Task).order_by(Task.priority.asc(), Task.due_at.asc().nullslast(), Task.created_at.desc())
        if status is not None:
            statement = statement.where(Task.status == status)
        elif not include_done:
            statement = statement.where(Task.status.not_in([TaskStatus.done, TaskStatus.cancelled]))
        if assignee:
            statement = statement.where(Task.assignee == assignee)
        return [task_to_read(task) for task in self.db.scalars(statement).all()]

    def create_task(self, payload: TaskCreate) -> TaskRead:
        payload = apply_create_defaults(payload)
        task = Task(**payload.model_dump())
        self.db.add(task)
        try:
            self.db.commit()
        except IntegrityError:
            self.db.rollback()
            if payload.external_id and self.get_task_by_external_id(payload.external_id) is not None:
                raise DuplicateExternalIdError(payload.external_id) from None
            raise
        self.db.refresh(task)
        return task_to_read(task)

    def update_task(self, task_id: int, payload: TaskUpdate) -> TaskRead | None:
        task = self.db.get(Task, task_id)
        if task is None:
            return None
        updates = payload.model_dump(exclude_unset=True)
        if updates.get("status") == TaskStatus.done and task.completed_at is None:
            task.completed_at = utc_now()
        if "status" in updates and updates["status"] != TaskStatus.done:
            task.completed_at = None
        for key, value in updates.items():
            setattr(task, key, value)
        self.db.commit()
        self.db.refresh(task)
        return task_to_read(task)

    def try_claim(self, task_id: int) -> TaskRead | None:
        result = self.db.execute(
            update(Task)
            .where(Task.id == task_id, Task.status == TaskStatus.backlog)
            .values(status=TaskStatus.in_progress, updated_at=utc_now())
        )
        if result.rowcount != 1:
            self.db.rollback()
            return None
        self.db.commit()
        return self.get_task(task_id)

    def delete_task(self, task_id: int) -> bool:
        task = self.db.get(Task, task_id)
        if task is None:
            return False
        self.db.delete(task)
        self.db.commit()
        return True

    def due_reminders(self, *, now: datetime | None = None) -> list[TaskRead]:
        current = now or datetime.now(timezone.utc)
        tasks = self.list_tasks(include_done=False)
        return [task for task in tasks if is_reminder_due(task, current)]


class FirestoreTaskStore:
    def __init__(self):
        self.client = firestore.Client()
        self.collection = self.client.collection("tasks")
        self.counter = self.client.collection("metadata").document("task_counter")

    def _next_id(self) -> int:
        @firestore.transactional
        def increment(transaction: firestore.Transaction) -> int:
            snapshot = self.counter.get(transaction=transaction)
            current = int(snapshot.get("value") or 0) if snapshot.exists else 0
            value = current + 1
            transaction.set(self.counter, {"value": value}, merge=True)
            return value

        return increment(self.client.transaction())

    def _doc_to_task(self, snapshot: firestore.DocumentSnapshot) -> TaskRead:
        data = snapshot.to_dict() or {}
        return TaskRead.model_validate(data)

    def get_task(self, task_id: int) -> TaskRead | None:
        snapshot = self.collection.document(str(task_id)).get()
        if not snapshot.exists:
            return None
        return self._doc_to_task(snapshot)

    def get_task_by_external_id(self, external_id: str) -> TaskRead | None:
        snapshots = list(self.collection.where("external_id", "==", external_id).limit(1).stream())
        if not snapshots:
            return None
        return self._doc_to_task(snapshots[0])

    def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        assignee: str | None = None,
        include_done: bool = False,
    ) -> list[TaskRead]:
        query = self.collection
        if status is not None:
            query = query.where("status", "==", status.value)
        elif not include_done:
            # Server-side filter so agent polling never reads the done/cancelled backlog.
            query = query.where("status", "not-in", [TaskStatus.done.value, TaskStatus.cancelled.value])
        if assignee:
            query = query.where("assignee", "==", assignee)
        tasks = [self._doc_to_task(snapshot) for snapshot in query.stream()]
        if status is None and not include_done:
            tasks = [task for task in tasks if task.status not in {TaskStatus.done, TaskStatus.cancelled}]
        return sorted(
            tasks,
            key=lambda task: (
                task.priority,
                task.due_at or datetime.max.replace(tzinfo=timezone.utc),
                -task.created_at.timestamp(),
            ),
        )

    def create_task(self, payload: TaskCreate) -> TaskRead:
        now = utc_now()
        if payload.external_id and self.get_task_by_external_id(payload.external_id) is not None:
            raise DuplicateExternalIdError(payload.external_id)
        task_id = self._next_id()
        payload = apply_create_defaults(payload, now=now)
        data = payload.model_dump()
        data.update(
            {
                "id": task_id,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "reminder_last_sent_at": None,
            }
        )
        data = encode_task_data(data)
        self.collection.document(str(task_id)).set(data)
        return TaskRead.model_validate(data)

    def update_task(self, task_id: int, payload: TaskUpdate) -> TaskRead | None:
        ref = self.collection.document(str(task_id))
        snapshot = ref.get()
        if not snapshot.exists:
            return None
        data = snapshot.to_dict() or {}
        updates = payload.model_dump(exclude_unset=True)
        if updates.get("status") == TaskStatus.done and data.get("completed_at") is None:
            updates["completed_at"] = utc_now()
        if "status" in updates and updates["status"] != TaskStatus.done:
            updates["completed_at"] = None
        updates["updated_at"] = utc_now()
        encoded_updates = encode_task_data(updates)
        ref.update(encoded_updates)
        refreshed = ref.get()
        if not refreshed.exists:
            return None
        return self._doc_to_task(refreshed)

    def try_claim(self, task_id: int) -> TaskRead | None:
        ref = self.collection.document(str(task_id))

        @firestore.transactional
        def claim(transaction: firestore.Transaction) -> TaskRead | None:
            snapshot = ref.get(transaction=transaction)
            if not snapshot.exists:
                return None
            data = snapshot.to_dict() or {}
            if data.get("status") != TaskStatus.backlog.value:
                return None
            now = utc_now()
            transaction.update(ref, {"status": TaskStatus.in_progress.value, "updated_at": now})
            data.update({"status": TaskStatus.in_progress.value, "updated_at": now})
            return TaskRead.model_validate(data)

        return claim(self.client.transaction())

    def delete_task(self, task_id: int) -> bool:
        ref = self.collection.document(str(task_id))
        snapshot = ref.get()
        if not snapshot.exists:
            return False
        ref.delete()
        return True

    def due_reminders(self, *, now: datetime | None = None) -> list[TaskRead]:
        current = now or datetime.now(timezone.utc)
        return [task for task in self.list_tasks(include_done=False) if is_reminder_due(task, current)]


def encode_task_data(data: dict) -> dict:
    encoded = {}
    for key, value in data.items():
        if hasattr(value, "value"):
            encoded[key] = value.value
        else:
            encoded[key] = value
    return encoded


def is_reminder_due(task: TaskRead, current: datetime) -> bool:
    if task.status in {TaskStatus.done, TaskStatus.cancelled}:
        return False
    current = normalize_datetime(current)
    targets = [normalize_datetime(value) for value in [task.reminder_at, task.due_at] if value is not None]
    if not targets:
        return False
    due_at = min(targets)
    if due_at > current:
        return False
    if task.reminder_last_sent_at is None:
        return True
    reminder_last_sent_at = normalize_datetime(task.reminder_last_sent_at)
    return any(reminder_last_sent_at < target for target in targets)


def normalize_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def get_task_store(db: Session | None = None) -> TaskStore:
    if get_settings().use_firestore:
        return FirestoreTaskStore()
    if db is None:
        raise RuntimeError("SQL-backed task store requires a database session")
    return SqliteTaskStore(db)
