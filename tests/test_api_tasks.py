from datetime import datetime, timedelta, timezone


def _parse(value: str) -> datetime:
    # SQLite returns naive datetimes; the app treats them as UTC.
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def test_api_requires_auth(client):
    assert client.get("/api/tasks").status_code == 401
    assert client.post("/api/tasks", json={"title": "x"}).status_code == 401
    assert client.get("/api/tasks", headers={"Authorization": "Bearer wrong-key"}).status_code == 401


def test_x_api_key_header_accepted(client):
    response = client.get("/api/tasks", headers={"x-api-key": "test-api-key"})
    assert response.status_code == 200


def test_create_task_defaults(make_task):
    task = make_task(title="Write report")
    assert task["status"] == "backlog"
    assert task["assignee"] == "unassigned"
    assert task["priority"] == 3
    assert task["due_at"] is not None


def test_due_default_follows_priority(make_task):
    now = datetime.now(timezone.utc)
    for priority, days in [(1, 1), (2, 3), (3, 7), (4, 14), (5, 30)]:
        task = make_task(title=f"p{priority}", priority=priority)
        assert abs((_parse(task["due_at"]) - now) - timedelta(days=days)) < timedelta(hours=1)


def test_explicit_due_is_preserved(make_task):
    task = make_task(title="fixed due", due_at="2030-01-02T10:00:00Z")
    assert _parse(task["due_at"]) == datetime(2030, 1, 2, 10, 0, tzinfo=timezone.utc)


def test_list_excludes_done_by_default(client, api_headers, make_task):
    keep = make_task(title="active")
    done = make_task(title="finished")
    client.patch(f"/api/tasks/{done['id']}", json={"status": "done"}, headers=api_headers)

    titles = [task["title"] for task in client.get("/api/tasks", headers=api_headers).json()]
    assert titles == [keep["title"]]

    with_done = client.get("/api/tasks?include_done=true", headers=api_headers).json()
    assert {task["title"] for task in with_done} == {"active", "finished"}


def test_patch_done_sets_and_clears_completed_at(client, api_headers, make_task):
    task = make_task(title="lifecycle")
    done = client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=api_headers).json()
    assert done["completed_at"] is not None

    reopened = client.patch(f"/api/tasks/{task['id']}", json={"status": "backlog"}, headers=api_headers).json()
    assert reopened["completed_at"] is None


def test_patch_and_delete_unknown_task(client, api_headers):
    assert client.patch("/api/tasks/99999", json={"title": "x"}, headers=api_headers).status_code == 404
    assert client.delete("/api/tasks/99999", headers=api_headers).status_code == 404


def test_delete_task(client, api_headers, make_task):
    task = make_task(title="to delete")
    assert client.delete(f"/api/tasks/{task['id']}", headers=api_headers).json() == {"deleted": True}
    assert client.get("/api/tasks", headers=api_headers).json() == []


def test_pagination(client, api_headers, make_task):
    for index in range(5):
        make_task(title=f"task {index}", priority=3)
    assert len(client.get("/api/tasks?limit=2", headers=api_headers).json()) == 2
    assert len(client.get("/api/tasks?limit=3&offset=3", headers=api_headers).json()) == 2
    assert len(client.get("/api/tasks", headers=api_headers).json()) == 5


def test_duplicate_external_id_conflicts(client, api_headers, make_task):
    make_task(title="first", external_id="ext:1")
    response = client.post("/api/tasks", json={"title": "second", "external_id": "ext:1"}, headers=api_headers)
    assert response.status_code == 409

    agent_response = client.post(
        "/api/agent/tasks", json={"title": "third", "external_id": "ext:1"}, headers=api_headers
    )
    assert agent_response.status_code == 409
