#!/usr/bin/env bash
# Install (or remove) the 15:00 daily task-ritual job for the current user.
# Cross-platform: launchd on macOS, a systemd user timer on Linux (crontab as
# the fallback). Run this yourself — it schedules an agent that acts on your
# behalf, so it is intentionally never loaded automatically.
#
#   automation/install.sh           # install and load the daily job
#   automation/install.sh --remove  # unload and delete it
#   automation/install.sh --dry-run # show what would be installed
#
# The continuous watch loop (reminders, review requests, agent trigger) is a
# separate opt-in job with the same verbs:
#
#   automation/install.sh --watch            # install and start the loop
#   automation/install.sh --watch --remove   # stop and delete it
#   automation/install.sh --watch --dry-run  # show what would be installed
#
# The Telegram polling adapter (task ingest + review-button callbacks) is a
# third opt-in job with the same verbs:
#
#   automation/install.sh --telegram [--remove|--dry-run]
#
# For Docker deployments skip this entirely: the `ritual` service in
# docker-compose.yml schedules itself (RITUAL_TIME in .env) and the `watch`
# profile runs the loop (docker compose --profile watch up -d).
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TARGET="daily"
case "${1:-}" in
  --watch) TARGET="watch"; shift ;;
  --telegram) TARGET="telegram"; shift ;;
esac
MODE="${1:-}"
OS="$(uname -s)"

# ---------- shared: install a KeepAlive launchd job from a template ----------
install_macos_keepalive() {
  local LABEL="$1" DESCRIPTION="$2"
  local TEMPLATE="$REPO/automation/${LABEL}.plist"
  local TARGET_PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
  local GUI="gui/$(id -u)"

  case "$MODE" in
    --remove)
      launchctl bootout "$GUI/$LABEL" 2>/dev/null || launchctl unload "$TARGET_PLIST" 2>/dev/null || true
      rm -f "$TARGET_PLIST"
      echo "Removed $LABEL."
      return
      ;;
    --dry-run)
      echo "Would render $TEMPLATE -> $TARGET_PLIST with __REPO__=$REPO and keep it alive (launchd KeepAlive)."
      return
      ;;
  esac

  mkdir -p "$HOME/Library/LaunchAgents" "$REPO/automation/logs"
  sed "s#__REPO__#${REPO}#g" "$TEMPLATE" > "$TARGET_PLIST"
  launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$GUI" "$TARGET_PLIST" 2>/dev/null || launchctl load "$TARGET_PLIST"
  echo "Installed $LABEL (launchd, KeepAlive) — $DESCRIPTION"
}

install_linux_systemd_service() {
  local NAME="$1" SCRIPT="$2" DESCRIPTION="$3"
  local UNIT_DIR="$HOME/.config/systemd/user"
  case "$MODE" in
    --remove)
      systemctl --user disable --now "${NAME}.service" 2>/dev/null || true
      rm -f "$UNIT_DIR/${NAME}.service"
      systemctl --user daemon-reload
      echo "Removed the ${NAME} systemd unit."
      return
      ;;
    --dry-run)
      echo "Would install a systemd user service in $UNIT_DIR keeping $REPO/automation/${SCRIPT} running."
      return
      ;;
  esac

  mkdir -p "$UNIT_DIR" "$REPO/automation/logs"
  cat > "$UNIT_DIR/${NAME}.service" <<UNIT
[Unit]
Description=${DESCRIPTION}

[Service]
WorkingDirectory=${REPO}
ExecStart=/bin/bash ${REPO}/automation/${SCRIPT}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now "${NAME}.service"
  echo "Installed ${NAME}.service (systemd user) — running."
  echo "Note: for runs while you are logged out, enable lingering once:"
  echo "  sudo loginctl enable-linger $USER"
}

# ---------- macOS: launchd ----------
install_macos() {
  local LABEL="com.taskassistant.daily"
  local TEMPLATE="$REPO/automation/${LABEL}.plist"
  local TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
  local GUI="gui/$(id -u)"

  case "$MODE" in
    --remove)
      launchctl bootout "$GUI/$LABEL" 2>/dev/null || launchctl unload "$TARGET" 2>/dev/null || true
      rm -f "$TARGET"
      echo "Removed $LABEL."
      return
      ;;
    --dry-run)
      echo "Would render $TEMPLATE -> $TARGET with __REPO__=$REPO and load it at 15:00 daily (launchd)."
      return
      ;;
  esac

  mkdir -p "$HOME/Library/LaunchAgents" "$REPO/automation/logs"
  sed "s#__REPO__#${REPO}#g" "$TEMPLATE" > "$TARGET"
  launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
  launchctl bootstrap "$GUI" "$TARGET" 2>/dev/null || launchctl load "$TARGET"

  echo "Installed $LABEL (launchd) — the daily ritual runs at 15:00 local time."
  echo "Digest lands in $REPO/automation/logs/digest-<date>.md"
  echo
  echo "Run it once now to confirm it works end to end:"
  echo "  launchctl kickstart -k $GUI/$LABEL"
  echo "  # or directly, watching the output:"
  echo "  automation/daily_run.sh --no-work"
}

