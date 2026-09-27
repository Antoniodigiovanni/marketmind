#!/usr/bin/env bash
set -euo pipefail

install_dir=${MARKETMIND_INSTALL_DIR:-/srv/marketmind}
cd "$install_dir/releases/current"
export PGPASSFILE="$install_dir/secrets/app_pgpass"
export MARKETMIND_MCP_TOKEN_FILE="$install_dir/secrets/mcp_token"
export AI_API_KEY_FILE="$install_dir/secrets/llm_api_key"
export FRED_API_KEY_FILE="$install_dir/secrets/fred_api_key"
export MARKETMIND_PROCESSED_DIR="$install_dir/state/processed"
export MARKETMIND_UNIVERSE_DIR="$install_dir/state/universe"
export MARKETMIND_REFRESH_DIR="$install_dir/state/refresh"
export HOME="$install_dir/state"
export XDG_CACHE_HOME="$install_dir/state/cache"
export UV_CACHE_DIR="$install_dir/state/uv-cache"
export PYTHONDONTWRITEBYTECODE=1
export MARKETMIND_MCP_HOST=127.0.0.1

case "${1:-server}" in
  server) exec .venv/bin/python -m server.mcp ;;
  daily) exec .venv/bin/python -m agent.catchup ;;
  universe)
    exec flock -n "$install_dir/state/refresh/daily-refresh.lock" \
      bash -c '.venv/bin/python -m data.universe.update &&
        exec .venv/bin/python -m data.universe.resolve' ;;
  profiles) exec flock -n "$install_dir/state/refresh/daily-refresh.lock" \
    .venv/bin/python -m agent.ingestion "$(date -I)" --backfill --profiles-only ;;
  financials) exec flock -n "$install_dir/state/refresh/daily-refresh.lock" \
    .venv/bin/python -m data.filings.backfill --financials ;;
  esef) exec flock -n "$install_dir/state/refresh/daily-refresh.lock" \
    .venv/bin/python -m data.filings.esef ;;
  backfill)
    [[ ! -f "$install_dir/state/refresh/initial-backfill.complete" ]] || exit 0
    exec flock "$install_dir/state/refresh/daily-refresh.lock" bash -c '
      .venv/bin/python -m data.universe.resolve &&
      .venv/bin/python -m data.filings.backfill --issuers &&
      .venv/bin/python -m data.filings.backfill --financials &&
      .venv/bin/python -m data.filings.esef &&
      touch "$1/state/refresh/initial-backfill.complete"' bash "$install_dir" ;;
  *) printf 'Unknown job: %s\n' "$1" >&2; exit 2 ;;
esac
