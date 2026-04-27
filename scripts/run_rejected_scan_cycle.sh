#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

LOCK_FILE="${REJECTED_SCAN_LOCK_FILE:-/tmp/radio-db-rejected-scan.lock}"

# Prevent overlapping runs (timer jitter/restarts).
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "rejected-scan already running, skip"
  exit 0
fi

radio-db scan-rejected-cycle \
  --station-limit "${REJECTED_SCAN_STATION_LIMIT:-20}" \
  --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}"

radio-db stats
