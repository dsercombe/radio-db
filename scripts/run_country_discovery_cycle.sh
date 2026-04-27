#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

LINKUP_FLAG="--include-linkup"
if [[ "${COUNTRY_DISCOVERY_INCLUDE_LINKUP:-true}" == "false" ]]; then
  LINKUP_FLAG="--no-include-linkup"
fi

radio-db run-country-discovery-cycle \
  --max-queries "${COUNTRY_DISCOVERY_MAX_QUERIES_PER_RUN:-8}" \
  --min-confidence "${COUNTRY_DISCOVERY_MIN_CONFIDENCE:-0.35}" \
  "${LINKUP_FLAG}"

radio-db stats
