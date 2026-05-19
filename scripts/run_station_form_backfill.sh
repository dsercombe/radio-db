#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-station-form-backfill.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"
_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "station form backfill already running; skip"
  exit 0
fi
exec 8>"$GLOBAL_WRITE_LOCK_FILE"
if ! flock -n 8; then
  umask "$_LOCK_UMASK"
  echo "heavy radio-db writer already running, skip station form backfill"
  exit 0
fi
umask "$_LOCK_UMASK"

SCAN_MODE="${SCAN_MODE:-read}"
MAX_FORMS_PER_STATION="${MAX_FORMS_PER_STATION:-3}"
PER_STATION_TIMEOUT_SECONDS="${PER_STATION_TIMEOUT_SECONDS:-240}"
STATION_SCAN_MIN_QUALITY_SCORE="${STATION_SCAN_MIN_QUALITY_SCORE:-15}"
export STATION_SCAN_MIN_QUALITY_SCORE

mapfile -t STATION_IDS < <(
  sqlite3 "$APP_DIR/radio.db" "
    SELECT id
    FROM stations
    WHERE status IN ('VERIFIED','CANDIDATE')
      AND website_url IS NOT NULL
      AND website_url != ''
    ORDER BY
      CASE status WHEN 'VERIFIED' THEN 0 ELSE 1 END,
      priority_tier DESC,
      confidence_score DESC,
      id ASC;
  "
)

TOTAL="${#STATION_IDS[@]}"
echo "station form backfill started: total=$TOTAL mode=$SCAN_MODE max_forms_per_station=$MAX_FORMS_PER_STATION threshold=$STATION_SCAN_MIN_QUALITY_SCORE"

processed=0
success=0
timeouts=0
errors=0

for station_id in "${STATION_IDS[@]}"; do
  processed=$((processed + 1))
  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "[$ts] station $processed/$TOTAL id=$station_id"
  if timeout "${PER_STATION_TIMEOUT_SECONDS}s" \
    radio-db scan-submission-forms \
      --station-id "$station_id" \
      --mode "$SCAN_MODE" \
      --max-forms-per-station "$MAX_FORMS_PER_STATION"; then
    success=$((success + 1))
    continue
  fi

  rc=$?
  if [[ "$rc" -eq 124 ]]; then
    timeouts=$((timeouts + 1))
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] timeout id=$station_id"
  else
    errors=$((errors + 1))
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] error id=$station_id rc=$rc"
  fi
done

echo "station form backfill finished: processed=$processed success=$success timeouts=$timeouts errors=$errors"
