#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
cd "$APP_DIR"

source .venv/bin/activate

# Phase A: free bulk ingestion (cheap) - optional, disabled by default for daily cycles
if [[ "${INGEST_LIMIT:-0}" -gt 0 ]]; then
  radio-db ingest-free --limit "${INGEST_LIMIT:-0}"
fi

# Phase B: paid enrichment (rate-sensitive)
DISCOVERY_CREATE_NEW_STATIONS=false \
radio-db enrich-priority --max-queries "${ENRICH_QUERIES:-10}"

# Phase C: targeted domain-scoped enrichment on existing stations
if [[ "${STATION_ENRICH_STATIONS:-0}" -gt 0 ]]; then
  radio-db station-enrich-search \
    --station-limit "${STATION_ENRICH_STATIONS:-120}" \
    --queries-per-station "${STATION_ENRICH_QUERIES:-2}" \
    --min-confidence "${STATION_ENRICH_MIN_CONFIDENCE:-0.35}"
fi

# Phase D: consolidate people entities from contacts/evidence
radio-db build-people --limit "${PEOPLE_LIMIT:-800}"

# Phase E: explicit people discovery via search sources (incl. LinkedIn URLs)
radio-db people-discovery --station-limit "${PEOPLE_DISCOVERY_STATIONS:-120}" --queries-per-station "${PEOPLE_DISCOVERY_QUERIES:-2}"

# Phase E2: optional preference enrichment for existing people
if [[ "${PEOPLE_PREFS_LIMIT:-0}" -gt 0 ]]; then
  radio-db people-enrich-preferences \
    --people-limit "${PEOPLE_PREFS_LIMIT:-120}" \
    --queries-per-person "${PEOPLE_PREFS_QUERIES_PER_PERSON:-1}" \
    --min-hits-for-update "${PEOPLE_PREFS_MIN_HITS_FOR_UPDATE:-1}"
fi

# Phase F: aggregate multi-source facts into profile snapshots
radio-db build-profiles --limit "${PROFILE_LIMIT:-500}"

# Snapshot stats
radio-db stats
