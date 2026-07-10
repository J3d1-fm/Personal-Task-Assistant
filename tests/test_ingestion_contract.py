from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import app.ingestion_store as ingestion_store
from app.migrate import run_migrations
from app.schemas import IngestionDecisionWrite, SourceReport


class _FakeSnapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class _FakeDocument:
    def __init__(self, documents, document_id):
        self.documents = documents
        self.document_id = document_id

    def get(self, transaction=None):
        return _FakeSnapshot(self.documents.get(self.document_id))

    def set(self, data):
        self.documents[self.document_id] = dict(data)


class _FakeCollection:
    def __init__(self):
        self.documents = {}

    def document(self, document_id):
        return _FakeDocument(self.documents, document_id)

    def stream(self):
        return [_FakeSnapshot(data) for data in self.documents.values()]


class _FakeTransaction:
    def set(self, document, data):
        document.set(data)


class _FakeFirestoreClient:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        return self.collections.setdefault(name, _FakeCollection())

    def transaction(self):
        return _FakeTransaction()

    def get_all(self, references):
        return [reference.get() for reference in references]


def _source_report(**overrides):
    return {
        "source_id": "gmail:personal",
        "source_name": "Personal Gmail",
        "adapter_type": "gmail_polling",
        "status": "healthy",
        "items_checked": 12,
        "candidates": 3,
        "created": 1,
        "duplicates": 1,
        "ignored": 1,
        "suppressed": 0,
        **overrides,
    }


def _decision(**overrides):
    return {
        "source_id": "slack:work",
        "source_item_id": "thread:C123:1710000000.000100",
        "content_fingerprint": "sha256:original",
        "decision": "ignored",
        "reason": "FYI only; no action requested",
        "decided_by": "adapter",
        **overrides,
    }


def _check(client, api_headers, *, fingerprint="sha256:original"):
    response = client.post(
        "/api/agent/ingestion/check",
        json={
            "source_id": "slack:work",
            "items": [
                {
                    "source_item_id": "thread:C123:1710000000.000100",
                    "content_fingerprint": fingerprint,
                }
            ],
        },
        headers=api_headers,
    )
    assert response.status_code == 200, response.text
    return response.json()["items"][0]


def test_source_health_report_is_visible_and_keeps_last_success(client, api_headers):
    healthy = client.post("/api/agent/sources/report", json=_source_report(), headers=api_headers)
    assert healthy.status_code == 200, healthy.text
    first = healthy.json()
    assert first["status"] == "healthy"
    assert first["last_success_at"] == first["last_checked_at"]
    assert first["last_checked_at"].endswith(("Z", "+00:00"))
    assert first["error_message"] is None

    reauth = client.post(
        "/api/agent/sources/report",
        json=_source_report(
            status="reauth_required",
            error_code="invalid_grant",
            error_message="Access expired",
            action_url="https://example.com/reauth",
            items_checked=0,
            candidates=0,
            created=0,
            duplicates=0,
            ignored=0,
        ),
        headers=api_headers,
    )
    assert reauth.status_code == 200, reauth.text
    second = reauth.json()
    assert second["status"] == "reauth_required"
    assert second["last_success_at"] == first["last_success_at"]
    assert second["last_error_at"] == second["last_checked_at"]
    assert second["action_url"] == "https://example.com/reauth"

    listed = client.get("/api/sources", headers=api_headers)
    assert listed.status_code == 200
    assert [source["source_id"] for source in listed.json()] == ["gmail:personal"]


def test_source_health_rejects_out_of_order_reports(client, api_headers):
    newer_time = datetime.now(timezone.utc)
    newer = newer_time.isoformat()
    older = (newer_time - timedelta(hours=1)).isoformat()
    healthy = client.post(
        "/api/agent/sources/report",
        json=_source_report(checked_at=newer),
        headers=api_headers,
    )
    assert healthy.status_code == 200
    stale_failure = client.post(
        "/api/agent/sources/report",
        json=_source_report(
            checked_at=older,
            status="reauth_required",
            error_code="old_error",
            error_message="This must not replace current state",
        ),
        headers=api_headers,
    )
    assert stale_failure.status_code == 200
    assert stale_failure.json()["status"] == "healthy"
    assert datetime.fromisoformat(stale_failure.json()["last_checked_at"].replace("Z", "+00:00")) == datetime.fromisoformat(
        newer
    )


def test_source_health_rejects_implausible_future_reports(client, api_headers):
    response = client.post(
        "/api/agent/sources/report",
        json=_source_report(checked_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat()),
        headers=api_headers,
    )
    assert response.status_code == 422


def test_sqlite_source_reports_resolve_concurrently_by_checked_at(tmp_path):
    url = f"sqlite:///{tmp_path / 'source-race.db'}"
    run_migrations(url)
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 10})
    sessions = sessionmaker(bind=engine)
    base = datetime.now(timezone.utc) - timedelta(minutes=2)
    with sessions() as session:
        ingestion_store.SqliteIngestionStore(session).report_source(
            SourceReport(**_source_report(checked_at=base.isoformat()))
        )

    barrier = Barrier(2)

    def submit(report):
        with sessions() as session:
            barrier.wait()
            return ingestion_store.SqliteIngestionStore(session).report_source(SourceReport(**report))

    healthy_time = base + timedelta(seconds=30)
    failure_time = base + timedelta(seconds=60)
    healthy_report = _source_report(checked_at=healthy_time.isoformat(), status="healthy")
    failure_report = _source_report(
        checked_at=failure_time.isoformat(),
        status="reauth_required",
        error_code="invalid_grant",
        error_message="Access expired",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(submit, [healthy_report, failure_report]))

    with sessions() as session:
        final = ingestion_store.SqliteIngestionStore(session).list_source_states()[0]
    engine.dispose()
    assert final.status.value == "reauth_required"
    assert final.last_checked_at == failure_time


