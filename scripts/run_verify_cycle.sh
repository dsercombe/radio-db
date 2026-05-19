#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

VERIFY_BATCH_LIMIT="${VERIFY_BATCH_LIMIT:-300}"
VERIFY_ONLY_CANDIDATES="${VERIFY_ONLY_CANDIDATES:-true}"
VERIFY_FETCH_HOMEPAGES="${VERIFY_FETCH_HOMEPAGES:-true}"
VERIFY_MAX_PAGE_FETCHES_PER_BATCH="${VERIFY_MAX_PAGE_FETCHES_PER_BATCH:-30}"
VERIFY_APPLY="${VERIFY_APPLY:-false}"
VERIFY_USE_CHECKPOINT="${VERIFY_USE_CHECKPOINT:-true}"
VERIFY_RESET_CHECKPOINT="${VERIFY_RESET_CHECKPOINT:-false}"
VERIFY_BATCH_SLEEP_SEC="${VERIFY_BATCH_SLEEP_SEC:-2}"
VERIFY_MAX_BATCHES="${VERIFY_MAX_BATCHES:-0}"
LOCK_FILE="${VERIFY_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-verify-cycle.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"

mkdir -p "$APP_DIR/.radio_db_state"

_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "verify-cycle already running, skip"
  exit 0
fi

if [[ "$VERIFY_APPLY" == "true" ]]; then
  exec 8>"$GLOBAL_WRITE_LOCK_FILE"
  if ! flock -n 8; then
    umask "$_LOCK_UMASK"
    echo "heavy radio-db writer already running, skip verify-cycle"
    exit 0
  fi
fi
umask "$_LOCK_UMASK"

APP_DIR="$APP_DIR" \
VERIFY_BATCH_LIMIT="$VERIFY_BATCH_LIMIT" \
VERIFY_ONLY_CANDIDATES="$VERIFY_ONLY_CANDIDATES" \
VERIFY_FETCH_HOMEPAGES="$VERIFY_FETCH_HOMEPAGES" \
VERIFY_MAX_PAGE_FETCHES_PER_BATCH="$VERIFY_MAX_PAGE_FETCHES_PER_BATCH" \
VERIFY_APPLY="$VERIFY_APPLY" \
VERIFY_USE_CHECKPOINT="$VERIFY_USE_CHECKPOINT" \
VERIFY_RESET_CHECKPOINT="$VERIFY_RESET_CHECKPOINT" \
VERIFY_BATCH_SLEEP_SEC="$VERIFY_BATCH_SLEEP_SEC" \
VERIFY_MAX_BATCHES="$VERIFY_MAX_BATCHES" \
python -u - <<'PY'
import json
import os
import time

from radio_db.db import SessionLocal
from radio_db.services.verification import verify_real_stations

batch_limit = int(os.getenv("VERIFY_BATCH_LIMIT", "300"))
only_candidates = os.getenv("VERIFY_ONLY_CANDIDATES", "true").lower() == "true"
fetch_homepages = os.getenv("VERIFY_FETCH_HOMEPAGES", "true").lower() == "true"
max_page_fetches = int(os.getenv("VERIFY_MAX_PAGE_FETCHES_PER_BATCH", "30"))
apply_changes = os.getenv("VERIFY_APPLY", "true").lower() == "true"
use_checkpoint = os.getenv("VERIFY_USE_CHECKPOINT", "true").lower() == "true"
reset_checkpoint = os.getenv("VERIFY_RESET_CHECKPOINT", "false").lower() == "true"
sleep_sec = max(0.0, float(os.getenv("VERIFY_BATCH_SLEEP_SEC", "2")))
max_batches = int(os.getenv("VERIFY_MAX_BATCHES", "0"))

batch = 0
total_checked = 0
total_keep = 0
total_reject = 0
total_ambiguous = 0
total_applied_rejects = 0

while True:
    if max_batches > 0 and batch >= max_batches:
        break
    batch += 1
    with SessionLocal() as session:
        result = verify_real_stations(
            session=session,
            limit=batch_limit,
            only_candidates=only_candidates,
            fetch_homepages=fetch_homepages,
            max_page_fetches=max_page_fetches,
            apply=apply_changes,
            use_checkpoint=use_checkpoint,
            reset_checkpoint=(reset_checkpoint and batch == 1),
        )

    print(f"Batch {batch} result:", flush=True)
    print(json.dumps(result, indent=2), flush=True)

    checked = int(result.get("checked", 0))
    total_checked += checked
    total_keep += int(result.get("keep", 0))
    total_reject += int(result.get("reject", 0))
    total_ambiguous += int(result.get("ambiguous", 0))
    total_applied_rejects += int(result.get("applied_rejects", 0))

    if checked == 0:
        break
    if bool(result.get("checkpoint_completed", False)):
        break
    if sleep_sec > 0:
        time.sleep(sleep_sec)

print("Verify cycle summary:", flush=True)
print(
    json.dumps(
        {
            "batches": batch,
            "checked_total": total_checked,
            "keep_total": total_keep,
            "reject_total": total_reject,
            "ambiguous_total": total_ambiguous,
            "applied_rejects_total": total_applied_rejects,
            "batch_limit": batch_limit,
            "fetch_homepages": fetch_homepages,
            "max_page_fetches_per_batch": max_page_fetches,
            "checkpoint_enabled": use_checkpoint,
        },
        indent=2,
    ),
    flush=True,
)
PY

radio-db stats
