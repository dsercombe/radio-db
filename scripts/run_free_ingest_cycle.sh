#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-free-ingest.lock}"
if ! { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
  LOCK_FILE="$APP_DIR/.radio_db_state/radio-db-free-ingest.$(id -u).lock"
  exec 9>"$LOCK_FILE"
fi
if ! flock -n 9; then
  echo "radio-db free ingest already running; skip"
  exit 0
fi

INGEST_LIMIT="${FREE_INGEST_LIMIT:-1200}"
radio-db ingest-free --limit "$INGEST_LIMIT"
radio-db stats
