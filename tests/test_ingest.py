INGEST_PAYLOAD = {
    "origin": "telegram",
    "source_name": "Telegram test chat",
    "source_url": "https://t.me/test/42",
    "source_context": "codex: do the thing",
    "tasks": [
        {"title": "Deduped task", "assignee": "codex", "priority": 2, "external_id": "telegram:1:42:0"},
        {"title": "Plain task", "assignee": "me"},
    ],
}


def test_ingest_creates_tasks_with_source_fields(client, api_headers):
    response = client.post("/api/agent/ingest/context", json=INGEST_PAYLOAD, headers=api_headers)
    assert response.status_code == 200
    body = response.json()
    assert len(body["created"]) == 2
    assert body["duplicates"] == []
    for task in body["created"]:
        assert task["origin"] == "telegram"
        assert task["source_name"] == "Telegram test chat"
        assert task["source_url"] == "https://t.me/test/42"
        assert task["source_context"] == "codex: do the thing"


def test_ingest_deduplicates_by_external_id(client, api_headers):
    first = client.post("/api/ingest/context", json=INGEST_PAYLOAD, headers=api_headers).json()
    original_id = next(task["id"] for task in first["created"] if task["external_id"] == "telegram:1:42:0")

    second = client.post("/api/ingest/context", json=INGEST_PAYLOAD, headers=api_headers).json()
    assert [task["title"] for task in second["created"]] == ["Plain task"]
    assert [task["id"] for task in second["duplicates"]] == [original_id]

    all_tasks = client.get("/api/tasks", headers=api_headers).json()
    external_ids = [task["external_id"] for task in all_tasks if task["external_id"]]
    assert external_ids == ["telegram:1:42:0"]


def test_item_source_fields_override_payload_defaults(client, api_headers):
    payload = {
        "origin": "email",
        "source_name": "Inbox",
        "tasks": [{"title": "custom source", "source_name": "Specific thread"}],
    }
    created = client.post("/api/ingest/context", json=payload, headers=api_headers).json()["created"]
    assert created[0]["source_name"] == "Specific thread"
    assert created[0]["origin"] == "email"
