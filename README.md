# Personal Task Assistant

Personal Task Assistant is an interface for collaboration between a person and an
AI agent. The agent parses tasks from context, turns them into an operational
queue, assigns priority and execution roles, starts work that does not need human
input, and keeps human-dependent tasks visible with minimal required input.

It is connector-ready rather than locked to one source. User-provided adapters
can connect Jira, Asana, YouTrack, Linear, Trello, Slack, Telegram, email, or
other systems by calling the JSON API, forwarding webhook events, or running
scheduled polling jobs. Those source-specific adapters are intentionally left to
the user to implement and operate for their own accounts, permissions, and data
rules.

It provides:

- A web UI for reviewing, sorting, assigning, and completing human/AI tasks.
- A JSON API for agents and integrations to create, ingest, read, and update tasks.
- A connector contract for user-built adapters from task trackers, chats, and inboxes.
- Database-backed storage, with SQLite for local development and Firestore for Google Cloud.
- Automatic DD estimates when the source did not provide a deadline.
- Reminder fields so missed tasks can be queried and notified.
- Role-oriented queues for agent-ready work, human input, review, blocked work,
  and unassigned tasks.

## Local Development

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000`.
Use your own local `.env`; never commit real API keys, OAuth secrets, databases,
or service-account files.

## Google Login

The web UI is protected with Google OAuth. Create OAuth credentials in Google Cloud
Console and add this redirect URI:

```text
http://127.0.0.1:8000/auth/google/callback
```

For Cloud Run, add the deployed callback URL too:

```text
https://<cloud-run-url>/auth/google/callback
```

Then set:

```bash
GOOGLE_OAUTH_CLIENT_ID=...
GOOGLE_OAUTH_CLIENT_SECRET=...
ALLOWED_GOOGLE_EMAILS=your-email@gmail.com
SESSION_SECRET_KEY=long-random-secret
PUBLIC_BASE_URL=http://127.0.0.1:8000
SESSION_COOKIE_HTTPS=false
```

The JSON API still supports `Authorization: Bearer <TASK_TRACKER_API_KEY>` for Codex,
reminder jobs, and external ingestion.

## API

Set `TASK_TRACKER_API_KEY` in `.env`. API clients must send:

```http
Authorization: Bearer <key>
```

Main endpoints:

- `GET /readyz`
- `GET /api/tasks`
- `POST /api/tasks`
- `PATCH /api/tasks/{task_id}`
- `DELETE /api/tasks/{task_id}`
- `GET /api/agent/queue`
- `POST /api/agent/tasks`
- `POST /api/agent/ingest/context`
- `POST /api/ingest/context`
- `GET /api/reminders/due`

For day-to-day PM usage, see `docs/PM_GUIDE.md`. For integration examples, see
`docs/API.md`.

## Integration Model

Personal Task Assistant does not ship with built-in Jira, Asana, YouTrack,
Slack, Telegram, or email credentials. Instead, integrations should be built as
small adapters controlled by the user:

- Webhook adapters receive events from systems such as Jira, Asana, YouTrack, or Slack and call `POST /api/agent/ingest/context`.
- Polling adapters periodically read systems such as Telegram, email, or task trackers and create normalized tasks through `POST /api/agent/tasks`.
- Sync adapters can read `GET /api/agent/queue` and update external tools through their own APIs.

The app supplies the task model, prioritization surface, DD defaults, and agent
queue API. The user remains responsible for implementing each external adapter,
storing its credentials safely, and deciding what data is allowed to enter the
assistant.

## Google Cloud

The intended production shape is Cloud Run + Firestore.

Use `deploy/gcloud.example.sh` as a deployment checklist after creating a GCP project,
Artifact Registry repository, Firestore database, and Secret Manager secrets.

For Cloud Run set:

```bash
PUBLIC_BASE_URL=https://<cloud-run-url>
SESSION_COOKIE_HTTPS=true
TASK_STORE=firestore
```

## License

MIT. See `LICENSE`.
