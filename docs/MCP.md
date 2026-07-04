# MCP Server

Personal Task Assistant ships an MCP (Model Context Protocol) server at
`adapters/task_assistant_mcp.py`. It wraps the JSON API, so any MCP client —
Claude Code, Claude Desktop, or another agent runtime — can work with the task
queue directly, without a custom adapter.

The server is a thin API client: it does not touch the database and needs a
running Personal Task Assistant instance (Local Mode or hosted).

## Tools

| Tool | What it does |
| --- | --- |
| `task_assistant_get_queue` | Queue metrics + ranked task list with filters |
| `task_assistant_queue_summary` | Metrics only — cheapest polling call |
| `task_assistant_claim_task` | Atomically take the next codex backlog task |
| `task_assistant_finish_task` | Hand finished work back as `waiting_review` |
| `task_assistant_create_task` | Create one task (external_id supported) |
| `task_assistant_update_task` | Change status, owner, priority, dates, text |
| `task_assistant_list_tasks` | Board-ordered list with pagination |
| `task_assistant_ingest_context` | Turn source context into a task batch, idempotent by external_id |
| `task_assistant_due_reminders` | Active tasks past their deadline or reminder |

The intended agent loop mirrors the product workflow: `claim_task` ->
do the work -> `finish_task` (waiting_review), so the human always reviews
agent output before a task is closed.

## Configuration

Environment variables:

```bash
TASK_TRACKER_URL=http://127.0.0.1:8000   # default
TASK_TRACKER_API_KEY=<the instance API key>  # required
```

Install dependencies (the `mcp` package is part of `requirements.txt`):

```bash
pip install -r requirements.txt
```

## Register in Claude Code

```bash
claude mcp add task-assistant \
  -e TASK_TRACKER_URL=http://127.0.0.1:8000 \
  -e TASK_TRACKER_API_KEY=<key> \
  -- /path/to/repo/.venv/bin/python /path/to/repo/adapters/task_assistant_mcp.py
```

## Register in Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "task-assistant": {
      "command": "/path/to/repo/.venv/bin/python",
      "args": ["/path/to/repo/adapters/task_assistant_mcp.py"],
      "env": {
        "TASK_TRACKER_URL": "http://127.0.0.1:8000",
        "TASK_TRACKER_API_KEY": "<key>"
      }
    }
  }
}
```

## Try it

With the app running locally, ask the connected agent something like:

- "What's in my task queue?" (`task_assistant_get_queue`)
- "Claim the next task and do it." (`task_assistant_claim_task` -> work -> `task_assistant_finish_task`)
- "Turn this email into tasks: ..." (`task_assistant_ingest_context`)

## Safety

- The server holds the instance API key — treat its env config like any other
  adapter credential and never commit it.
- Claiming and finishing tasks are write operations; the human review loop
  (`waiting_review`) keeps a person in control of closing work.
- The server only talks to `TASK_TRACKER_URL`; it makes no other network calls.
