#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state logs
LOCK_FILE="${ENRICHMENT_FREE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-enrichment-free.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"
_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "enrichment-free already running, skip"
  exit 0
fi
exec 8>"$GLOBAL_WRITE_LOCK_FILE"
if ! flock -n 8; then
  umask "$_LOCK_UMASK"
  echo "heavy radio-db writer already running, skip enrichment-free"
  exit 0
fi
umask "$_LOCK_UMASK"

SEED_LIMIT="${ENRICHMENT_FREE_SEED_LIMIT:-800}"
MIN_PRIORITY="${ENRICHMENT_FREE_MIN_PRIORITY:-35}"
SCAN_LIMIT="${ENRICHMENT_FREE_SCAN_LIMIT:-120}"
MAX_URLS="${ENRICHMENT_FREE_MAX_URLS_PER_STATION:-12}"
RETRIES="${ENRICHMENT_DB_LOCK_RETRIES:-3}"

run_radio_db() {
  local attempt=1
  local output
  while true; do
    set +e
    output="$(radio-db "$@" 2>&1)"
    local status=$?
    set -e
    printf '%s\n' "$output"
    if [[ $status -eq 0 ]]; then
      return 0
    fi
    if [[ $attempt -ge $RETRIES || "$output" != *"database is locked"* ]]; then
      return "$status"
    fi
    sleep "$((attempt * 20))"
    attempt="$((attempt + 1))"
  done
}

run_radio_db seed-enrichment-queue --limit "$SEED_LIMIT" --min-priority "$MIN_PRIORITY"
run_radio_db enrichment-free-scan --limit "$SCAN_LIMIT" --max-urls-per-station "$MAX_URLS"
run_radio_db enrichment-queue-stats
