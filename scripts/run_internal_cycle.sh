#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

# Internal-only scan: sitemap/rss/contact/about pages, no paid/external search APIs.
INTERNAL_STATION_LIMIT="${INTERNAL_STATION_LIMIT:-8}"
INTERNAL_QUERIES_PER_STATION="${INTERNAL_QUERIES_PER_STATION:-1}"
INTERNAL_MIN_CONFIDENCE="${INTERNAL_MIN_CONFIDENCE:-0.35}"

ENABLE_BRAVE_SEARCH=false \
ENABLE_GOOGLE_CSE_SEARCH=false \
ENABLE_TAVILY_SEARCH=false \
ENABLE_GROK_SEARCH=false \
ENABLE_LINKUP_SEARCH=false \
ENABLE_DUCKDUCKGO_SEARCH=false \
STATION_ENRICH_USE_GOOGLE=false \
STATION_ENRICH_USE_TAVILY=false \
STATION_ENRICH_USE_GROK=false \
STATION_ENRICH_USE_LINKUP=false \
STATION_ENRICH_USE_DUCKDUCKGO=false \
STATION_ENRICH_USE_SITEMAP=true \
STATION_ENRICH_USE_RSS=true \
radio-db station-enrich-search \
  --station-limit "$INTERNAL_STATION_LIMIT" \
  --queries-per-station "$INTERNAL_QUERIES_PER_STATION" \
  --min-confidence "$INTERNAL_MIN_CONFIDENCE"

radio-db stats
