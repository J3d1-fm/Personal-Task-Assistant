# Public Examples

These examples show the intended product loop: external context enters the
assistant, an adapter or AI agent extracts concrete tasks, and the shared queue
separates agent-executable work from human-dependent work.

## Example 1: Telegram Context

Raw message:

```text
codex: check why the Cloud Run deploy failed and open a PR if the fix is safe
me: approve the production deploy after Codex posts the diff
review: inspect the README demo copy before publishing
todo: post the project to LinkedIn and Hacker News
```

Adapter output:

| Task | Owner | Status | Priority | Source |
| --- | --- | --- | --- | --- |
| Check why the Cloud Run deploy failed and open a PR if the fix is safe | Codex | Backlog | P2 | Telegram |
| Approve the production deploy after Codex posts the diff | Me | Backlog | P2 | Telegram |
| Inspect the README demo copy before publishing | Me | Waiting review | P2 | Telegram |
| Post the project to LinkedIn and Hacker News | Unassigned | Backlog | P3 | Telegram |

Agent queue effect:

- The Codex-owned deploy investigation appears in `Agent queue`.
- The approval and review tasks appear in `Human input`.
- DD is estimated automatically when the message does not include one.
- The original Telegram text stays on the task as source context.

## Example 2: Agent Takes Work

An AI agent reads:

```http
GET /api/agent/queue?assignee=codex&sort=smart&limit=10
Authorization: Bearer $TASK_TRACKER_API_KEY
```

The agent chooses the highest priority task it can do without a human:

```json
{
  "title": "Check why the Cloud Run deploy failed and open a PR if the fix is safe",
  "assignee": "codex",
  "status": "backlog",
  "priority": 2,
  "origin": "telegram"
}
```

Then it marks the task as active:

```http
PATCH /api/tasks/42
Authorization: Bearer $TASK_TRACKER_API_KEY
Content-Type: application/json

{"status": "in_progress", "assignee": "codex"}
```

When complete, the agent moves it to review instead of closing it silently:

```http
PATCH /api/tasks/42
Authorization: Bearer $TASK_TRACKER_API_KEY
Content-Type: application/json

{"status": "waiting_review"}
```

## Example 3: Jira Or Asana Adapter Shape

External task trackers can follow the same contract. A user-built adapter can
receive a webhook event or poll an API, then call:

```http
POST /api/agent/ingest/context
Authorization: Bearer $TASK_TRACKER_API_KEY
Content-Type: application/json
```

```json
{
  "origin": "other",
  "source_name": "Jira PTA-24",
  "source_url": "https://example.atlassian.net/browse/PTA-24",
  "source_context": "User asked for Slack import support and a safer setup wizard.",
  "tasks": [
    {
      "title": "Design Slack import adapter contract",
      "assignee": "codex",
      "priority": 2
    },
    {
      "title": "Approve Slack workspace permission scope",
      "assignee": "me",
      "priority": 1
    }
  ]
}
```

The adapter owns source credentials. Personal Task Assistant owns the normalized
queue, priority, DD defaults, and human/agent execution surface.
