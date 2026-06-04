#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is required. Install Python 3, then run this installer again."
  echo
  read -r -p "Press Enter to close."
  exit 1
fi

python3 scripts/setup_wizard.py
