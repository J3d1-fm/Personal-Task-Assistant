#!/usr/bin/env bash
# Entrypoint launchd/systemd keeps alive for the Telegram polling adapter —
# the bot's single getUpdates consumer: inbound task ingest AND the inline
# review-button callbacks both live here. Without a supervised adapter the
# buttons on review requests do nothing.
#
# Reads configuration from the repo's .env (TELEGRAM_BOT_TOKEN,
# TELEGRAM_ALLOWED_CHAT_IDS, TASK_TRACKER_API_KEY). The adapter exits on
# credential errors by design; the supervisor's KeepAlive restarts it after
# transient failures.
set -euo pipefail
export PYTHONUNBUFFERED=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VENV_PY="$REPO/.venv/bin/python"
PORT="${TASK_TRACKER_PORT:-8000}"
mkdir -p "$REPO/automation/logs"

if [[ ! -x "$VENV_PY" ]]; then
  echo "ERROR: $VENV_PY not found. Run install.command / set up the venv first." >&2
  exit 1
fi

# Load .env safely (parsed, not executed).
. "$REPO/automation/env.sh"
load_env "$REPO/.env"
export TASK_TRACKER_URL="${TASK_TRACKER_URL:-http://127.0.0.1:${PORT}}"

exec "$VENV_PY" "$REPO/adapters/telegram_polling_adapter.py" "$@"
