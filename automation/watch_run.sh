#!/usr/bin/env bash
# Entrypoint launchd keeps alive for the watch loop. Unlike daily_run.sh it
# does not manage the tracker's lifecycle: the loop presumes a running tracker
# (run-local.command or Docker) and, when it is unreachable, logs and retries
# on its own instead of exiting — so KeepAlive never thrashes.
#
# Reads configuration from the repo's .env (TASK_TRACKER_API_KEY; for
# Telegram delivery TELEGRAM_BOT_TOKEN + TELEGRAM_NOTIFY_CHAT_ID; for the
# agent trigger WATCH_WORK=1 plus ANTHROPIC_API_KEY).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VENV_PY="$REPO/.venv/bin/python"
PORT="${TASK_TRACKER_PORT:-8000}"
mkdir -p "$REPO/automation/logs"

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: $VENV_PY not found. Run install.command / set up the venv first." >&2
  exit 1
fi

# Load .env (export every assignment) so the loop sees keys and flags.
if [[ -f "$REPO/.env" ]]; then
  set -a; . "$REPO/.env"; set +a
fi
export TASK_TRACKER_URL="${TASK_TRACKER_URL:-http://127.0.0.1:${PORT}}"

exec "$VENV_PY" "$REPO/automation/watch.py" "$@"
