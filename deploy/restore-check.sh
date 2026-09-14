#!/bin/sh
set -eu

set_dir="${1:?usage: restore-check.sh /path/to/marketmind-*.ready}"
install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
cd "$set_dir"
sha256sum --check SHA256SUMS
pg_restore --list < postgres.dump >/dev/null
temp_dir="$(mktemp -d)"
restore_db="marketmind_restore_$(date +%s)_$$"
cleanup() {
  cd "$install_dir"
  docker compose -f docker-compose.prod.yml exec -T postgres \
    dropdb -U marketmind --if-exists "$restore_db" >/dev/null 2>&1 || true
  rm -rf -- "$temp_dir"
}
trap 'exit 1' HUP INT TERM
trap cleanup EXIT
tar -xzf files.tar.gz -C "$temp_dir"
test -d "$temp_dir/data/raw"
test -d "$temp_dir/state"
cd "$install_dir"
docker compose -f docker-compose.prod.yml exec -T postgres \
  createdb -U marketmind "$restore_db"
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U marketmind -d "$restore_db" --exit-on-error --no-owner \
  < "$set_dir/postgres.dump"
docker compose -f docker-compose.prod.yml exec -T postgres \
  psql -U marketmind -d "$restore_db" -Atc 'SELECT count(*) FROM agent_runs' >/dev/null
trap - EXIT HUP INT TERM
cleanup
