#!/bin/sh
set -eu

install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
backup_dir="${MARKETMIND_BACKUP_DIR:-/srv/marketmind-backups}"
keep_days="${MARKETMIND_BACKUP_KEEP_DAYS:-14}"
remote="${MARKETMIND_BACKUP_REMOTE:?set MARKETMIND_BACKUP_REMOTE to an rclone destination}"
umask 077
mkdir -p "$backup_dir"
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
set_dir="$backup_dir/marketmind-$stamp"
mkdir "$set_dir"
trap 'exit 1' HUP INT TERM
trap 'rm -rf -- "$set_dir"' EXIT

cd "$install_dir"
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_dump -U marketmind -Fc marketmind > "$set_dir/postgres.dump"
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore --list < "$set_dir/postgres.dump" >/dev/null
tar -czf "$set_dir/files.tar.gz" data/raw state
tar -tzf "$set_dir/files.tar.gz" >/dev/null
(
  cd "$set_dir"
  sha256sum postgres.dump files.tar.gz > SHA256SUMS
  printf 'created_utc=%s\n' "$stamp" > MANIFEST
  printf 'image=%s\n' "$(docker compose -f "$install_dir/docker-compose.prod.yml" \
    config --format json | jq -r '.services.marketmind.image')" >> MANIFEST
)
mv "$set_dir" "$set_dir.ready"
set_dir="$set_dir.ready"
trap - HUP INT TERM EXIT
rclone copy "$set_dir" "$remote/$(basename "$set_dir")" --checksum
rclone check "$set_dir" "$remote/$(basename "$set_dir")" --download --one-way
find "$backup_dir" -maxdepth 1 -type d -name 'marketmind-*.ready' -mtime "+$keep_days" \
  -exec rm -rf -- {} +
