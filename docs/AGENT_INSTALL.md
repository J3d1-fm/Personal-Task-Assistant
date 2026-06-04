# Agent Install Instructions

Use this guide when a user gives you the repository link and asks you to install
Personal Task Assistant.

## Goal

Set up a working local Personal Task Assistant instance quickly, without asking
the user to understand Python, FastAPI, OAuth, or task-ingestion internals.

## Safety Rules

- Start with local setup unless the user explicitly asks for production deploy.
- Do not request or store Jira, Asana, YouTrack, Slack, Telegram, email, OAuth,
  cloud, or service-account credentials during the first local install.
- Do not commit `.env`, SQLite databases, service-account files, or exported task
  data.
- Treat external integrations as user-built adapters. Explain that the app
  exposes the API contract, but source-specific connectors are not bundled.

## Local Install Steps

1. Clone the repository if it is not already present.
2. Run the setup wizard:

   ```bash
   python3 scripts/setup_wizard.py
   ```

3. If Python is installed as `python`, use:

   ```bash
   python scripts/setup_wizard.py
   ```

4. Let the wizard create `.venv`, install dependencies, and create `.env`.
5. Start the app through the wizard, or run:

   ```bash
   .venv/bin/uvicorn app.main:app --reload
   ```

6. Open `http://127.0.0.1:8000`.
7. Verify `GET http://127.0.0.1:8000/readyz` returns `{"status":"ok"}`.

## What To Tell The User After Local Install

Report:

- the local app URL;
- that `.env` was created locally and must not be shared;
- that the default setup uses SQLite;
- that Jira, Asana, YouTrack, Slack, Telegram, email, and similar systems require
  separate adapters;
- the next recommended integration step, if the user named a specific source.

## Adapter Pattern

For each external system, build a small adapter owned by the user:

- Webhook adapter: receive source events and call `POST /api/agent/ingest/context`.
- Polling adapter: periodically read source APIs and call `POST /api/agent/tasks`.
- Sync adapter: read `GET /api/agent/queue` and update the external system via
  that system's API.

Keep adapter credentials outside this repository, preferably in the user's secret
manager or local environment.

## Production Deploy

Only do this when the user explicitly asks for hosted deployment.

1. Review `deploy/gcloud.example.sh`.
2. Ask for the target cloud project and allowed login emails.
3. Help the user create OAuth credentials and secret manager entries.
4. Deploy to Cloud Run or the user's chosen host.
5. Verify `/readyz`, login, task creation, and API-key access.
