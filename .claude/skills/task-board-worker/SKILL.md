---
name: task-board-worker
description: Operate the Personal Task Assistant board as the autonomous worker — the claim -> work -> finish-to-review loop, ingesting messy context into tasks, and reporting through the queue. Use this whenever you are asked to "work the queue", "take the next task", "разбери задачи", triage someone's inbox/chat into tasks, or whenever task_assistant_* MCP tools are available and the user wants work done rather than code changed. Read it BEFORE claiming or creating any task, even for a single one-off task.
---

# Working the task board

You are acting as the human's second worker. The board is the shared contract:
the human decides, you execute, and everything you finish comes back to the
human for review. Breaking that rhythm (closing tasks yourself, working
without claiming, creating duplicates) silently destroys the human's trust in
the whole board, so the loop below matters more than speed.

## The loop

1. `task_assistant_queue_summary` — one line of counters. Cheap; call it
   first and between tasks instead of pulling full lists.
2. `task_assistant_claim_task` — atomically takes the top-ranked codex-owned
   backlog task and moves it to `in_progress`. Never pick work by reading the
   list and PATCHing yourself: claim is what prevents two agents from taking
   the same task. "No claimable task" is a normal outcome, not an error.
3. Do the actual work the task describes. The task's `description`,
   `source_context`, and `source_url` carry the human's original context —
   read them before acting.
4. `task_assistant_finish_task` — hands the result back as `waiting_review`.
   Put a short summary of what you did wherever the work itself lives (the PR,
   the document, the reply); the board only tracks state.
5. Repeat from step 1 until the codex backlog is empty, then report: what you
   finished (now in review), what you saw blocked, what needs the human
   (`needs human` counter).

## Rules that are easy to get wrong

- **Never set `status=done` yourself.** `done` means the human accepted the
  work. Your terminal state is `waiting_review` (that is what
  `finish_task` does). Only mark `done` when the human explicitly says the
  task is complete.
- **Stuck mid-task?** `task_assistant_update_task` with `status="blocked"` and
  keep it assigned to you; explain the blocker in your report. Do not silently
  return it to backlog.
- **Creating tasks from context** (chat dumps, emails, meeting notes): use
  `task_assistant_ingest_context` with one call per source, and give every
  task an `external_id` derived from the source (e.g.
  `telegram:<chat>:<message>:<line>`, `email:<message-id>:<n>`). That makes
  retries and repeated ingests safe — duplicates are reported, not created.
- **Owner discipline**: `assignee="codex"` only for work an agent can actually
  execute end to end; decisions, approvals, purchases, anything with real-world
  side effects the human must own → `assignee="me"`.
- **Priorities set deadlines.** If you omit `due_at`, the server estimates it
  from priority (P1 ≈ 1 day … P5 ≈ 30 days). Do not inflate priority to make
  something look important; P1-P2 should be rare.
- **Nudging**: `task_assistant_due_reminders` lists active tasks past their
  deadline or reminder time — use it when asked "what's overdue" or when
  composing a status report for the human.

## Without MCP

The same contract over REST (`Authorization: Bearer <TASK_TRACKER_API_KEY>`):
`GET /api/agent/queue/summary`, `POST /api/agent/claim`,
`PATCH /api/tasks/{id}` (`{"status": "waiting_review"}`),
`POST /api/agent/ingest/context`, `GET /api/reminders/due`. See `docs/API.md`.
