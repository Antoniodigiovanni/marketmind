#!/bin/sh
set -eu

set_dir="${1:?usage: restore-check.sh /path/to/marketmind-*.ready}"
install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
cd "$set_dir"
sha256sum --check SHA256SUMS
temp_dir="$(mktemp -d)"
restore_container="marketmind-restore-$(date +%s)-$$"
cleanup() {
  docker rm -fv "$restore_container" >/dev/null 2>&1 || true
  rm -rf -- "$temp_dir"
}
trap 'exit 1' HUP INT TERM
trap cleanup EXIT
tar -xzf files.tar.gz -C "$temp_dir"
test -d "$temp_dir/data/raw"
test -d "$temp_dir/state"
cd "$install_dir"
. ./release.env
: "${MARKETMIND_POSTGRES_IMAGE:?release.env has no PostgreSQL image}"
docker run --detach --name "$restore_container" \
  -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=marketmind_restore \
  "$MARKETMIND_POSTGRES_IMAGE" >/dev/null
attempt=0
until docker exec "$restore_container" pg_isready -U postgres -d marketmind_restore \
  >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then
    echo "temporary PostgreSQL did not become ready" >&2
    exit 1
  fi
  sleep 1
done
docker exec -i "$restore_container" \
  pg_restore -U postgres -d marketmind_restore --exit-on-error --no-owner --no-acl \
  < "$set_dir/postgres.dump"
docker exec "$restore_container" \
  psql -U postgres -d marketmind_restore -Atc 'SELECT count(*) FROM agent_runs' >/dev/null
trap - EXIT HUP INT TERM
cleanup