def test_source_health_contract_rejects_unsafe_action_urls(client, api_headers):
    response = client.post(
        "/api/agent/sources/report",
        json=_source_report(action_url="javascript:alert(1)"),
        headers=api_headers,
    )
    assert response.status_code == 422
    credential_url = client.post(
        "/api/agent/sources/report",
        json=_source_report(action_url="https://user:secret@example.com/reauth"),
        headers=api_headers,
    )
    assert credential_url.status_code == 422


def test_ingestion_decision_suppresses_same_content_but_not_changed_content(client, api_headers):
    unseen = _check(client, api_headers)
    assert unseen == {
        "source_item_id": "thread:C123:1710000000.000100",
        "content_fingerprint": "sha256:original",
        "should_process": True,
        "existing_decision": None,
    }

    recorded = client.post(
        "/api/agent/ingestion/decisions",
        json=_decision(),
        headers=api_headers,
    )
    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["decision"] == "ignored"

    same = _check(client, api_headers)
    assert same["should_process"] is False
    assert same["existing_decision"]["reason"] == "FYI only; no action requested"

    changed = _check(client, api_headers, fingerprint="sha256:changed")
    assert changed["should_process"] is True
    assert changed["existing_decision"] is None


def test_decision_upsert_preserves_decided_at_and_batch_check(client, api_headers):
    first = client.post("/api/agent/ingestion/decisions", json=_decision(), headers=api_headers).json()
    second = client.post(
        "/api/agent/ingestion/decisions",
        json=_decision(reason="Confirmed by human", decided_by="human"),
        headers=api_headers,
    ).json()
    assert second["decided_at"] == first["decided_at"]
    assert second["updated_at"] >= first["updated_at"]

    response = client.post(
        "/api/agent/ingestion/check",
        json={
            "source_id": "slack:work",
            "items": [
                {
                    "source_item_id": "thread:C123:1710000000.000100",
                    "content_fingerprint": "sha256:original",
                },
                {
                    "source_item_id": "thread:C123:1710000000.000100",
                    "content_fingerprint": "sha256:changed",
                },
            ],
        },
        headers=api_headers,
    )
    assert response.status_code == 200, response.text
    assert [item["should_process"] for item in response.json()["items"]] == [False, True]


def test_revisit_and_failed_decisions_remain_retryable(client, api_headers):
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    recorded = client.post(
        "/api/agent/ingestion/decisions",
        json=_decision(revisit_after=past),
        headers=api_headers,
    )
    assert recorded.status_code == 200, recorded.text
    assert _check(client, api_headers)["should_process"] is True

    failed = client.post(
        "/api/agent/ingestion/decisions",
        json=_decision(decision="failed", revisit_after=None),
        headers=api_headers,
    )
    assert failed.status_code == 200, failed.text
    assert _check(client, api_headers)["should_process"] is True


def test_ingestion_contract_requires_auth(client):
    assert client.get("/api/sources").status_code == 401
    assert client.post("/api/agent/sources/report", json=_source_report()).status_code == 401
    assert client.post("/api/agent/ingestion/decisions", json=_decision()).status_code == 401


def test_firestore_ingestion_store_matches_source_and_decision_contract(monkeypatch):
    fake_client = _FakeFirestoreClient()
    monkeypatch.setattr(ingestion_store.firestore, "Client", lambda: fake_client)
    monkeypatch.setattr(ingestion_store.firestore, "transactional", lambda function: function)
    store = ingestion_store.FirestoreIngestionStore()

    base_time = datetime.now(timezone.utc) - timedelta(hours=2)
    healthy = store.report_source(SourceReport(**_source_report(checked_at=base_time.isoformat())))
    assert healthy.status.value == "healthy"
    assert healthy.last_success_at == healthy.last_checked_at

    failed = store.report_source(
        SourceReport(
            **_source_report(
                checked_at=(base_time + timedelta(hours=1)).isoformat(),
                status="unavailable",
                error_code="network",
                error_message="Timed out",
                items_checked=0,
                candidates=0,
                created=0,
                duplicates=0,
                ignored=0,
            )
        )
    )
    assert failed.status.value == "unavailable"
    assert failed.last_success_at == healthy.last_success_at
    assert failed.last_error_at == failed.last_checked_at
    assert store.list_source_states()[0].source_id == "gmail:personal"

    stale = store.report_source(
        SourceReport(
            **_source_report(
                checked_at=(base_time - timedelta(hours=1)).isoformat(),
                status="healthy",
            )
        )
    )
    assert stale.status.value == "unavailable"
    assert stale.last_checked_at == failed.last_checked_at

    payload = IngestionDecisionWrite(**_decision())
    recorded = store.record_decision(payload)
    fetched = store.get_decision(payload.source_id, payload.source_item_id, payload.content_fingerprint)
    assert fetched == recorded
    assert ingestion_store.decision_should_process(fetched) is False
    batch = store.get_decisions(
        payload.source_id,
        [
            ingestion_store.IngestionCheckItem(
                source_item_id=payload.source_item_id,
                content_fingerprint=payload.content_fingerprint,
            )
        ],
    )
    assert batch[(payload.source_item_id, payload.content_fingerprint)] == recorded

    changed = store.get_decision(payload.source_id, payload.source_item_id, "sha256:changed")
    assert changed is None


def test_firestore_keys_are_unambiguous_for_arbitrary_source_ids():
    assert ingestion_store._firestore_key("a\0b", "c", "d") != ingestion_store._firestore_key("a", "b\0c", "d")
