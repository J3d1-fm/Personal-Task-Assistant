# PM Guide

This guide describes how to use Personal Task Assistant as a lightweight PM
queue for work shared between a person and an AI agent.

## Purpose

Use the assistant as the single place for actionable work that comes from Slack,
Gmail, Telegram, meetings, or manual notes.

The tracker is not intended to replace full project management tools. It is for:

- personal follow-ups;
- tasks extracted from messages;
- work that an AI agent can take over;
- tasks that need reminders;
- keeping review-ready agent work visible.

## Daily Workflow

1. Open the tracker and review `Backlog`, `In progress`, and `Waiting review`.
2. Assign each actionable task to `Me` or `Codex`.
3. Move the tasks that are actively being handled to `In progress`.
4. Review tasks in `Waiting review`, then move accepted work to `Done`.
5. Leave unclear or externally blocked tasks in `Blocked` until the next action is known.

Codex or another agent can also scan connected sources and add tasks through the
API. Those tasks should still be triaged by assignee, priority, DD, and reminder.

Use `Agent queue` when you want to see what the AI agent can execute without
more human input. Use `Human input` when you want the smallest set of decisions,
reviews, or actions that still need a person.

## Columns

`Backlog`
: New or unstarted tasks. This is the default place for tasks that need triage.

`In progress`
: Tasks currently being handled by you or Codex.

`Waiting review`
: Work completed by Codex or another party that needs your review before closing.
  This is the preferred state for Codex output instead of moving directly to `Done`.

`Blocked`
: Tasks that cannot move forward because something is missing: a decision, answer,
  credential, file, approval, or external dependency.

`Done`
: Completed tasks. Move tasks here only after the actual outcome is accepted.

Tasks can be moved between columns by dragging the card or by using action buttons.

## Assignees

`Me`
: You own the next action.

`Codex`
: The AI agent owns the next action. Use this when the task can be handled through code,
  research, inbox triage, drafting, data cleanup, or other tool-assisted work.

`Unassigned`
: The next owner is not clear yet. These should be triaged regularly.

When the agent takes a task, it should mark the task as `Codex` and move it to
`In progress`. When the work is complete, Codex should move it to `Waiting review`
and describe what changed.

## Priorities

Use priority to decide what should be seen first, not to express emotional weight.

`P1`
: Urgent and high-impact. Needs attention now or today.

`P2`
: Important. Should be handled soon, usually this week.

`P3`
: Normal default task.

`P4`
: Low priority. Useful but not time-sensitive.

`P5`
: Someday or optional.

Visual coloring:

- overdue tasks are red;
- P1 urgent tasks are gold unless overdue;
- P2 important tasks are blue unless overdue;
- normal tasks are green unless overdue;
- simple thank-you or lightweight feedback tasks are intentionally neutral.

## Dates, DD, And Reminders

Every task has a date set through `created_at`. Every new task should also have
a DD:

- If the source includes a real deadline, use it.
- If the source does not include a deadline, the server estimates DD from priority.
- P1 defaults to about 1 day, P2 to about 3 days, P3 to about 7 days, P4 to
  about 14 days, and P5 to about 30 days.

Use `Reminder` when the task should reappear at a specific time.

Good reminder examples:

- follow up if no answer by tomorrow afternoon;
- review contract before Friday;
- send a weekly update before a meeting;
- check whether Codex output was accepted.

The reminder field is used by automation to find due work. A completed or
cancelled task should not trigger reminders.

## Creating Tasks Manually

Use the `New task` panel:

1. Write a short action-oriented title.
2. Add enough description to understand context without reopening Slack or Gmail.
3. Pick assignee, status, priority, and reminder.
4. Create the task.

Good titles:

- `Send Laguna Seca approval follow-up`
- `Review Codex UI changes for task 11`
- `Find emails for Jet Games 1-to-1 recipients`

Weak titles:

- `Laguna`
- `Check this`
- `Important`

## Tasks From Slack, Gmail, And Telegram

When Codex extracts tasks from context, each task should include:

- concrete next action;
- source name, such as Slack channel or Gmail thread;
- source URL when available;
- owner suggestion;
- priority suggestion;
- reminder if the task is time-sensitive.

The PM rule is simple: extracted tasks are candidates, not final truth. Review the
queue and correct assignee, priority, and status if Codex inferred something wrong.

## Working With Codex

Use `Codex` assignee for tasks that can be delegated.

Good Codex tasks:

- find information across Slack/Gmail/Telegram;
- draft a message or email;
- update code;
- create documentation;
- review a pull request;
- summarize a thread;
- prepare a list of next actions;
- check an integration or deployment.

Poor Codex tasks:

- decisions that require your judgment;
- sensitive outbound communication without prior draft approval;
- tasks requiring credentials Codex cannot access;
- ambiguous tasks with no desired outcome.

For outbound emails or Slack messages, Codex should show the draft first and send
only after explicit approval.

## Review Process

Use `Waiting review` for anything Codex says is complete.

Review checklist:

- Is the requested outcome actually done?
- Was anything sent externally?
- Are there files, links, or PRs to inspect?
- Does the task need a follow-up reminder?
- Should the task become `Done`, go back to `In progress`, or become `Blocked`?

If changes are needed, move the task back to `In progress`, keep assignee as
`Codex`, and describe what needs fixing.

## Suggested Operating Rhythm

Morning:

- scan new extracted tasks;
- assign owners;
- mark today's active work as `In progress`;
- set reminders for anything that can be missed.

Midday:

- check overdue and P1/P2 tasks;
- review any Codex tasks in `Waiting review`.

End of day:

- close completed tasks;
- move stalled tasks to `Blocked`;
- leave clear next actions for tomorrow.

## When To Use Blocked

Use `Blocked` only when the next action cannot be taken.

Examples:

- waiting for someone else's answer;
- missing access;
- unclear requirement;
- awaiting legal, finance, or approval;
- no source document available.

Do not use `Blocked` as a parking lot for low-priority tasks. Use P4/P5 in
`Backlog` for that.

## Source Of Truth

The tracker is the operational source of truth for what you and Codex are doing
next. Slack, Gmail, and Telegram are sources of context. Once a task is extracted,
the tracker status and assignee should be treated as the current state.
