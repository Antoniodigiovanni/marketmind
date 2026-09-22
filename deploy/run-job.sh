#!/bin/sh
set -eu
job="${1:?usage: run-job.sh daily|universe|profiles|financials|esef|backfill|backup}"
install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
export MARKETMIND_INSTALL_DIR="$install_dir"
cd "$install_dir"
compose='docker compose -f docker-compose.prod.yml'
lock=state/refresh/daily-refresh.lock
case "$job" in
  daily)
    $compose run --rm marketmind uv run --frozen --no-sync python -m agent.catchup
    ;;
  universe)
    flock -n "$lock" sh -c \
      'docker compose -f docker-compose.prod.yml run --rm marketmind uv run --frozen --no-sync python -m data.universe.update && docker compose -f docker-compose.prod.yml run --rm marketmind uv run --frozen --no-sync python -m data.universe.resolve'
    ;;
  profiles)
    flock -n "$lock" $compose run --rm marketmind uv run --frozen --no-sync \
      python -m agent.ingestion "$(date -I)" --backfill --profiles-only
    ;;
  financials)
    flock -n "$lock" $compose run --rm marketmind uv run --frozen --no-sync \
      python -m data.filings.backfill --financials
    ;;
  esef)
    flock -n "$lock" $compose run --rm marketmind uv run --frozen --no-sync \
      python -m data.filings.esef
    ;;
  backfill)
    marker=state/refresh/initial-backfill.complete
    if [ -f "$marker" ]; then
      echo "initial backfill already completed"
      exit 0
    fi
    flock "$lock" sh -c '
      set -eu
      compose="docker compose -f docker-compose.prod.yml"
      $compose run --rm marketmind uv run --frozen --no-sync python -m data.universe.resolve
      $compose run --rm marketmind uv run --frozen --no-sync python -m data.filings.backfill --issuers
      $compose run --rm marketmind uv run --frozen --no-sync python -m data.filings.backfill --financials
      $compose run --rm marketmind uv run --frozen --no-sync python -m data.filings.esef
      touch state/refresh/initial-backfill.complete
    '
    ;;
  backup)
    flock -n "$lock" ./deploy/backup-set.sh
    ;;
  *)
    echo "unknown job: $job" >&2
    exit 2
    ;;
esac
