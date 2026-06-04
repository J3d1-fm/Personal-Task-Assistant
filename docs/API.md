# API

All `/api/*` endpoints require `Authorization: Bearer <TASK_TRACKER_API_KEY>` or
an authenticated browser session. Do not expose an instance without a real API
key and session secret.

If `due_at` is omitted when a task is created, the server estimates DD from
priority:

- P1: about 1 day
- P2: about 3 days
- P3: about 7 days
- P4: about 14 days
- P5: about 30 days

## Create a Task

```bash
curl -X POST "$TASK_TRACKER_URL/api/tasks" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Reply to partner about contract",
    "description": "Extracted from Slack thread.",
    "assignee": "me",
    "origin": "slack",
    "priority": 2,
    "source_name": "Slack #partners",
    "source_url": "https://example.slack.com/archives/...",
    "due_at": "2026-05-08T15:00:00Z",
    "reminder_at": "2026-05-06T08:00:00Z"
  }'
```

Use `/api/agent/tasks` for the same create contract when the caller is another
agent. The endpoint records the task event with the `agent_api` note.

```bash
curl -X POST "$TASK_TRACKER_URL/api/agent/tasks" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Audit stale tasks in the PM queue",
    "assignee": "codex",
    "origin": "codex",
    "priority": 2
  }'
```

## Ingest Tasks From Context

Codex should read Slack, Telegram, Gmail, or local context, extract concrete tasks, and
send normalized records:

```bash
curl -X POST "$TASK_TRACKER_URL/api/ingest/context" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "origin": "email",
    "source_name": "Gmail thread from Alice",
    "source_url": "https://mail.google.com/...",
    "source_context": "Alice asked for revised numbers by Friday.",
    "tasks": [
      {
        "title": "Send Alice revised numbers",
        "assignee": "me",
        "priority": 2,
        "reminder_at": "2026-05-07T09:00:00Z"
      }
    ]
  }'
```

The same context-ingest contract is also available at
`POST /api/agent/ingest/context` for external agent clients.

## Read Agent Queue

Use the agent queue endpoint when another agent needs the next execution list
without scraping the UI:

```bash
curl "$TASK_TRACKER_URL/api/agent/queue?assignee=codex&sort=smart&limit=25" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY"
```

Supported query parameters:

- `status`: `backlog`, `in_progress`, `waiting_review`, `blocked`, `done`, or `cancelled`
- `assignee`: `me`, `codex`, or `unassigned`
- `include_done`: `true` or `false`
- `limit`: 1-250
- `sort`: `smart`, `due`, `created`, `updated`, `priority`, or `owner`

Response shape:

```json
{
  "summary": {
    "active": 42,
    "overdue": 3,
    "due_soon": 4,
    "codex_ready": 12,
    "human_input": 18,
    "review": 5,
    "blocked": 2,
    "unassigned": 1
  },
  "tasks": []
}
```

## Update Status or Assignment

```bash
curl -X PATCH "$TASK_TRACKER_URL/api/tasks/1" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"assignee": "codex", "status": "in_progress"}'
```

Task status values are `backlog`, `in_progress`, `waiting_review`, `blocked`, `done`, and `cancelled`.

## Delete a Task

```bash
curl -X DELETE "$TASK_TRACKER_URL/api/tasks/1" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY"
```
