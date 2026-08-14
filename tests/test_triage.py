FAR_DUE = "2035-01-01T00:00:00Z"
PAST_DUE = "2020-01-01T00:00:00Z"


def _triage_next(client, api_headers, **params):
    response = client.post("/api/agent/triage/next", params=params, headers=api_headers)
    assert response.status_code == 200, response.text
    return response.json()


def _triage_apply(client, api_headers, resolutions):
    response = client.post(
        "/api/agent/triage/apply", json={"resolutions": resolutions}, headers=api_headers
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_next_returns_smart_ordered_batch_and_marks_shown(client, api_headers, make_task):
    urgent = make_task(title="urgent", priority=1, due_at=PAST_DUE)
    make_task(title="normal", priority=3, due_at=FAR_DUE)
    outside_batch = make_task(title="later", priority=5, due_at=FAR_DUE)

    body = _triage_next(client, api_headers, limit=2)
    titles = [card["title"] for card in body["cards"]]
    assert titles == ["urgent", "normal"]
    assert body["cards"][0]["overdue"] is True
    assert body["cards"][0]["last_shown_at"] is not None
    assert body["skipped_recently_shown"] == 0
    assert body["summary"]["active"] == 3

    shown = client.get(f"/api/tasks/{urgent['id']}", headers=api_headers).json()
    assert shown["last_shown_at"] is not None
    not_shown = client.get(f"/api/tasks/{outside_batch['id']}", headers=api_headers).json()
    assert not_shown["last_shown_at"] is None


def test_next_excludes_recently_shown_until_cooldown(client, api_headers, make_task):
    make_task(title="first", priority=1, due_at=FAR_DUE)
    make_task(title="second", priority=2, due_at=FAR_DUE)
    make_task(title="third", priority=3, due_at=FAR_DUE)

    first_batch = _triage_next(client, api_headers, limit=2)
    assert [card["title"] for card in first_batch["cards"]] == ["first", "second"]

    second_batch = _triage_next(client, api_headers, limit=2)
    assert [card["title"] for card in second_batch["cards"]] == ["third"]
    assert second_batch["skipped_recently_shown"] == 2

    ignoring_cooldown = _triage_next(client, api_headers, limit=3, cooldown_hours=0)
    assert len(ignoring_cooldown["cards"]) == 3
    assert ignoring_cooldown["skipped_recently_shown"] == 0


def test_next_with_mark_false_is_a_peek(client, api_headers, make_task):
    task = make_task(title="peek", priority=1, due_at=FAR_DUE)

    body = _triage_next(client, api_headers, limit=1, mark="false")
    assert [card["title"] for card in body["cards"]] == ["peek"]

    fetched = client.get(f"/api/tasks/{task['id']}", headers=api_headers).json()
    assert fetched["last_shown_at"] is None

    again = _triage_next(client, api_headers, limit=1)
    assert [card["title"] for card in again["cards"]] == ["peek"]


def test_marking_shown_does_not_bump_updated_at(client, api_headers, make_task):
    task = make_task(title="stable", priority=1, due_at=FAR_DUE)
    before = client.get(f"/api/tasks/{task['id']}", headers=api_headers).json()

    _triage_next(client, api_headers, limit=1)

    after = client.get(f"/api/tasks/{task['id']}", headers=api_headers).json()
    assert after["updated_at"] == before["updated_at"]
    assert after["last_shown_at"] is not None


def test_apply_resolves_a_mixed_batch_in_one_call(client, api_headers, make_task):
    to_close = make_task(title="close me", description="context", due_at=FAR_DUE)
    to_cancel = make_task(title="cancel me", due_at=FAR_DUE)
    to_defer = make_task(title="defer me", due_at=PAST_DUE)
    to_assign = make_task(title="assign me", due_at=FAR_DUE)

    body = _triage_apply(
        client,
        api_headers,
        [
            {"id": to_close["id"], "action": "done", "note": "оплата пришла"},
            {"id": to_cancel["id"], "action": "cancel"},
            {"id": to_defer["id"], "action": "defer", "due_at": FAR_DUE},
            {"id": to_assign["id"], "action": "assign", "assignee": "codex"},
        ],
    )
    assert body["applied"] == 4
    assert body["failed"] == 0
    by_id = {item["id"]: item for item in body["results"]}

    closed = by_id[to_close["id"]]["task"]
    assert closed["status"] == "done"
    assert closed["completed_at"] is not None
    assert "context" in closed["description"]
    assert "(triage): оплата пришла" in closed["description"]

    assert by_id[to_cancel["id"]]["task"]["status"] == "cancelled"
    assert by_id[to_defer["id"]]["task"]["due_at"].startswith("2035-01-01")
    assert by_id[to_assign["id"]]["task"]["assignee"] == "codex"

    assert body["summary"]["active"] == 2
    assert body["summary"]["overdue"] == 0


def test_apply_continues_past_bad_items(client, api_headers, make_task):
    good = make_task(title="good", due_at=FAR_DUE)
    body = _triage_apply(
        client,
        api_headers,
        [
            {"id": 999999, "action": "done"},
            {"id": good["id"], "action": "defer"},
            {"id": good["id"], "action": "done"},
        ],
    )
    assert body["applied"] == 1
    assert body["failed"] == 2
    errors = {item["error"] for item in body["results"] if not item["ok"]}
    assert "Task not found" in errors
    assert any("defer requires" in error for error in errors)
    assert body["results"][2]["task"]["status"] == "done"


def test_apply_update_requires_some_change(client, api_headers, make_task):
    task = make_task(title="noop", due_at=FAR_DUE)
    body = _triage_apply(client, api_headers, [{"id": task["id"], "action": "update"}])
    assert body["applied"] == 0
    assert "at least one field" in body["results"][0]["error"]

    noted = _triage_apply(
        client, api_headers, [{"id": task["id"], "action": "update", "note": "просто заметка"}]
    )
    assert noted["applied"] == 1
    assert "(triage): просто заметка" in noted["results"][0]["task"]["description"]
