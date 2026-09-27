#!/bin/sh
set -eu
job="${1:?usage: run-job.sh mcp|daily|universe|profiles|financials|esef|backfill|backup}"
install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
export MARKETMIND_INSTALL_DIR="$install_dir"
# Releases keep code and the frozen environment under releases/current; mutable
# data and credentials remain under the installation root.
if [ -d "$install_dir/releases/current" ]; then
  cd "$install_dir/releases/current"
else
  cd "$install_dir"
fi
# .env is operator-owned configuration, not an untrusted input file.
set -a
. "$install_dir/.env"
set +a
: "${MARKETMIND_DATABASE_DSN:?configure a host-reachable PostgreSQL DSN in .env}"
export PGPASSFILE="$install_dir/secrets/app_pgpass"
export MARKETMIND_MCP_TOKEN_FILE="$install_dir/secrets/mcp_token"
export AI_API_KEY_FILE="$install_dir/secrets/llm_api_key"
export FRED_API_KEY_FILE="$install_dir/secrets/fred_api_key"
export MARKETMIND_REFRESH_DIR="$install_dir/state/refresh"
export MARKETMIND_UNIVERSE_DIR="$install_dir/state/universe"
export MARKETMIND_PROCESSED_DIR="$install_dir/state/processed"
export PYTHONDONTWRITEBYTECODE=1
export UV_CACHE_DIR="$install_dir/state/uv-cache"
lock="$MARKETMIND_REFRESH_DIR/daily-refresh.lock"
mkdir -p "$MARKETMIND_REFRESH_DIR" "$UV_CACHE_DIR"
case "$job" in
  mcp)
    export MARKETMIND_MCP_HOST=127.0.0.1
    exec uv run --frozen --no-sync python -m server.mcp
    ;;
  daily)
    uv run --frozen --no-sync python -m agent.catchup
    ;;
  universe)
    flock -n "$lock" sh -c '
      uv run --frozen --no-sync python -m data.universe.update &&
      uv run --frozen --no-sync python -m data.universe.resolve
    '
    ;;
  profiles)
    flock -n "$lock" uv run --frozen --no-sync \
      python -m agent.ingestion "$(date -I)" --backfill --profiles-only
    ;;
  financials)
    flock -n "$lock" uv run --frozen --no-sync \
      python -m data.filings.backfill --financials
    ;;
  esef)
    flock -n "$lock" uv run --frozen --no-sync python -m data.filings.esef
    ;;
  backfill)
    marker="$MARKETMIND_REFRESH_DIR/initial-backfill.complete"
    if [ -f "$marker" ]; then
      echo "initial backfill already completed"
      exit 0
    fi
    flock "$lock" sh -c '
      set -eu
      marker="$MARKETMIND_REFRESH_DIR/initial-backfill.complete"
      [ ! -f "$marker" ] || exit 0
      uv run --frozen --no-sync python -m data.universe.resolve
      uv run --frozen --no-sync python -m data.filings.backfill --issuers
      uv run --frozen --no-sync python -m data.filings.backfill --financials
      uv run --frozen --no-sync python -m data.filings.esef
      touch "$marker"
    '
    ;;
  backup)
    flock -n "$lock" "$install_dir/deploy/backup-set.sh"
    ;;
  *)
    echo "unknown job: $job" >&2
    exit 2
    ;;
esac
