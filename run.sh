#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — please fill in your tokens, then re-run."
  exit 1
fi

# Source .env to check token
set -a; source .env; set +a
if [ "$THOUSANDEYES_API_TOKEN" = "your_token_here" ]; then
  echo "Error: Update THOUSANDEYES_API_TOKEN in .env"
  exit 1
fi

MODE="${1:-docker}"

case "$MODE" in
  docker)
    docker compose build
    docker compose run --rm rca-agent "$@"
    ;;
  local)
    pip install -q -r requirements.txt
    python3 agent.py "${@:2}"
    ;;
  *)
    echo "Usage: ./run.sh [docker|local] [--query 'your question']"
    ;;
esac
