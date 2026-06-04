# Installation

Personal Task Assistant can be installed locally by a non-technical user, then
extended by an AI agent or developer.

## Fast Path For Non-Technical Users

1. Open the public repository:
   `https://github.com/J3d1-fm/Personal-Task-Assistant`
2. Give the repository link to your AI coding agent.
3. Ask it to follow `docs/AGENT_INSTALL.md`.
4. Let the agent run the installer, check the local app, and explain what it did.

Suggested prompt:

```text
Install this app for me:
https://github.com/J3d1-fm/Personal-Task-Assistant

Follow docs/AGENT_INSTALL.md. Use the local quick-start first. Do not add real
Jira, Asana, YouTrack, Slack, Telegram, email, OAuth, or cloud credentials unless
I explicitly provide them. After setup, open the app locally and tell me the URL,
the generated API key location, and what integrations still need custom adapters.
```

## Click Installer On macOS

1. Download or clone the repository.
2. Double-click `install.command`.
3. Follow the questions in the terminal window.
4. When setup finishes, the installer starts the local app and opens the browser.

The local installer:

- creates `.venv`;
- installs `requirements.txt`;
- creates `.env` with generated local secrets;
- uses SQLite by default;
- starts the app at `http://127.0.0.1:8000`.

After the first setup, use `run-local.command` to start the local app again
without repeating dependency installation.

## Local Mode Without Google Cloud

Local Mode is the default path for personal use:

- tasks are stored in the local SQLite file `task_tracker.db`;
- auth uses a local browser session when Google OAuth is not configured;
- generated secrets stay in `.env`;
- Cloud Run, Firestore, Google OAuth, Secret Manager, and service accounts are
  not required.

Local Mode is intended for `127.0.0.1` only. Do not expose it to a LAN, public
tunnel, or `--host 0.0.0.0` without configuring real authentication.

Check the local setup:

```bash
.venv/bin/python scripts/local_doctor.py
```

Start locally:

```bash
./run-local.command
```

If another local app is already using port `8000`, stop that app or start
Personal Task Assistant manually on another loopback port:

```bash
.venv/bin/uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

More detail: `docs/LOCAL_MODE.md`.

## Terminal Installer

On macOS, Linux, or Windows with Python installed:

```bash
python3 scripts/setup_wizard.py
```

On Windows, use `python` instead of `python3` if that is how Python is installed.

For non-interactive local setup:

```bash
python3 scripts/setup_wizard.py --yes --no-start
```

## What The Installer Does Not Do

The installer does not configure Jira, Asana, YouTrack, Linear, Trello, Slack,
Telegram, email, Google OAuth, or Cloud Run by itself.

Those systems need user-specific credentials, permissions, and data rules. Add
them later as user-built adapters that call the JSON API:

- `POST /api/agent/ingest/context`
- `POST /api/agent/tasks`
- `GET /api/agent/queue`

## Production Deployment

For a hosted deployment, use `deploy/gcloud.example.sh` as a checklist. An AI
agent can help, but the user must provide the cloud project, OAuth setup,
allowed emails, and secret manager values.
