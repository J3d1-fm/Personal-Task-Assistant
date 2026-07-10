from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Protocol

from google.cloud import firestore
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import IngestionDecision, IngestionDecisionType, SourceHealthStatus, SourceState, utc_now
from app.schemas import (
    IngestionCheckItem,
    IngestionDecisionRead,
    IngestionDecisionWrite,
    SourceReport,
    SourceStateRead,
)

SOURCE_ATTENTION_ORDER = {
    SourceHealthStatus.reauth_required: 0,
    SourceHealthStatus.misconfigured: 1,
    SourceHealthStatus.failed: 2,
    SourceHealthStatus.unavailable: 3,
    SourceHealthStatus.rate_limited: 4,
    SourceHealthStatus.degraded: 5,
    SourceHealthStatus.healthy: 6,
}


class IngestionStore(Protocol):
    def list_source_states(self) -> list[SourceStateRead]:
        ...

    def report_source(self, payload: SourceReport) -> SourceStateRead:
        ...

    def get_decision(
        self, source_id: str, source_item_id: str, content_fingerprint: str
    ) -> IngestionDecisionRead | None:
        ...

    def get_decisions(
        self, source_id: str, items: list[IngestionCheckItem]
    ) -> dict[tuple[str, str], IngestionDecisionRead]:
        ...

    def record_decision(self, payload: IngestionDecisionWrite) -> IngestionDecisionRead:
        ...


def _source_to_read(source: SourceState) -> SourceStateRead:
    return SourceStateRead.model_validate(source)


def _decision_to_read(decision: IngestionDecision) -> IngestionDecisionRead:
    return IngestionDecisionRead.model_validate(decision)


def _source_state_data(
    payload: SourceReport,
    *,
    previous: SourceStateRead | None,
    now: datetime,
    checked_at: datetime,
) -> dict:
    is_healthy = payload.status == SourceHealthStatus.healthy
    return {
        "source_id": payload.source_id,
        "source_name": payload.source_name,
        "adapter_type": payload.adapter_type,
        "status": payload.status,
        "last_checked_at": checked_at,
        "last_success_at": checked_at if is_healthy else (previous.last_success_at if previous else None),
        "last_error_at": (previous.last_error_at if previous else None) if is_healthy else checked_at,
        "error_code": None if is_healthy else payload.error_code,
        "error_message": None if is_healthy else payload.error_message,
        "action_url": payload.action_url,
        "items_checked": payload.items_checked,
        "candidates": payload.candidates,
        "created": payload.created,
        "duplicates": payload.duplicates,
        "ignored": payload.ignored,
        "suppressed": payload.suppressed,
        "updated_at": now,
    }


def _source_sort_key(source: SourceStateRead) -> tuple[int, float, str]:
    return (
        SOURCE_ATTENTION_ORDER.get(source.status, 99),
        -_normalized_datetime(source.last_checked_at).timestamp(),
        source.source_name.lower(),
    )


class SqliteIngestionStore:
    def __init__(self, db: Session):
        self.db = db

    def list_source_states(self) -> list[SourceStateRead]:
        states = [_source_to_read(source) for source in self.db.scalars(select(SourceState)).all()]
        return sorted(states, key=_source_sort_key)

    def report_source(self, payload: SourceReport) -> SourceStateRead:
        source = self.db.get(SourceState, payload.source_id)
        previous = _source_to_read(source) if source is not None else None
        now = utc_now()
        checked_at = _normalized_datetime(payload.checked_at or now)
        data = _source_state_data(payload, previous=previous, now=now, checked_at=checked_at)
        if source is None:
            source = SourceState(**data, created_at=now)
            self.db.add(source)
            try:
                self.db.commit()
                self.db.refresh(source)
                return _source_to_read(source)
            except IntegrityError:
                self.db.rollback()

        atomic_data = dict(data)
        if payload.status == SourceHealthStatus.healthy:
            atomic_data["last_error_at"] = SourceState.last_error_at
        else:
            atomic_data["last_success_at"] = SourceState.last_success_at
        result = self.db.execute(
            update(SourceState)
            .where(
                SourceState.source_id == payload.source_id,
                SourceState.last_checked_at < checked_at,
            )
            .values(**atomic_data)
            .execution_options(synchronize_session=False)
        )
        self.db.commit()
        self.db.expire_all()
        current = self.db.get(SourceState, payload.source_id)
        if current is None:
            raise RuntimeError("Source state disappeared during report update")
        if result.rowcount not in {0, 1}:
            raise RuntimeError("Source state report updated an unexpected number of rows")
        return _source_to_read(current)

    def _find_decision(
        self, source_id: str, source_item_id: str, content_fingerprint: str
    ) -> IngestionDecision | None:
        statement = select(IngestionDecision).where(
            IngestionDecision.source_id == source_id,
            IngestionDecision.source_item_id == source_item_id,
            IngestionDecision.content_fingerprint == content_fingerprint,
        )
        return self.db.scalars(statement).first()

    def get_decision(
        self, source_id: str, source_item_id: str, content_fingerprint: str
    ) -> IngestionDecisionRead | None:
        decision = self._find_decision(source_id, source_item_id, content_fingerprint)
        return _decision_to_read(decision) if decision is not None else None

    def get_decisions(
        self, source_id: str, items: list[IngestionCheckItem]
    ) -> dict[tuple[str, str], IngestionDecisionRead]:
        item_ids = {item.source_item_id for item in items}
        if not item_ids:
            return {}
        statement = select(IngestionDecision).where(
            IngestionDecision.source_id == source_id,
            IngestionDecision.source_item_id.in_(item_ids),
        )
        wanted = {(item.source_item_id, item.content_fingerprint) for item in items}
        decisions = {}
        for decision in self.db.scalars(statement).all():
            key = (decision.source_item_id, decision.content_fingerprint)
            if key in wanted:
                decisions[key] = _decision_to_read(decision)
        return decisions

    def record_decision(self, payload: IngestionDecisionWrite) -> IngestionDecisionRead:
        now = utc_now()
        decision = self._find_decision(payload.source_id, payload.source_item_id, payload.content_fingerprint)
        values = payload.model_dump()
        values["updated_at"] = now
        if decision is None:
            decision = IngestionDecision(**values, decided_at=now)
            self.db.add(decision)
            try:
                self.db.commit()
            except IntegrityError:
                self.db.rollback()
                decision = self._find_decision(payload.source_id, payload.source_item_id, payload.content_fingerprint)
                if decision is None:
                    raise
                for key, value in values.items():
                    setattr(decision, key, value)
                self.db.commit()
        else:
            for key, value in values.items():
                setattr(decision, key, value)
            self.db.commit()
        self.db.refresh(decision)
        return _decision_to_read(decision)


