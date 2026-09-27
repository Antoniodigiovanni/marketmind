#!/bin/sh
set -eu

[ "$#" -eq 1 ] || { echo 'usage: restore-check.sh /path/to/marketmind-*.ready' >&2; exit 2; }
[ "$(id -u)" -ne 0 ] || {
  echo 'run as an unprivileged account, not root (initdb refuses root)' >&2; exit 1;
}
set_dir="$(cd "$1" && pwd -P)"
(cd "$set_dir" && sha256sum --check SHA256SUMS)
major="$(awk -F= '$1 == "pg_major" {print $2}' "$set_dir/MANIFEST")"
case "$major" in *[!0-9]* | '') echo 'invalid pg_major in MANIFEST' >&2; exit 1 ;; esac
bin_dir="/usr/lib/postgresql/$major/bin"
for command in initdb pg_ctl createdb pg_restore psql; do
  [ -x "$bin_dir/$command" ] || {
    echo "missing $bin_dir/$command; install PostgreSQL $major and postgresql-$major-pgvector" >&2
    exit 1
  }
done
[ -f "/usr/share/postgresql/$major/extension/vector.control" ] || {
  echo "postgresql-$major-pgvector is required for restore" >&2; exit 1;
}
# A private, unprivileged cluster with no TCP listener: never create a
# database in the production server, even when it is on this same host.
umask 077
temp_dir="$(mktemp -d /tmp/marketmind-restore.XXXXXXXX)"
mkdir "$temp_dir/socket" "$temp_dir/files"
cleanup() {
  if [ -f "$temp_dir/db/postmaster.pid" ]; then
    "$bin_dir/pg_ctl" -D "$temp_dir/db" -m immediate -w stop >/dev/null || true
  fi
  rm -rf -- "$temp_dir"
}
trap 'exit 1' HUP INT TERM
trap cleanup EXIT
# Do not restore file ownership, permission bits, or absolute paths from an
# archive. SHA256SUMS verifies transfer integrity, not source authenticity.
if tar -tzf "$set_dir/files.tar.gz" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  echo 'unsafe files archive paths' >&2; exit 1
fi
tar -xzf "$set_dir/files.tar.gz" --no-same-owner --no-same-permissions -C "$temp_dir/files"
[ -d "$temp_dir/files/data/raw" ] && [ -d "$temp_dir/files/state" ] || {
  echo 'file archive missing data/raw or state' >&2; exit 1;
}
"$bin_dir/initdb" -D "$temp_dir/db" -U restore_check --auth-local=trust \
  --auth-host=reject --no-instructions >/dev/null
"$bin_dir/pg_ctl" -D "$temp_dir/db" -o \
  "-c listen_addresses= -c unix_socket_directories=$temp_dir/socket -p 5432" \
  -l "$temp_dir/postgres.log" -w start >/dev/null
export PGHOST="$temp_dir/socket" PGPORT=5432 PGUSER=restore_check
unset PGSERVICE PGPASSFILE PGPASSWORD PGDATABASE PGHOSTADDR PGOPTIONS || true
"$bin_dir/createdb" -w marketmind_restore
"$bin_dir/pg_restore" -w -d marketmind_restore --exit-on-error --no-owner --no-acl \
  "$set_dir/postgres.dump"
"$bin_dir/psql" -X -w -v ON_ERROR_STOP=1 -d marketmind_restore -Atqc \
  'SELECT count(*) FROM public.agent_runs' >/dev/null
printf 'Isolated database and files restore passed: %s\n' "$set_dir"
