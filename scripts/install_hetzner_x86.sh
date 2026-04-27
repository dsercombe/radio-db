#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${1:-$HOME/radio-database}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v apt-get >/dev/null 2>&1; then
  echo "This installer currently expects Debian/Ubuntu (apt-get)."
  exit 1
fi

if [[ "$(uname -m)" != "x86_64" ]]; then
  echo "Warning: target architecture is $(uname -m), expected x86_64."
fi

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip sqlite3 curl git rsync

mkdir -p "$APP_DIR"

# If script is run inside repo, sync into target directory.
if [[ -f "pyproject.toml" ]]; then
  rsync -a --delete --exclude '.venv' --exclude '__pycache__' --exclude '*.pyc' ./ "$APP_DIR"/
fi

cd "$APP_DIR"

$PYTHON_BIN -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -e .

if [[ ! -f .env && -f .env.example ]]; then
  cp .env.example .env
fi

radio-db init-db
radio-db seed-frontier

echo "Install complete."
echo "Next steps:"
echo "1) Edit $APP_DIR/.env and set OPENAI_API_KEY / BRAVE_API_KEY"
echo "2) Run: source $APP_DIR/.venv/bin/activate && radio-db ingest-free --limit 5000"
echo "3) Optional daemon mode: install systemd units from deploy/systemd"
