#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state logs
LOCK_FILE="${PHASE3_ENRICHMENT_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-phase3-enrichment.lock}"
GLOBAL_WRITE_LOCK_FILE="${RADIO_DB_HEAVY_WRITE_LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-heavy-write.lock}"
_LOCK_UMASK="$(umask)"
umask 000
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  umask "$_LOCK_UMASK"
  echo "phase3 enrichment already running, skip"
  exit 0
fi
exec 8>"$GLOBAL_WRITE_LOCK_FILE"
if ! flock -n 8; then
  umask "$_LOCK_UMASK"
  echo "heavy radio-db writer already running, skip phase3 enrichment"
  exit 0
fi
umask "$_LOCK_UMASK"

SEED_LIMIT="${PHASE3_ENRICHMENT_SEED_LIMIT:-300}"
MIN_PRIORITY="${PHASE3_ENRICHMENT_MIN_PRIORITY:-35}"
FREE_SCAN_LIMIT="${PHASE3_ENRICHMENT_FREE_SCAN_LIMIT:-40}"
FREE_MAX_URLS="${PHASE3_ENRICHMENT_FREE_MAX_URLS_PER_STATION:-8}"
API_SCAN_LIMIT="${PHASE3_ENRICHMENT_API_SCAN_LIMIT:-4}"
DYNAMIC_SCAN_LIMIT="${PHASE3_DYNAMIC_SCAN_LIMIT:-3}"
DYNAMIC_MAX_LINKS="${PHASE3_DYNAMIC_MAX_LINKS:-120}"
DYNAMIC_MAX_DEEP_PAGES="${PHASE3_DYNAMIC_MAX_DEEP_PAGES:-8}"
DYNAMIC_MAX_GEMINI_CALLS="${PHASE3_DYNAMIC_MAX_GEMINI_CALLS:-6}"
DYNAMIC_MIN_CONFIDENCE="${PHASE3_DYNAMIC_MIN_CONFIDENCE:-0.75}"
RETRIES="${PHASE3_ENRICHMENT_DB_LOCK_RETRIES:-3}"

# Phase 3 must not consume Phase 2's Brave budget. The dynamic official-site
# pass below uses its own small Gemini cap for structure routing/verdicts.
export ENABLE_BRAVE_SEARCH=false
export ENABLE_BRAVE_ANSWER=false
export ENABLE_GOOGLE_CSE_SEARCH=false
export ENABLE_GROK_SEARCH=false
export MAX_LLM_CALLS_PER_RUN="${PHASE3_DYNAMIC_MAX_GEMINI_CALLS:-6}"
export MAX_LLM_CALLS_PER_DAY="${PHASE3_DYNAMIC_MAX_LLM_CALLS_PER_DAY:-30}"
export MAX_DAILY_USD=0

# Use only low-risk/free enrichment sources in small batches.
export ENABLE_TAVILY_SEARCH="${ENABLE_TAVILY_SEARCH:-true}"
export ENABLE_LINKUP_SEARCH="${ENABLE_LINKUP_SEARCH:-true}"
export ENABLE_DUCKDUCKGO_SEARCH="${ENABLE_DUCKDUCKGO_SEARCH:-true}"
export STATION_ENRICH_USE_GOOGLE=false
export STATION_ENRICH_USE_TAVILY=true
export STATION_ENRICH_USE_LINKUP=true
export STATION_ENRICH_USE_DUCKDUCKGO=true
export STATION_ENRICH_USE_GROK=false
export STATION_ENRICH_MAX_PAGE_FETCHES="${STATION_ENRICH_MAX_PAGE_FETCHES:-25}"
export STATION_ENRICH_MAX_RESULTS_PER_QUERY="${STATION_ENRICH_MAX_RESULTS_PER_QUERY:-6}"

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
run_radio_db enrichment-free-scan --limit "$FREE_SCAN_LIMIT" --max-urls-per-station "$FREE_MAX_URLS"
if [[ "$DYNAMIC_MAX_GEMINI_CALLS" -gt 0 && "$DYNAMIC_SCAN_LIMIT" -gt 0 ]]; then
  run_radio_db phase3-dynamic-official-discovery \
    --limit "$DYNAMIC_SCAN_LIMIT" \
    --max-links "$DYNAMIC_MAX_LINKS" \
    --max-deep-pages "$DYNAMIC_MAX_DEEP_PAGES" \
    --max-gemini-calls "$DYNAMIC_MAX_GEMINI_CALLS" \
    --min-confidence "$DYNAMIC_MIN_CONFIDENCE" \
    --apply
fi
run_radio_db enrichment-paid-scan --limit "$API_SCAN_LIMIT"
run_radio_db enrichment-queue-stats
