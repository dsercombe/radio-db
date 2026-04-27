#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

LOCK_FILE="${MAIN_SCAN_CAMPAIGN_LOCK_FILE:-/tmp/radio-db-main-scan-campaign.lock}"
STATION_LIMIT="${MAIN_SCAN_STATION_LIMIT:-100}"
MAX_PAGES="${MAIN_SCAN_MAX_PAGES:-3}"
MIN_CONFIDENCE="${MAIN_SCAN_MIN_CONFIDENCE:-0.0}"
LOG_DIR="${MAIN_SCAN_CAMPAIGN_LOG_DIR:-$APP_DIR/.radio_db_state}"
LOG_FILE="$LOG_DIR/main_scan_campaign_$(date -u +%Y%m%dT%H%M%SZ).log"

mkdir -p "$LOG_DIR"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "main-scan-campaign already running, skip"
  exit 0
fi

echo "== main scan campaign start $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
echo "station_limit=$STATION_LIMIT max_pages=$MAX_PAGES min_confidence=$MIN_CONFIDENCE" | tee -a "$LOG_FILE"

radio-db scan-main-cycle \
  --station-limit "$STATION_LIMIT" \
  --max-pages "$MAX_PAGES" \
  --min-confidence "$MIN_CONFIDENCE" \
  --reset-checkpoint | tee -a "$LOG_FILE"

while true; do
  RAW_OUTPUT="$(radio-db scan-main-cycle \
    --station-limit "$STATION_LIMIT" \
    --max-pages "$MAX_PAGES" \
    --min-confidence "$MIN_CONFIDENCE")"
  printf '%s\n' "$RAW_OUTPUT" | tee -a "$LOG_FILE"
  JSON_LINE="$(printf '%s\n' "$RAW_OUTPUT" | python - <<'PY'
import json, sys
raw = sys.stdin.read()
start = raw.find('{')
if start == -1:
    print('{}')
    raise SystemExit(0)
obj = json.loads(raw[start:])
print(json.dumps(obj))
PY
)"
  STATIONS_CONSIDERED="$(printf '%s' "$JSON_LINE" | python - <<'PY'
import json, sys
payload = json.loads(sys.stdin.read() or '{}')
print(int(payload.get('stations_considered', 0) or 0))
PY
)"
  if [ "$STATIONS_CONSIDERED" -lt $(( STATION_LIMIT * 3 )) ]; then
    break
  fi
done

echo "== main scan campaign end $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
