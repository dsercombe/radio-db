#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

LOCK_FILE="${REJECTED_SCAN_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-rejected-scan.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"

mkdir -p "$APP_DIR/.radio_db_state"

# Prevent overlapping runs (timer jitter/restarts).
_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "rejected-scan already running, skip"
  exit 0
fi

exec 8>"$GLOBAL_WRITE_LOCK_FILE"
if ! flock -n 8; then
  umask "$_LOCK_UMASK"
  echo "heavy radio-db writer already running, skip rejected-scan"
  exit 0
fi
umask "$_LOCK_UMASK"

SCAN_MODE="${REJECTED_SCAN_MODE:-cycle}"
WORKER_COUNT="${REJECTED_SCAN_WORKER_COUNT:-1}"
TOTAL_LIMIT="${REJECTED_SCAN_STATION_LIMIT:-20}"
if ! [[ "$WORKER_COUNT" =~ ^[0-9]+$ ]] || [ "$WORKER_COUNT" -lt 1 ]; then
  WORKER_COUNT=1
fi
PER_WORKER_LIMIT=$(( (TOTAL_LIMIT + WORKER_COUNT - 1) / WORKER_COUNT ))

radio-db reset-rejected-scan-claims --older-than-minutes "${REJECTED_SCAN_CLAIM_RESET_MINUTES:-15}" >/dev/null

if [ "$SCAN_MODE" = "dispatch" ]; then
  radio-db scan-rejected-dispatch \
    --station-limit "$TOTAL_LIMIT" \
    --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}" \
    --workers "$WORKER_COUNT"
  radio-db stats
  exit 0
fi

if [ "$SCAN_MODE" = "pipeline" ]; then
  radio-db scan-rejected-discovery \
    --station-limit "$TOTAL_LIMIT" \
    --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}" \
    --workers "$WORKER_COUNT"
  radio-db stats
  exit 0
fi

if [ "$SCAN_MODE" = "continuous" ]; then
  radio-db scan-rejected-continuous \
    --station-limit "$TOTAL_LIMIT" \
    --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}" \
    --workers "$WORKER_COUNT"
  radio-db stats
  exit 0
fi

radio-db scan-rejected-cycle \
  --station-limit "$PER_WORKER_LIMIT" \
  --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}" &
PIDS=("$!")

if [ "$WORKER_COUNT" -gt 1 ]; then
  for _ in $(seq 2 "$WORKER_COUNT"); do
    radio-db scan-rejected-cycle \
      --station-limit "$PER_WORKER_LIMIT" \
      --max-pages "${REJECTED_SCAN_MAX_PAGES:-3}" &
    PIDS+=("$!")
  done
fi

status=0
for pid in "${PIDS[@]}"; do
  if ! wait "$pid"; then
    status=1
  fi
done

if [ "$status" -ne 0 ]; then
  exit "$status"
fi

radio-db stats
