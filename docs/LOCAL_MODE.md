# Local Mode

Local Mode runs Personal Task Assistant entirely on one machine. It is the
recommended first setup for personal use, demos, and non-technical onboarding.

## What Runs Locally

- FastAPI web app at `http://127.0.0.1:8000`
- SQLite database in `task_tracker.db`
- Generated API key and session secret in `.env`
- Local development auth in the browser
- Optional local adapters, such as the Telegram polling adapter

Google Cloud, Cloud Run, Firestore, Google OAuth, Secret Manager, and service
accounts are not required for Local Mode.

## First Setup

On macOS:

```bash
./install.command
```

Or from a terminal:

```bash
python3 scripts/setup_wizard.py
```

For an AI agent or scripted setup:

```bash
python3 scripts/setup_wizard.py --yes --no-start
```

The wizard creates:

- `.venv`
- `.env`
- local generated secrets
- SQLite configuration

## Start The App Later

On macOS, double-click:

```text
run-local.command
```

Or run:

```bash
./run-local.command
```

Manual fallback:

```bash
.venv/bin/uvicorn app.main:app --reload
```

## Check Local Mode

Before starting the server:

```bash
.venv/bin/python scripts/local_doctor.py
```

After starting the server:

```bash
.venv/bin/python scripts/local_doctor.py --require-server
```

If you start the app on a custom local port:

```bash
.venv/bin/python scripts/local_doctor.py --require-server --url http://127.0.0.1:8034
```

The doctor checks:

- Python version
- `.venv`
- `.env`
- SQLite configuration
- generated local secrets
- local dependency imports
- optional `/readyz` status

## Local Data

Local Mode stores live tasks in:

```text
task_tracker.db
```

This file is ignored by git. Do not commit it if it contains personal tasks.

Simple backup:

```bash
cp task_tracker.db task_tracker.backup.db
```

Simple restore:

```bash
cp task_tracker.backup.db task_tracker.db
```

Stop the local server before copying the database for backup or restore.

## Local Adapters

Adapters can also run locally. For example:

```bash
cp adapters/telegram.env.example .env.telegram
```

Fill in your Telegram bot token and allowed chat ids, then run:

```bash
set -a
. ./.env.telegram
set +a
python3 adapters/telegram_polling_adapter.py --once --dry-run
```

Adapters remain user-owned. Keep source credentials in local env files or a
secret manager, never in the repository.

## When To Use Cloud Later

Use hosted deployment only when you need:

- access from multiple devices without a tunnel;
- shared team access;
- managed Firestore storage;
- Google OAuth allowlisting;
- Cloud Run uptime.

For single-user personal operation, Local Mode is enough.
