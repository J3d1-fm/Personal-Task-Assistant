PAST_DUE = "2020-01-01T00:00:00Z"
FAR_DUE = "2035-01-01T00:00:00Z"


def _seed_board(client, api_headers, make_task):
    overdue_codex = make_task(title="overdue codex", assignee="codex", priority=2, due_at=PAST_DUE)
    plain_codex = make_task(title="plain codex", assignee="codex", priority=2, due_at=FAR_DUE)
    my_task = make_task(title="mine", assignee="me", priority=1, due_at=FAR_DUE)
    review_task = make_task(title="review", status="waiting_review", due_at=FAR_DUE)
    blocked_task = make_task(title="blocked", status="blocked", assignee="me", due_at=FAR_DUE)
    done_task = make_task(title="done")
    client.patch(f"/api/tasks/{done_task['id']}", json={"status": "done"}, headers=api_headers)
    return overdue_codex, plain_codex, my_task, review_task, blocked_task, done_task


def test_queue_summary_counts(client, api_headers, make_task):
    _seed_board(client, api_headers, make_task)
    body = client.get("/api/agent/queue", headers=api_headers).json()
    summary = body["summary"]
    assert summary == {
        "active": 5,
        "overdue": 1,
        "due_soon": 0,
        "codex_ready": 2,
        "human_input": 3,
        "review": 1,
        "blocked": 1,
        "unassigned": 1,
    }
    titles = [task["title"] for task in body["tasks"]]
    assert "done" not in titles


def test_summary_endpoint_matches_queue(client, api_headers, make_task):
    _seed_board(client, api_headers, make_task)
    queue_summary = client.get("/api/agent/queue", headers=api_headers).json()["summary"]
    summary = client.get("/api/agent/queue/summary", headers=api_headers).json()
    assert summary == queue_summary


def test_smart_sort_prefers_urgent_work(client, api_headers, make_task):
    _seed_board(client, api_headers, make_task)
    titles = [task["title"] for task in client.get("/api/agent/queue", headers=api_headers).json()["tasks"]]
    assert titles.index("mine") < titles.index("overdue codex") < titles.index("plain codex")


def test_queue_filters_and_limit(client, api_headers, make_task):
    _seed_board(client, api_headers, make_task)
    codex_only = client.get("/api/agent/queue?assignee=codex", headers=api_headers).json()["tasks"]
    assert {task["assignee"] for task in codex_only} == {"codex"}

    limited = client.get("/api/agent/queue?limit=2", headers=api_headers).json()["tasks"]
    assert len(limited) == 2

    done_tasks = client.get("/api/agent/queue?include_done=true&status=done", headers=api_headers).json()["tasks"]
    assert [task["title"] for task in done_tasks] == ["done"]


def test_claim_takes_ranked_codex_backlog(client, api_headers, make_task):
    _seed_board(client, api_headers, make_task)

    first = client.post("/api/agent/claim", headers=api_headers)
    assert first.status_code == 200
    assert first.json()["title"] == "overdue codex"
    assert first.json()["status"] == "in_progress"

    second = client.post("/api/agent/claim", headers=api_headers)
    assert second.json()["title"] == "plain codex"

    third = client.post("/api/agent/claim", headers=api_headers)
    assert third.status_code == 404

    mine = client.get("/api/agent/queue?assignee=me&status=backlog", headers=api_headers).json()["tasks"]
    assert [task["title"] for task in mine] == ["mine"]


def test_claim_ignores_non_backlog_codex_tasks(client, api_headers, make_task):
    task = make_task(title="already running", assignee="codex", status="in_progress")
    response = client.post("/api/agent/claim", headers=api_headers)
    assert response.status_code == 404
    refreshed = client.get("/api/agent/queue?assignee=codex", headers=api_headers).json()["tasks"]
    assert [item["id"] for item in refreshed] == [task["id"]]
