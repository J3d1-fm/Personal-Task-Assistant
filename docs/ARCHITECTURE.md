# Architecture

## MVP

- FastAPI serves both the web UI and JSON API.
- Google OAuth protects the browser UI with a signed session cookie and an email allowlist.
- `PUBLIC_BASE_URL` controls the OAuth callback URL in hosted environments.
- `SESSION_COOKIE_HTTPS=true` should be used behind HTTPS in Cloud Run.
- SQLAlchemy stores tasks in SQLite locally.
- Firestore stores tasks in Google Cloud when `TASK_STORE=firestore`.
- Codex or another AI agent extracts tasks from Slack, Telegram, Gmail, or other context and writes them through the API.
- Agent clients can read a role-aware execution queue through `/api/agent/queue`.
- Reminder polling calls `/api/reminders/due`, sends notifications, then marks each reminder as sent.

## Google Cloud Target

- Cloud Run hosts the app container.
- Firestore Native stores tasks.
- Secret Manager stores `TASK_TRACKER_API_KEY`.
- Secret Manager stores Google OAuth client credentials and the session secret.
- Cloud Scheduler can call a small reminder job or a Cloud Run job every 15-30 minutes.

## Data Model

Task fields intentionally stay small:

- `title`, `description`
- `status`: `backlog`, `in_progress`, `waiting_review`, `blocked`, `done`, `cancelled`
- `assignee`: `me`, `codex`, `unassigned`
- `origin`: `manual`, `slack`, `telegram`, `email`, `codex`, `other`
- `priority`, `due_at`, `reminder_at`
- `source_name`, `source_url`, `source_context`

`created_at` is the date the task was set. `due_at` is the DD. If an API client
omits `due_at`, the store layer estimates DD from priority before saving.
