#!/bin/sh
set -eu

postgres_dir="${1:-/srv/postgres}"
password_file="${2:-/srv/marketmind/secrets/marketmind_database_password}"

[ -f "$postgres_dir/docker-compose.yml" ] || {
  echo "shared PostgreSQL Compose file not found: $postgres_dir/docker-compose.yml" >&2
  exit 1
}
[ -s "$password_file" ] || {
  echo "Marketmind database password file not found: $password_file" >&2
  exit 1
}

database_password="$(cat "$password_file")"
case "$database_password" in
  "" | *[!0-9a-f]*)
    echo "marketmind_database_password must be a nonblank lowercase hex value" >&2
    exit 1
    ;;
esac

postgres_compose() {
  docker compose --project-directory "$postgres_dir" \
    -f "$postgres_dir/docker-compose.yml" "$@"
}

# Feed the password over stdin so it does not appear in the host process list.
# Local psql runs inside the database container as the cluster administrator.
{
  printf "\\set marketmind_password '%s'\n" "$database_password"
  cat <<'SQL'
SELECT format('CREATE ROLE marketmind LOGIN PASSWORD %L', :'marketmind_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind')
\gexec
SELECT format('ALTER ROLE marketmind LOGIN PASSWORD %L', :'marketmind_password')
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind')
\gexec
SELECT 'CREATE DATABASE marketmind OWNER marketmind'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'marketmind')
\gexec
ALTER DATABASE marketmind OWNER TO marketmind;
SQL
} | postgres_compose exec -T postgres \
  psql --set ON_ERROR_STOP=1 --username postgres --dbname postgres

postgres_compose exec -T postgres \
  psql --set ON_ERROR_STOP=1 --username postgres --dbname marketmind <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL

echo "Marketmind database and role are ready."
