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

## Connector Adapters

Personal Task Assistant exposes a connector contract; it does not include
ready-made credentials or source-specific clients for Jira, Asana, YouTrack,
Linear, Trello, Slack, Telegram, email, or similar systems.

Users should implement their own adapters for each source:

- Webhook adapters receive events from task trackers or chat tools and call
  `POST /api/agent/ingest/context`.
- Polling adapters periodically read source APIs and call `POST /api/agent/tasks`
  or `POST /api/ingest/context`.
- Sync adapters read `GET /api/agent/queue` and update external trackers through
  the external tool's own API.

Keep credentials in the adapter runtime or secret manager. Do not send API keys,
OAuth tokens, service-account files, or raw private exports into the repository.

The repository includes a safe Telegram polling adapter as a reference
implementation. It demonstrates the contract without requiring a public webhook
URL or any committed credentials:

```bash
python3 adapters/telegram_polling_adapter.py --once --dry-run
```

See `adapters/README.md`.

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

A task may carry an optional `external_id` (up to 200 characters) that uniquely
identifies it in the source system, for example
`telegram:<source_hash>:<chat_id>:<message_id>:<task_hash>:<occurrence>`. Creating a second task with the same
`external_id` returns `409 Conflict`, and context ingest reports it as a
duplicate instead of failing, so adapter retries stay safe.

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

The response contains `created` (new tasks) and `duplicates` (existing tasks
matched by `external_id`). Sending the same batch twice creates nothing on the
second call and returns the original tasks in `duplicates`.

## Report Source Health

Adapters should perform a real least-privilege source read, then report the
result. The core app never stores source credentials or calls external auth
endpoints itself.

```bash
curl -X POST "$TASK_TRACKER_URL/api/agent/sources/report" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "gmail:personal",
    "source_name": "Personal Gmail",
    "adapter_type": "gmail_polling",
    "status": "healthy",
    "items_checked": 42,
    "candidates": 3,
    "created": 1,
    "duplicates": 1,
    "ignored": 1,
    "suppressed": 0
  }'
```

Statuses are `healthy`, `degraded`, `unavailable`, `reauth_required`,
`misconfigured`, `rate_limited`, and `failed`. Failure reports may include a
sanitized `error_code`, `error_message`, and an absolute HTTP(S) `action_url`.
Optional `checked_at` timestamps are normalized to UTC, may be at most five
minutes in the future, and are applied monotonically so a late old report cannot
replace newer health.

Read the latest state through `GET /api/sources` for the web/user surface or
`GET /api/agent/sources` for an agent client.

## Check And Record Ingestion Decisions

Before parsing or creating tasks, an adapter can ask whether an exact source
item/fingerprint pair was already handled:

```bash
curl -X POST "$TASK_TRACKER_URL/api/agent/ingestion/check" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "slack:work",
    "items": [{
      "source_item_id": "thread:C123:1710000000.000100",
      "content_fingerprint": "8a86..."
    }]
  }'
```

Record the resulting decision:

```bash
curl -X POST "$TASK_TRACKER_URL/api/agent/ingestion/decisions" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "source_id": "slack:work",
    "source_item_id": "thread:C123:1710000000.000100",
    "content_fingerprint": "8a86...",
    "decision": "ignored",
    "reason": "FYI only; no action requested",
    "decided_by": "adapter"
  }'
```

Decisions are `created`, `duplicate`, `ignored`, `needs_review`, and `failed`.
All except `failed` suppress unchanged content. `revisit_after` can make a
decision retryable later; a changed fingerprint is immediately processable.
See `docs/ADAPTER_CONTRACT.md` for the full lifecycle and safety rules.

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

`GET /api/tasks` additionally supports `limit` (1-500) and `offset` for paging
through large boards.

`GET /api/tasks/{id}` returns a single task (404 when the id is unknown) —
useful for read-modify-write flows such as appending a note to the
description:

```bash
curl "$TASK_TRACKER_URL/api/tasks/42" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY"
```

## Poll the Queue Cheaply

`GET /api/agent/queue/summary` returns only the summary block above. Use it for
frequent polling loops; it reads only active tasks and skips the task list and
sorting entirely:

```bash
curl "$TASK_TRACKER_URL/api/agent/queue/summary" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY"
```

## Claim the Next Task

`POST /api/agent/claim` atomically assigns the top-ranked claimable task to the
calling agent: it picks the best `assignee=codex`, `status=backlog` task by
smart rank, transitions it to `in_progress`, and returns it. The transition is
conditional in the store (SQL conditional update, Firestore transaction), so
two agents claiming concurrently always receive different tasks. When nothing
is claimable the endpoint returns `404`.

```bash
curl -X POST "$TASK_TRACKER_URL/api/agent/claim" \
  -H "Authorization: Bearer $TASK_TRACKER_API_KEY"
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
