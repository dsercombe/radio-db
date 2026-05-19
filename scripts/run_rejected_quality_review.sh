#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

WORKER_ID="${REJECTED_REVIEW_WORKER_ID:-0}"
LOCK_FILE="${REJECTED_REVIEW_LOCK_FILE:-/tmp/radio-db-rejected-review-${WORKER_ID}.lock}"

# Prevent overlapping runs per worker instance.
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "rejected-review worker=${WORKER_ID} already running, skip"
  exit 0
fi

REVIEW_LIMIT="${REJECTED_SCAN_REVIEW_LIMIT:-30}"
PROVIDER="${STATION_QUALITY_PROVIDER:-}"
GLOBAL_TIMEOUT_SECONDS="${REJECTED_REVIEW_GLOBAL_TIMEOUT_SECONDS:-420}"
STATE_FILE="${REJECTED_REVIEW_CIRCUIT_STATE_FILE:-.radio_db_state/rejected_review_circuit_${WORKER_ID}.json}"
FAILURE_THRESHOLD="${REJECTED_REVIEW_CIRCUIT_FAILURE_THRESHOLD:-3}"
COOLDOWN_SECONDS="${REJECTED_REVIEW_CIRCUIT_COOLDOWN_SECONDS:-300}"

now_epoch="$(date +%s)"
open_until_epoch=0
failures=0
if [ -f "$STATE_FILE" ]; then
  open_until_epoch="$(python3 - <<'PY' "$STATE_FILE"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, "r", encoding="utf-8"))
    print(int(data.get("open_until_epoch", 0)))
except Exception:
    print(0)
PY
)"
  failures="$(python3 - <<'PY' "$STATE_FILE"
import json, sys
path = sys.argv[1]
try:
    data = json.load(open(path, "r", encoding="utf-8"))
    print(int(data.get("consecutive_failures", 0)))
except Exception:
    print(0)
PY
)"
fi

if [ "$open_until_epoch" -gt "$now_epoch" ]; then
  echo "rejected-review worker=${WORKER_ID} circuit open until epoch=${open_until_epoch}, skip"
  exit 0
fi

set +e
timeout --signal=TERM --kill-after=20s "${GLOBAL_TIMEOUT_SECONDS}s" \
  radio-db review-rejected-quality --limit "$REVIEW_LIMIT" --provider "$PROVIDER"
code=$?
set -e

if [ "$code" -eq 0 ]; then
  python3 - <<'PY' "$STATE_FILE"
import json, os, sys, time
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(
    {
        "consecutive_failures": 0,
        "open_until_epoch": 0,
        "updated_at_epoch": int(time.time()),
        "last_error": "",
    },
    open(path, "w", encoding="utf-8"),
)
PY
  exit 0
fi

failures=$((failures + 1))
open_until=0
last_error="exit_code_${code}"
if [ "$code" -eq 124 ] || [ "$code" -eq 137 ]; then
  last_error="timeout"
fi
if [ "$failures" -ge "$FAILURE_THRESHOLD" ]; then
  open_until=$((now_epoch + COOLDOWN_SECONDS))
fi

python3 - <<'PY' "$STATE_FILE" "$failures" "$open_until" "$last_error"
import json, os, sys, time
path = sys.argv[1]
failures = int(sys.argv[2])
open_until = int(sys.argv[3])
last_error = str(sys.argv[4])
os.makedirs(os.path.dirname(path), exist_ok=True)
json.dump(
    {
        "consecutive_failures": failures,
        "open_until_epoch": open_until,
        "updated_at_epoch": int(time.time()),
        "last_error": last_error,
    },
    open(path, "w", encoding="utf-8"),
)
PY

# Keep timer healthy; failures are tracked via circuit state.
exit 0