# ---------- Linux: systemd user timer, crontab fallback ----------
install_linux_systemd() {
  local UNIT_DIR="$HOME/.config/systemd/user"
  case "$MODE" in
    --remove)
      systemctl --user disable --now taskassistant-daily.timer 2>/dev/null || true
      rm -f "$UNIT_DIR/taskassistant-daily.service" "$UNIT_DIR/taskassistant-daily.timer"
      systemctl --user daemon-reload
      echo "Removed taskassistant-daily systemd units."
      return
      ;;
    --dry-run)
      echo "Would install systemd user units in $UNIT_DIR running $REPO/automation/daily_run.sh at 15:00 daily."
      return
      ;;
  esac

  mkdir -p "$UNIT_DIR" "$REPO/automation/logs"
  cat > "$UNIT_DIR/taskassistant-daily.service" <<UNIT
[Unit]
Description=Personal Task Assistant daily ritual

[Service]
Type=oneshot
WorkingDirectory=${REPO}
ExecStart=/bin/bash ${REPO}/automation/daily_run.sh
UNIT
  cat > "$UNIT_DIR/taskassistant-daily.timer" <<UNIT
[Unit]
Description=Run the Personal Task Assistant daily ritual at 15:00

[Timer]
OnCalendar=*-*-* 15:00:00
Persistent=true

[Install]
WantedBy=timers.target
UNIT
  systemctl --user daemon-reload
  systemctl --user enable --now taskassistant-daily.timer

  echo "Installed taskassistant-daily.timer (systemd user) — runs at 15:00 local time."
  echo "Note: for runs while you are logged out, enable lingering once:"
  echo "  sudo loginctl enable-linger $USER"
  echo
  echo "Run it once now to confirm:"
  echo "  systemctl --user start taskassistant-daily.service"
}

install_linux_cron() {
  local MARK="# taskassistant-daily"
  local LINE="0 15 * * * /bin/bash ${REPO}/automation/daily_run.sh ${MARK}"
  case "$MODE" in
    --remove)
      (crontab -l 2>/dev/null | grep -v "$MARK") | crontab - || true
      echo "Removed the taskassistant-daily crontab entry."
      return
      ;;
    --dry-run)
      echo "Would add to crontab: $LINE"
      return
      ;;
  esac

  mkdir -p "$REPO/automation/logs"
  (crontab -l 2>/dev/null | grep -v "$MARK"; echo "$LINE") | crontab -
  echo "Installed a 15:00 crontab entry for the daily ritual."
  echo "Run it once now to confirm:  automation/daily_run.sh --no-work"
}

case "$OS" in
  Darwin)
    case "$TARGET" in
      watch)
        install_macos_keepalive "com.taskassistant.watch" "the watch loop is running."
        if [[ -z "$MODE" ]]; then
          echo "It presumes a running tracker (run-local.command); when the tracker is"
          echo "down it logs to automation/logs/watch.err.log and retries."
          echo "Watch it work:  tail -f $REPO/automation/logs/watch.out.log"
        fi
        ;;
      telegram)
        install_macos_keepalive "com.taskassistant.telegram" "the Telegram adapter is running."
        if [[ -z "$MODE" ]]; then
          echo "It needs TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWED_CHAT_IDS in .env."
          echo "Watch it work:  tail -f $REPO/automation/logs/telegram.out.log"
        fi
        ;;
      *)
        install_macos
        ;;
    esac
    ;;
  Linux)
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
      case "$TARGET" in
        watch) install_linux_systemd_service "taskassistant-watch" "watch_run.sh" "Personal Task Assistant watch loop" ;;
        telegram) install_linux_systemd_service "taskassistant-telegram" "telegram_run.sh" "Personal Task Assistant Telegram adapter" ;;
        *) install_linux_systemd ;;
      esac
    elif command -v crontab >/dev/null 2>&1 && [[ "$TARGET" == "daily" ]]; then
      install_linux_cron
    else
      echo "This job needs a systemd user session (cron cannot supervise a long-running loop)." >&2
      echo "Use the Docker deployment or run the matching automation/*_run.sh under your own supervisor." >&2
      exit 1
    fi
    ;;
  *)
    echo "Unsupported OS: $OS. Use the Docker deployment (docker compose up -d)." >&2
    exit 1
    ;;
esac