def _firestore_key(*parts: str) -> str:
    serialized = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _encode(data: dict) -> dict:
    return {key: value.value if hasattr(value, "value") else value for key, value in data.items()}


class FirestoreIngestionStore:
    def __init__(self):
        self.client = firestore.Client()
        self.sources = self.client.collection("source_states")
        self.decisions = self.client.collection("ingestion_decisions")

    def list_source_states(self) -> list[SourceStateRead]:
        states = [SourceStateRead.model_validate(snapshot.to_dict() or {}) for snapshot in self.sources.stream()]
        return sorted(states, key=_source_sort_key)

    def report_source(self, payload: SourceReport) -> SourceStateRead:
        ref = self.sources.document(_firestore_key(payload.source_id))
        now = utc_now()
        checked_at = _normalized_datetime(payload.checked_at or now)

        @firestore.transactional
        def apply_report(transaction: firestore.Transaction) -> SourceStateRead:
            snapshot = ref.get(transaction=transaction)
            previous = SourceStateRead.model_validate(snapshot.to_dict() or {}) if snapshot.exists else None
            if previous is not None and checked_at <= _normalized_datetime(previous.last_checked_at):
                return previous
            data = _source_state_data(payload, previous=previous, now=now, checked_at=checked_at)
            data["created_at"] = previous.created_at if previous else now
            encoded = _encode(data)
            transaction.set(ref, encoded)
            return SourceStateRead.model_validate(encoded)

        return apply_report(self.client.transaction())

    def _decision_ref(self, source_id: str, source_item_id: str, content_fingerprint: str):
        return self.decisions.document(_firestore_key(source_id, source_item_id, content_fingerprint))

    def get_decision(
        self, source_id: str, source_item_id: str, content_fingerprint: str
    ) -> IngestionDecisionRead | None:
        snapshot = self._decision_ref(source_id, source_item_id, content_fingerprint).get()
        if not snapshot.exists:
            return None
        return IngestionDecisionRead.model_validate(snapshot.to_dict() or {})

    def get_decisions(
        self, source_id: str, items: list[IngestionCheckItem]
    ) -> dict[tuple[str, str], IngestionDecisionRead]:
        refs = [
            self._decision_ref(source_id, item.source_item_id, item.content_fingerprint)
            for item in items
        ]
        decisions = {}
        for snapshot in self.client.get_all(refs):
            if not snapshot.exists:
                continue
            decision = IngestionDecisionRead.model_validate(snapshot.to_dict() or {})
            decisions[(decision.source_item_id, decision.content_fingerprint)] = decision
        return decisions

    def record_decision(self, payload: IngestionDecisionWrite) -> IngestionDecisionRead:
        now = utc_now()
        ref = self._decision_ref(payload.source_id, payload.source_item_id, payload.content_fingerprint)

        @firestore.transactional
        def apply_decision(transaction: firestore.Transaction) -> IngestionDecisionRead:
            snapshot = ref.get(transaction=transaction)
            existing = snapshot.to_dict() or {} if snapshot.exists else {}
            data = payload.model_dump()
            data.update({"decided_at": existing.get("decided_at") or now, "updated_at": now})
            encoded = _encode(data)
            transaction.set(ref, encoded)
            return IngestionDecisionRead.model_validate(encoded)

        return apply_decision(self.client.transaction())


def _normalized_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def decision_should_process(decision: IngestionDecisionRead | None, *, now: datetime | None = None) -> bool:
    if decision is None or decision.decision == IngestionDecisionType.failed:
        return True
    if decision.revisit_after is None:
        return False
    current = _normalized_datetime(now or utc_now())
    return _normalized_datetime(decision.revisit_after) <= current


def get_ingestion_store(db: Session | None = None) -> IngestionStore:
    if get_settings().use_firestore:
        return FirestoreIngestionStore()
    if db is None:
        raise RuntimeError("SQL-backed ingestion store requires a database session")
    return SqliteIngestionStore(db)
