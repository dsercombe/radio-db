#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/radio-database}"
cd "$APP_DIR"
source .venv/bin/activate

mkdir -p .radio_db_state
LOCK_FILE="${LOCK_FILE:-$APP_DIR/.radio_db_state/radio-db-form-cycle.lock}"
if ! { exec 9>"$LOCK_FILE"; } 2>/dev/null; then
  LOCK_FILE="$APP_DIR/.radio_db_state/radio-db-form-cycle.$(id -u).lock"
  exec 9>"$LOCK_FILE"
fi
if ! flock -n 9; then
  echo "radio-db form cycle already running; skip"
  exit 0
fi

FORM_STATION_LIMIT="${FORM_STATION_LIMIT:-80}"
FORM_MODE="${FORM_MODE:-read}"
FORM_MAX_PER_STATION="${FORM_MAX_PER_STATION:-5}"
FORM_COUNTRIES="${FORM_COUNTRIES:-US,GB,DE,FR,CA,AU,NL,SE,NO,ES,IT,AT,CH,BE,IE,JP,KR,BR,MX,AR,CO,PL,PT,DK,FI,NZ,ZA,IN}"

if [[ -z "${AGENT_MODE:-}" && -f ".env" ]]; then
  AGENT_MODE="$(awk -F= '/^AGENT_MODE=/{print $2; exit}' .env)"
  AGENT_MODE="${AGENT_MODE%\"}"
  AGENT_MODE="${AGENT_MODE#\"}"
  AGENT_MODE="${AGENT_MODE%\'}"
  AGENT_MODE="${AGENT_MODE#\'}"
fi
AGENT_MODE="${AGENT_MODE:-on}"
AGENT_MODE="$(echo "$AGENT_MODE" | tr '[:upper:]' '[:lower:]' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"

if [[ "$AGENT_MODE" == "off" ]]; then
  echo "agent mode=off -> skip form cycle"
  exit 0
fi

radio-db run-country-form-cycle \
  --station-limit "$FORM_STATION_LIMIT" \
  --mode "$FORM_MODE" \
  --max-forms-per-station "$FORM_MAX_PER_STATION" \
  --countries "$FORM_COUNTRIES"

radio-db stats
