#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

LOCK_FILE="${CANDIDATE_REVIEW_CAMPAIGN_LOCK_FILE:-/tmp/radio-db-candidate-review-campaign.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"
LOG_DIR="${CANDIDATE_REVIEW_CAMPAIGN_LOG_DIR:-$APP_DIR/logs}"
POOLS="${CANDIDATE_REVIEW_POOLS:-active_main_rescan promoted_rejected llm_review_all needs_review_all needs_review_high_score queue_candidates_unscanned}"
BATCH_LIMIT="${CANDIDATE_REVIEW_BATCH_LIMIT:-50}"
MAX_PAGES="${CANDIDATE_REVIEW_MAX_PAGES:-8}"
SLEEP_SECONDS="${CANDIDATE_REVIEW_SLEEP_SECONDS:-5}"
MAX_BATCHES="${CANDIDATE_REVIEW_MAX_BATCHES:-0}"
WORKERS="${CANDIDATE_REVIEW_WORKERS:-1}"
PLAYWRIGHT_TIMEOUT_MS="${CANDIDATE_REVIEW_BROWSER_TIMEOUT_MS:-12000}"
STALE_CLAIM_MINUTES="${CANDIDATE_REVIEW_STALE_CLAIM_MINUTES:-90}"
BATCH_TIMEOUT_SECONDS="${CANDIDATE_REVIEW_BATCH_TIMEOUT_SECONDS:-7200}"

mkdir -p "$LOG_DIR" "$APP_DIR/.radio_db_state"
LOG_FILE="$LOG_DIR/candidate_review_campaign_$(date -u +%Y%m%dT%H%M%SZ).log"
DB_DIALECT="$(
  .venv/bin/python - <<'PY'
from radio_db.db import engine
print(engine.dialect.name)
PY
)"

_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "candidate-review-campaign already running"
  exit 0
fi

if [[ "$DB_DIALECT" == "sqlite" ]]; then
  exec 8>"$GLOBAL_WRITE_LOCK_FILE"
  if ! flock -n 8; then
    umask "$_LOCK_UMASK"
    echo "another heavy radio-db writer is running; candidate-review-campaign skipped"
    exit 0
  fi
fi
umask "$_LOCK_UMASK"

echo "== candidate review campaign start $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
echo "db_dialect=$DB_DIALECT pools=$POOLS batch_limit=$BATCH_LIMIT max_pages=$MAX_PAGES max_batches=$MAX_BATCHES workers=$WORKERS playwright_timeout_ms=$PLAYWRIGHT_TIMEOUT_MS stale_claim_minutes=$STALE_CLAIM_MINUTES batch_timeout_seconds=$BATCH_TIMEOUT_SECONDS" | tee -a "$LOG_FILE"

batch_count=0
while true; do
  progressed=0
  for pool in $POOLS; do
    stale_reset="$(
      POOL="$pool" STALE_CLAIM_MINUTES="$STALE_CLAIM_MINUTES" .venv/bin/python - <<'PY'
import os
from sqlalchemy import text
from radio_db.db import SessionLocal
pool = os.environ["POOL"]
minutes = int(os.environ["STALE_CLAIM_MINUTES"])
with SessionLocal() as session:
    result = session.execute(
        text(
            """
            UPDATE candidate_rescan_queue
            SET status='pending',
                claim_token=NULL,
                claimed_at=NULL,
                result_status=NULL,
                notes=substr(coalesce(notes,'') || '; auto_reset_stale_running_claim',1,500),
                updated_at=CURRENT_TIMESTAMP
            WHERE source_pool=:pool
              AND status='running'
              AND claimed_at < (CURRENT_TIMESTAMP - (:minutes * interval '1 minute'))
            """
        ),
        {"pool": pool, "minutes": minutes},
    )
    session.commit()
    print(result.rowcount if result.rowcount is not None else 0)
PY
    )"
    if [[ "${stale_reset:-0}" -gt 0 ]]; then
      echo "reset stale running claims pool=$pool count=$stale_reset threshold_minutes=$STALE_CLAIM_MINUTES" | tee -a "$LOG_FILE"
    fi

    if [[ "$MAX_BATCHES" -gt 0 && "$batch_count" -ge "$MAX_BATCHES" ]]; then
      echo "max_batches reached: $MAX_BATCHES" | tee -a "$LOG_FILE"
      exit 0
    fi

    pending="$(
      POOL="$pool" .venv/bin/python - <<'PY'
import os
from sqlalchemy import text
from radio_db.db import SessionLocal
with SessionLocal() as session:
    print(int(session.scalar(text("SELECT COUNT(*) FROM candidate_rescan_queue WHERE source_pool=:pool AND status='pending'"), {"pool": os.environ["POOL"]}) or 0))
PY
    )"
    if [[ "$pending" -le 0 ]]; then
      continue
    fi

    batch_count=$((batch_count + 1))
    progressed=1
    echo "== batch $batch_count pool=$pool pending_before=$pending start $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
    if [[ "$WORKERS" -gt 1 ]]; then
      BROWSER_TIMEOUT_MS="$PLAYWRIGHT_TIMEOUT_MS" \
      STATION_QUALITY_DEEP_FETCH_TIMEOUT_SECONDS="${STATION_QUALITY_DEEP_FETCH_TIMEOUT_SECONDS:-4}" \
      STATION_QUALITY_DEEP_MAX_PAGES="${STATION_QUALITY_DEEP_MAX_PAGES:-2}" \
        timeout --kill-after=30s "$BATCH_TIMEOUT_SECONDS" radio-db run-candidate-rescan-continuous \
          --pool "$pool" \
          --limit "$BATCH_LIMIT" \
          --workers "$WORKERS" \
          --max-pages "$MAX_PAGES" \
          --include-archived \
          --apply-status 2>&1 | tee -a "$LOG_FILE"
    else
      BROWSER_TIMEOUT_MS="$PLAYWRIGHT_TIMEOUT_MS" \
      STATION_QUALITY_DEEP_FETCH_TIMEOUT_SECONDS="${STATION_QUALITY_DEEP_FETCH_TIMEOUT_SECONDS:-4}" \
      STATION_QUALITY_DEEP_MAX_PAGES="${STATION_QUALITY_DEEP_MAX_PAGES:-2}" \
        timeout --kill-after=30s "$BATCH_TIMEOUT_SECONDS" radio-db run-candidate-rescan-batch \
          --pool "$pool" \
          --limit "$BATCH_LIMIT" \
          --max-pages "$MAX_PAGES" \
          --include-archived \
          --no-dry-run \
          --apply-status 2>&1 | tee -a "$LOG_FILE"
    fi
    echo "== batch $batch_count pool=$pool end $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
    sleep "$SLEEP_SECONDS"
  done

  if [[ "$progressed" -eq 0 ]]; then
    echo "no pending rows left for pools: $POOLS" | tee -a "$LOG_FILE"
    break
  fi
done

echo "== candidate review campaign end $(date -u +%FT%TZ) ==" | tee -a "$LOG_FILE"
