#!/usr/bin/env bash
# Entrypoint the scheduler (launchd) runs once a day. Self-contained:
# ensures the tracker is reachable, runs the daily ritual, and — only if it
# started the tracker itself — stops it afterwards. If you already have the
# tracker running (run-local.command), this reuses it and leaves it alone.
#
# Reads configuration from the repo's .env (TASK_TRACKER_API_KEY, DATABASE_URL,
# and — to enable the agent work loop — DAILY_RITUAL_WORK=1 plus
# ANTHROPIC_API_KEY).
set -euo pipefail
export PYTHONUNBUFFERED=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

VENV_PY="$REPO/.venv/bin/python"
PORT="${TASK_TRACKER_PORT:-8000}"
URL="http://127.0.0.1:${PORT}"
LOG_DIR="$REPO/automation/logs"
mkdir -p "$LOG_DIR"
STAMP="$(date +%Y-%m-%d)"
RUN_LOG="$LOG_DIR/run-${STAMP}.log"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$RUN_LOG"; }

if [[ ! -x "$VENV_PY" ]]; then
  log "ERROR: $VENV_PY not found. Run install.command / set up the venv first."
  exit 1
fi

# Load .env safely (parsed, not executed) so the ritual sees the API key and
# flags — sourcing it as bash would break on unquoted values with spaces.
. "$REPO/automation/env.sh"
load_env "$REPO/.env"

# A remote TASK_TRACKER_URL from .env (hosted instance) wins; the local
# tracker lifecycle below only applies to the loopback default.
if [[ -n "${TASK_TRACKER_URL:-}" && "$TASK_TRACKER_URL" != http://127.0.0.1:* && "$TASK_TRACKER_URL" != http://localhost:* ]]; then
  URL="$TASK_TRACKER_URL"
  log "Using remote tracker at $URL."
  if ! curl -fsS -m 10 "$URL/readyz" >/dev/null 2>&1; then
    log "ERROR: remote tracker at $URL is not reachable; aborting."
    exit 3
  fi
  log "Running the daily ritual."
  set +e
  "$VENV_PY" "$REPO/automation/daily_ritual.py" "$@" >>"$RUN_LOG" 2>&1
  status=$?
  set -e
  log "Daily ritual finished with status $status. Digest in $LOG_DIR/digest-${STAMP}.md"
  exit "$status"
fi
export TASK_TRACKER_URL="$URL"

started_tracker=0
tracker_pid=""

readyz() { curl -fsS -m 2 "$URL/readyz" >/dev/null 2>&1; }

if readyz; then
  log "Tracker already up at $URL — reusing it."
else
  log "Tracker down — starting a temporary instance on :$PORT."
  "$VENV_PY" -m uvicorn app.main:app --host 127.0.0.1 --port "$PORT" >>"$RUN_LOG" 2>&1 &
  tracker_pid=$!
  started_tracker=1
  for _ in $(seq 1 30); do
    readyz && break
    sleep 0.5
  done
  if ! readyz; then
    log "ERROR: tracker did not become ready; aborting."
    [[ -n "$tracker_pid" ]] && kill "$tracker_pid" 2>/dev/null || true
    exit 3
  fi
fi

cleanup() {
  if [[ "$started_tracker" == "1" && -n "$tracker_pid" ]]; then
    log "Stopping the temporary tracker (pid $tracker_pid)."
    kill "$tracker_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT

log "Running the daily ritual."
set +e
"$VENV_PY" "$REPO/automation/daily_ritual.py" "$@" >>"$RUN_LOG" 2>&1
status=$?
set -e
log "Daily ritual finished with status $status. Digest in $LOG_DIR/digest-${STAMP}.md"
exit "$status"
