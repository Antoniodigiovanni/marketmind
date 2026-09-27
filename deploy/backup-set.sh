#!/bin/sh
set -eu

install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
backup_dir="${MARKETMIND_BACKUP_DIR:-/srv/marketmind-backups}"
keep_days="${MARKETMIND_BACKUP_KEEP_DAYS:-14}"
remote="${MARKETMIND_BACKUP_REMOTE:?set MARKETMIND_BACKUP_REMOTE to an rclone destination}"
database_dsn="${MARKETMIND_DATABASE_DSN:?set MARKETMIND_DATABASE_DSN to a password-free libpq URI}"
case "$database_dsn" in
  *password=* | *://*:*@*) echo 'database DSN must not contain a password' >&2; exit 1 ;;
esac
case "$keep_days" in *[!0-9]* | '') echo 'invalid retention days' >&2; exit 1 ;; esac
[ -s "$install_dir/secrets/app_pgpass" ] || {
  echo 'missing secrets/app_pgpass' >&2; exit 1;
}
[ "$(stat -c %a "$install_dir/secrets/app_pgpass")" = 600 ] || {
  echo 'app_pgpass must be mode 0600' >&2; exit 1;
}
[ -d "$install_dir/data/raw" ] && [ -d "$install_dir/state" ] || {
  echo 'data/raw and state must exist' >&2; exit 1;
}
export PGPASSFILE="$install_dir/secrets/app_pgpass"
umask 077
mkdir -p "$backup_dir"
backup_dir="$(cd "$backup_dir" && pwd -P)"
[ "$(psql -X -w -At -v ON_ERROR_STOP=1 -d "$database_dsn" \
  -c 'SELECT current_database()')" = marketmind ] || {
  echo 'database DSN must connect to marketmind' >&2; exit 1;
}
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
set_dir="$backup_dir/marketmind-$stamp"
mkdir "$set_dir"
cleanup() { rm -rf -- "$set_dir"; }
trap 'exit 1' HUP INT TERM
trap cleanup EXIT

# Only dump the Marketmind database. The shared cluster is never stopped or
# modified. The caller should hold the daily-refresh lock while creating the
# file archive so this is one recoverable application/database set.
pg_dump -w --dbname "$database_dsn" --format=custom --file "$set_dir/postgres.dump"
pg_restore --list "$set_dir/postgres.dump" >/dev/null
client_major="$(pg_dump --version | awk '{split($NF, version, "."); print version[1]}')"
case "$client_major" in *[!0-9]* | '') echo 'invalid pg_dump version' >&2; exit 1 ;; esac
(cd "$install_dir" && tar -czf "$set_dir/files.tar.gz" data/raw state)
tar -tzf "$set_dir/files.tar.gz" >/dev/null
(
  cd "$set_dir"
  sha256sum postgres.dump files.tar.gz > SHA256SUMS
  printf 'created_utc=%s\npg_major=%s\n' "$stamp" "$client_major" > MANIFEST
)
mv "$set_dir" "$set_dir.ready"
set_dir="$set_dir.ready"
# On remote failure, retain the complete local set for manual retry. Prune
# only after the copy and a download-backed verification succeed.
trap - HUP INT TERM EXIT
rclone copy "$set_dir" "$remote/$(basename "$set_dir")" --checksum
rclone check "$set_dir" "$remote/$(basename "$set_dir")" --download --one-way
find "$backup_dir" -maxdepth 1 -type d -name 'marketmind-*.ready' \
  -mtime "+$keep_days" -exec rm -rf -- {} +
printf 'Backup set verified: %s\n' "$set_dir"
