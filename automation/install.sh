#!/usr/bin/env bash
# Install (or remove) the 15:00 daily task-ritual launchd job for the current
# user. Run this yourself — it schedules an agent that acts on your behalf, so
# it is intentionally not loaded automatically.
#
#   automation/install.sh          # render, install, and load the job
#   automation/install.sh --remove # unload and delete it
#   automation/install.sh --dry-run# show what would be installed
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LABEL="com.taskassistant.daily"
TEMPLATE="$REPO/automation/${LABEL}.plist"
TARGET="$HOME/Library/LaunchAgents/${LABEL}.plist"
GUI="gui/$(id -u)"

case "${1:-}" in
  --remove)
    launchctl bootout "$GUI/$LABEL" 2>/dev/null || launchctl unload "$TARGET" 2>/dev/null || true
    rm -f "$TARGET"
    echo "Removed $LABEL."
    exit 0
    ;;
  --dry-run)
    echo "Would render $TEMPLATE -> $TARGET with __REPO__=$REPO and load it at 15:00 daily."
    exit 0
    ;;
esac

mkdir -p "$HOME/Library/LaunchAgents" "$REPO/automation/logs"
sed "s#__REPO__#${REPO}#g" "$TEMPLATE" > "$TARGET"

# Reload cleanly if already present.
launchctl bootout "$GUI/$LABEL" 2>/dev/null || true
launchctl bootstrap "$GUI" "$TARGET" 2>/dev/null || launchctl load "$TARGET"

echo "Installed $LABEL — the daily ritual will run at 15:00 local time."
echo "Digest lands in $REPO/automation/logs/digest-<date>.md"
echo
echo "Run it once now to confirm it works end to end:"
echo "  launchctl kickstart -k $GUI/$LABEL"
echo "  # or directly, watching the output:"
echo "  automation/daily_run.sh --no-work"
