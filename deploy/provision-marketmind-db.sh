#!/bin/sh
set -eu

# Use a libpq service for the administrator connection. Configure it outside
# this repository with a matching owner-only pgpass file or local peer auth.
# Example: MARKETMIND_ADMIN_SERVICE=marketmind-admin ./deploy/provision-marketmind-db.sh
#          /srv/marketmind/secrets/marketmind_database_password
password_file="${1:-/srv/marketmind/secrets/marketmind_database_password}"
admin_service="${MARKETMIND_ADMIN_SERVICE:?set MARKETMIND_ADMIN_SERVICE to local-postgres or a libpq administrator service}"
case "$admin_service" in
  *[!a-zA-Z0-9_.-]* | '') echo 'invalid admin service name' >&2; exit 1 ;;
esac
[ -s "$password_file" ] || { echo "password file not found: $password_file" >&2; exit 1; }
[ "$(stat -c %a "$password_file")" = 600 ] || {
  echo 'database password file must be mode 0600' >&2; exit 1;
}
password="$(cat "$password_file")"
case "$password" in
  '' | *[!0-9a-f]*) echo 'database password must be lowercase hex' >&2; exit 1 ;;
esac
umask 077

# This opt-in path is ONLY for a fresh Debian/Ubuntu host. Debian's package
# postinst may initialize/start a cluster; never install packages on an
# existing PostgreSQL host. pgvector's package name is major-versioned.
if [ "${MARKETMIND_INSTALL_POSTGRES:-0}" = 1 ]; then
  major="${MARKETMIND_POSTGRES_MAJOR:?set MARKETMIND_POSTGRES_MAJOR for apt installation}"
  case "$major" in *[!0-9]* | '') echo 'invalid PostgreSQL major version' >&2; exit 1 ;; esac
  [ "$major" -ge 16 ] || { echo 'PostgreSQL 16 or newer is required' >&2; exit 1; }
  [ "$(id -u)" -eq 0 ] || { echo 'apt installation requires root' >&2; exit 1; }
  command -v apt-get >/dev/null || { echo 'apt-get is required' >&2; exit 1; }
  if command -v pg_lsclusters >/dev/null 2>&1 || command -v postgres >/dev/null 2>&1 || \
      dpkg-query -W -f='${Status}' 'postgresql-[0-9]*' 2>/dev/null | grep -q 'install ok installed'; then
    echo 'PostgreSQL already installed; refusing apt changes on an existing server' >&2
    exit 1
  fi
  apt-get update
  apt-get install -y "postgresql-$major" "postgresql-client-$major" \
    "postgresql-$major-pgvector"
elif [ "${MARKETMIND_INSTALL_POSTGRES:-0}" != 0 ]; then
  echo 'MARKETMIND_INSTALL_POSTGRES must be 0 or 1' >&2; exit 1
fi

# Service must connect to a maintenance database (e.g. postgres). Do not
# expose administrator passwords in arguments or SQL output.
admin_conn="service=$admin_service"
admin_psql() { psql "$@"; }
if [ "$admin_service" = local-postgres ]; then
  admin_conn='dbname=postgres'
  admin_psql() { sudo -n -u postgres psql "$@"; }
fi
[ "$(admin_psql -X -w -At -v ON_ERROR_STOP=1 -d "$admin_conn" \
  -c 'SELECT rolsuper FROM pg_roles WHERE rolname = current_user')" = t ] || {
  echo 'admin service must connect as a PostgreSQL superuser' >&2; exit 1;
}
# Check before any mutations: a pre-existing database must already belong to
# Marketmind, and a pre-existing role must not carry elevated privileges.
admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn" <<'SQL'
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
             WHERE d.datname = 'marketmind' AND r.rolname <> 'marketmind') THEN
    RAISE EXCEPTION 'marketmind database belongs to another role';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketmind'
             AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)) THEN
    RAISE EXCEPTION 'marketmind role has elevated privileges';
  END IF;
END $$;
SQL
{
  printf "\\set marketmind_password '%s'\n" "$password"
  cat <<'SQL'
SELECT format('CREATE ROLE marketmind LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'marketmind_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind')
\gexec
SELECT format('ALTER ROLE marketmind LOGIN PASSWORD %L', :'marketmind_password')
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind')
\gexec
SELECT 'CREATE DATABASE marketmind OWNER marketmind'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'marketmind')
\gexec
SQL
} | admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn"
unset password
admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn dbname=marketmind" <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
printf '%s\n' 'Marketmind database and extensions are ready.'
