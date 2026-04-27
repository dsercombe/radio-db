#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

# API-paced cycle for Brave/Tavily budgets.
ENRICH_QUERIES="${ENRICH_QUERIES:-1}"
STATION_ENRICH_STATIONS="${STATION_ENRICH_STATIONS:-8}"
STATION_ENRICH_QUERIES="${STATION_ENRICH_QUERIES:-1}"
STATION_ENRICH_MIN_CONFIDENCE="${STATION_ENRICH_MIN_CONFIDENCE:-0.35}"

DISCOVERY_CREATE_NEW_STATIONS=false \
radio-db enrich-priority --max-queries "$ENRICH_QUERIES"

if [[ "$STATION_ENRICH_STATIONS" -gt 0 ]]; then
  STATION_ENRICH_USE_LINKUP=true \
  STATION_ENRICH_USE_SITEMAP=false \
  STATION_ENRICH_USE_RSS=false \
  radio-db station-enrich-search \
    --station-limit "$STATION_ENRICH_STATIONS" \
    --queries-per-station "$STATION_ENRICH_QUERIES" \
    --min-confidence "$STATION_ENRICH_MIN_CONFIDENCE"
fi

radio-db stats
