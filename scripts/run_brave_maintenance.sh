#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"

source .venv/bin/activate

# 900 searches / 32 days = 28.125/day
# Use 28 daily, and every 8th UTC day run 29.
day_of_year="$(date -u +%j)"
queries=28
if (( day_of_year % 8 == 0 )); then
  queries=29
fi

if [[ -n "${MAX_QUERIES_OVERRIDE:-}" ]]; then
  queries="${MAX_QUERIES_OVERRIDE}"
fi

radio-db enrich-priority --max-queries "$queries"
radio-db stats
