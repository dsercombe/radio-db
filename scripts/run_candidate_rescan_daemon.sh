#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

STATE_DIR="${STATE_DIR:-$APP_DIR/.radio_db_state}"
WORK_DIR="${WORK_DIR:-$APP_DIR/tmp}"
LOG_DIR="${LOG_DIR:-$APP_DIR/logs}"
mkdir -p "$STATE_DIR" "$WORK_DIR" "$LOG_DIR"

IDS_FILE="$WORK_DIR/open_candidate_ids.txt"
OFFSET_FILE="$STATE_DIR/open_candidate_rescan_offset.txt"
LOCK_FILE="${RESCAN_DAEMON_LOCK_FILE:-/tmp/radio-db-open-candidate-rescan.lock}"
LOG_FILE="$LOG_DIR/open_candidate_rescan_daemon.log"

BATCH_SIZE="${BATCH_SIZE:-50}"
SLEEP_SECONDS="${SLEEP_SECONDS:-20}"
MAX_PAGES="${MAX_PAGES:-3}"

exec 9>"$LOCK_FILE"
if ! flock -n 9; then
	echo "[$(date -Is)] rescan-daemon already running, exit" | tee -a "$LOG_FILE"
	exit 0
fi

if ! [[ "$BATCH_SIZE" =~ ^[0-9]+$ ]] || [[ "$BATCH_SIZE" -lt 1 ]]; then
	BATCH_SIZE=50
fi

if ! [[ "$SLEEP_SECONDS" =~ ^[0-9]+$ ]] || [[ "$SLEEP_SECONDS" -lt 1 ]]; then
	SLEEP_SECONDS=20
fi

if ! [[ "$MAX_PAGES" =~ ^[0-9]+$ ]] || [[ "$MAX_PAGES" -lt 1 ]]; then
	MAX_PAGES=3
fi

echo "[$(date -Is)] rebuilding open candidate list" | tee -a "$LOG_FILE"
sqlite3 radio.db "
SELECT id
FROM stations
WHERE status IN ('REJECTED','CANDIDATE')
	AND website_url IS NOT NULL
ORDER BY id ASC;
" > "$IDS_FILE"

TOTAL="$(wc -l < "$IDS_FILE" | tr -d ' ')"
if [[ "$TOTAL" -eq 0 ]]; then
	echo "[$(date -Is)] no open candidates found" | tee -a "$LOG_FILE"
	exit 0
fi

if [[ -f "$OFFSET_FILE" ]]; then
	OFFSET="$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)"
else
	OFFSET=0
fi

if ! [[ "$OFFSET" =~ ^[0-9]+$ ]]; then
	OFFSET=0
fi

if [[ "$OFFSET" -ge "$TOTAL" ]]; then
	OFFSET=0
fi

echo "[$(date -Is)] total_candidates=$TOTAL start_offset=$OFFSET batch_size=$BATCH_SIZE" | tee -a "$LOG_FILE"

while [[ "$OFFSET" -lt "$TOTAL" ]]; do
	START=$((OFFSET + 1))
	END=$((OFFSET + BATCH_SIZE))
	if [[ "$END" -gt "$TOTAL" ]]; then
		END="$TOTAL"
	fi

	IDS="$(sed -n "${START},${END}p" "$IDS_FILE" | paste -sd, -)"
	if [[ -z "$IDS" ]]; then
		break
	fi

	echo "[$(date -Is)] processing rows $START..$END" | tee -a "$LOG_FILE"

	IDS="$IDS" MAX_PAGES="$MAX_PAGES" python - <<'PY' | tee -a "$LOG_FILE"
import os
import sys
sys.path.insert(0, '/opt/radio-database')
from src.radio_db.db import SessionLocal
from src.radio_db.services.forms_agent import start_manual_scan_run

raw_ids = os.environ.get('IDS', '').strip()
max_pages = int(os.environ.get('MAX_PAGES', '3'))
ids = [int(x) for x in raw_ids.split(',') if x.strip()]

counts = {'awaiting_review': 0, 'blocked': 0, 'errors': 0, 'other': 0}
with SessionLocal() as session:
		for station_id in ids:
				try:
						run = start_manual_scan_run(
								session=session,
								station_id=station_id,
								max_pages=max(1, max_pages),
								force_rescan=True,
						)
						status = str(getattr(run, 'status', 'other') or 'other')
						if status in counts:
								counts[status] += 1
						else:
								counts['other'] += 1
				except Exception:
						counts['errors'] += 1

print(f"batch_summary awaiting_review={counts['awaiting_review']} blocked={counts['blocked']} other={counts['other']} errors={counts['errors']}")
PY

	OFFSET="$END"
	echo "$OFFSET" > "$OFFSET_FILE"
	echo "[$(date -Is)] progress offset=$OFFSET/$TOTAL" | tee -a "$LOG_FILE"

	sleep "$SLEEP_SECONDS"
done

echo "[$(date -Is)] rescan daemon finished offset=$OFFSET/$TOTAL" | tee -a "$LOG_FILE"
