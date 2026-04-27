#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

PEOPLE_LIMIT="${PEOPLE_LIMIT:-800}"
PEOPLE_DISCOVERY_STATIONS="${PEOPLE_DISCOVERY_STATIONS:-30}"
PEOPLE_DISCOVERY_QUERIES="${PEOPLE_DISCOVERY_QUERIES:-1}"
PEOPLE_PREFS_LIMIT="${PEOPLE_PREFS_LIMIT:-80}"
PEOPLE_PREFS_QUERIES_PER_PERSON="${PEOPLE_PREFS_QUERIES_PER_PERSON:-1}"
PEOPLE_PREFS_MIN_HITS_FOR_UPDATE="${PEOPLE_PREFS_MIN_HITS_FOR_UPDATE:-1}"
PROFILE_LIMIT="${PROFILE_LIMIT:-400}"

if [[ "$PEOPLE_LIMIT" -gt 0 ]]; then
  radio-db build-people --limit "$PEOPLE_LIMIT"
fi

if [[ "$PEOPLE_DISCOVERY_STATIONS" -gt 0 ]]; then
  radio-db people-discovery \
    --station-limit "$PEOPLE_DISCOVERY_STATIONS" \
    --queries-per-station "$PEOPLE_DISCOVERY_QUERIES"
fi

if [[ "$PEOPLE_PREFS_LIMIT" -gt 0 ]]; then
  radio-db people-enrich-preferences \
    --people-limit "$PEOPLE_PREFS_LIMIT" \
    --queries-per-person "$PEOPLE_PREFS_QUERIES_PER_PERSON" \
    --min-hits-for-update "$PEOPLE_PREFS_MIN_HITS_FOR_UPDATE"
fi

if [[ "$PROFILE_LIMIT" -gt 0 ]]; then
  radio-db build-profiles --limit "$PROFILE_LIMIT"
fi
radio-db stats
