import pytest

import app.store as store_module
from app.schemas import TaskCreate, TaskUpdate


class _Snapshot:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data) if self._data is not None else None

    def get(self, key):
        return (self._data or {}).get(key)


class _Document:
    def __init__(self, documents, document_id):
        self.documents = documents
        self.document_id = document_id

    def get(self, transaction=None):
        return _Snapshot(self.documents.get(self.document_id))

    def set(self, data, merge=False):
        if merge and self.document_id in self.documents:
            self.documents[self.document_id].update(data)
        else:
            self.documents[self.document_id] = dict(data)

    def update(self, data):
        self.documents[self.document_id].update(data)

    def delete(self):
        self.documents.pop(self.document_id, None)


class _Collection:
    def __init__(self):
        self.documents = {}

    def document(self, document_id):
        return _Document(self.documents, document_id)

    def where(self, field, operator, value):
        assert operator == "=="
        return _Query(self.documents, field, value)


class _Query:
    def __init__(self, documents, field, value):
        self.documents = documents
        self.field = field
        self.value = value
        self.limit_value = None

    def limit(self, value):
        self.limit_value = value
        return self

    def stream(self):
        matches = [data for data in self.documents.values() if data.get(self.field) == self.value]
        if self.limit_value is not None:
            matches = matches[: self.limit_value]
        return [_Snapshot(data) for data in matches]


class _Transaction:
    def set(self, document, data, merge=False):
        document.set(data, merge=merge)

    def update(self, document, data):
        document.update(data)

    def delete(self, document):
        document.delete()


class _Client:
    def __init__(self):
        self.collections = {}
        self.before_transaction = None

    def collection(self, name):
        return self.collections.setdefault(name, _Collection())

    def transaction(self):
        if self.before_transaction is not None:
            callback = self.before_transaction
            self.before_transaction = None
            callback()
        return _Transaction()


def _firestore_store(monkeypatch, *, skip_compatibility_query=True):
    client = _Client()
    monkeypatch.setattr(store_module.firestore, "Client", lambda: client)
    monkeypatch.setattr(store_module.firestore, "transactional", lambda function: function)
    store = store_module.FirestoreTaskStore()
    # Force the transaction-backed reservation path to prove it does not rely
    # on the legacy query-then-create compatibility lookup.
    if skip_compatibility_query:
        monkeypatch.setattr(store, "get_task_by_external_id", lambda _external_id: None)
    return store, client


def test_firestore_external_id_reservation_prevents_duplicate_create(monkeypatch):
    store, client = _firestore_store(monkeypatch)
    payload = TaskCreate(title="Only once", external_id="telegram:source:item")

    first = store.create_task(payload)
    with pytest.raises(store_module.DuplicateExternalIdError):
        store.create_task(payload)

    assert first.external_id == payload.external_id
    assert len(client.collections["tasks"].documents) == 1
    assert len(client.collections["task_external_ids"].documents) == 1


def test_firestore_external_id_mapping_tracks_update_and_delete(monkeypatch):
    store, client = _firestore_store(monkeypatch)
    first = store.create_task(TaskCreate(title="First", external_id="external:first"))
    second = store.create_task(TaskCreate(title="Second", external_id="external:second"))

    with pytest.raises(store_module.DuplicateExternalIdError):
        store.update_task(second.id, TaskUpdate(external_id="external:first"))

    def concurrent_change_to_intermediate_id():
        tasks = client.collections["tasks"].documents
        mappings = client.collections["task_external_ids"].documents
        tasks[str(first.id)]["external_id"] = "external:intermediate"
        mappings.pop(store_module._external_id_key("external:first"), None)
        mappings[store_module._external_id_key("external:intermediate")] = {
            "external_id": "external:intermediate",
            "task_id": first.id,
        }

    client.before_transaction = concurrent_change_to_intermediate_id
    changed = store.update_task(first.id, TaskUpdate(external_id="external:changed"))
    assert changed is not None
    assert changed.external_id == "external:changed"
    mappings = client.collections["task_external_ids"].documents.values()
    assert {mapping["external_id"] for mapping in mappings} == {"external:changed", "external:second"}

    def concurrent_change_before_delete():
        tasks = client.collections["tasks"].documents
        mappings = client.collections["task_external_ids"].documents
        tasks[str(first.id)]["external_id"] = "external:latest"
        mappings.pop(store_module._external_id_key("external:changed"), None)
        mappings[store_module._external_id_key("external:latest")] = {
            "external_id": "external:latest",
            "task_id": first.id,
        }

    client.before_transaction = concurrent_change_before_delete
    assert store.delete_task(first.id) is True
    assert len(client.collections["tasks"].documents) == 1
    mappings = client.collections["task_external_ids"].documents.values()
    assert {mapping["external_id"] for mapping in mappings} == {"external:second"}


def test_firestore_update_detects_legacy_task_without_reservation(monkeypatch):
    store, client = _firestore_store(monkeypatch, skip_compatibility_query=False)
    legacy = store.create_task(TaskCreate(title="Legacy", external_id="external:legacy"))
    client.collections["task_external_ids"].documents.pop(
        store_module._external_id_key("external:legacy"),
        None,
    )
    second = store.create_task(TaskCreate(title="Second", external_id="external:second"))

    with pytest.raises(store_module.DuplicateExternalIdError):
        store.update_task(second.id, TaskUpdate(external_id="external:legacy"))
    assert store.get_task(legacy.id).external_id == "external:legacy"
    assert store.get_task(second.id).external_id == "external:second"
