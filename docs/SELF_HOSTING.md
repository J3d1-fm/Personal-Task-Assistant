# Self-Hosting: Your Own Second Worker

This page is the standalone deployment story: one clone, one command, and you
have your own task board with a daily AI worker — the digest every day, and
(opt-in) an agent that claims tasks and hands results back for your review.

Pick one of three shapes:

| Shape | Best for | Scheduler |
| --- | --- | --- |
| Docker Compose | Any OS, servers, NAS, always-on | built-in (`ritual` service) |
| Native macOS | Your own Mac, Local Mode | launchd (`automation/install.sh`) |
| Native Linux | Your own machine/server without Docker | systemd user timer or cron (`automation/install.sh`) |

## Docker Compose (recommended)

```bash
git clone https://github.com/J3d1-fm/Personal-Task-Assistant.git
cd Personal-Task-Assistant
cp .env.example .env
# in .env set at least:
#   TASK_TRACKER_API_KEY=<long random string>
#   SESSION_SECRET_KEY=<long random string, 32+ chars>
#   TZ=Europe/Belgrade            # your timezone; RITUAL_TIME is local to it
docker compose up -d
```

You get two services:

- **tracker** — the board API on `127.0.0.1:8000`, SQLite persisted in the
  `tracker-data` volume, schema migrated automatically on start.
- **ritual** — the daily worker: at `RITUAL_TIME` (default 15:00) it writes a
  prioritized digest of your board; with `DAILY_RITUAL_WORK=1` and an
  `ANTHROPIC_API_KEY` in `.env` it also works the codex queue through the MCP
  server and reports what it moved to review. With `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_NOTIFY_CHAT_ID` in `.env` the digest is also delivered to your
  Telegram chat (the spoken voice note is macOS-only and skips itself in
  Docker).

A third, opt-in service adds the reflexes:

```bash
docker compose --profile watch up -d
```

Note: the local no-API-billing work runner (`WORK_RUNNER=claude`, headless
Claude Code) applies to native installs only — the Docker image ships no
Claude Code CLI, so containers use the Anthropic-API loop
(`ANTHROPIC_API_KEY`).

- **watch** — polls the board every `WATCH_INTERVAL` seconds (default 30):
  pushes due reminders to Telegram, announces tasks entering `waiting_review`
  with inline ✅/🔁/✋ buttons (presses are handled by the Telegram polling
  adapter), and with `WATCH_WORK=1` kicks the agent work loop as soon as a
  task is assigned to the agent. See `automation/README.md` for the safety
  model.

Read the digest:

```bash
docker compose exec ritual sh -c 'cat automation/logs/digest-*.md | tail -60'
```

Run the ritual immediately instead of waiting for the schedule:

```bash
docker compose exec ritual python automation/daily_ritual.py --no-work
```

### Web UI note

Local-dev auto-login is loopback-only **by design**, and traffic from your
browser into a container is not loopback — so the web UI in Docker requires
Google OAuth (set `GOOGLE_OAUTH_CLIENT_ID/SECRET` and `ALLOWED_GOOGLE_EMAILS`
in `.env`, see README "Login And Auth"). Everything agent-facing — the JSON
API, the MCP server, the daily ritual — authenticates with
`TASK_TRACKER_API_KEY` and works without OAuth.

### Connect your agents

Point any MCP client at the containerized board:

```bash
claude mcp add task-assistant \
  -e TASK_TRACKER_URL=http://127.0.0.1:8000 \
  -e TASK_TRACKER_API_KEY=<your key> \
  -- python3 /path/to/repo/adapters/task_assistant_mcp.py
```

## Native (no Docker)

macOS: `install.command` sets up Local Mode; then `automation/install.sh`
loads the 15:00 launchd job.

Linux: create the venv (`python3 -m venv .venv && .venv/bin/pip install -r
requirements.txt`), write `.env`, then `automation/install.sh` — it installs a
systemd user timer (or a crontab entry when systemd user sessions are
unavailable). For runs while logged out: `sudo loginctl enable-linger $USER`.

Both native shapes run `automation/daily_run.sh`, which reuses your running
tracker or starts a temporary one just for the run.

## Safety model (what the unattended agent can and cannot do)

- The scheduled agent has **only** the `task_assistant_*` MCP tools — no
  shell, no repo, no web access.
- It can never mark a task `done`; its terminal state is `waiting_review`, so
  you approve everything it produces. This is enforced in code
  (`automation/work_loop.py`), not just requested in a prompt, and covered by
  tests.
- `--max-tasks` (default 3, via `RITUAL_ARGS`) caps how much it claims per run.
- Without `DAILY_RITUAL_WORK=1` it is read-only: digest, no writes.

## Upgrading

Releases are tagged (`vX.Y.Z`) with notes generated from `CHANGELOG.txt`.

```bash
git pull
docker compose up -d --build     # Docker shape
# native shape: .venv/bin/pip install -r requirements.txt (run_local does this automatically)
```

SQLite schema migrations run automatically at startup, including databases
created by older releases.
