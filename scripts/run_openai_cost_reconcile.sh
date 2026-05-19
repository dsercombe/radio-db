#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-openai-reconcile.lock}"
if ! { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
  LOCK_FILE="$APP_DIR/.radio_db_state/radio-db-openai-reconcile.$(id -u).lock"
  exec 9>"$LOCK_FILE"
fi
if ! flock -n 9; then
  echo "radio-db openai cost reconcile already running; skip"
  exit 0
fi

radio-db reconcile-openai-costs
