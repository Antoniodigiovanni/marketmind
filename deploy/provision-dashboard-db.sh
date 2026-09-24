#!/bin/sh
set -eu

postgres_dir="${1:-/srv/postgres}"
password_file="${2:-/srv/marketmind/secrets/dashboard_database_password}"

[ -f "$postgres_dir/docker-compose.yml" ] || {
  echo "shared PostgreSQL Compose file not found: $postgres_dir/docker-compose.yml" >&2
  exit 1
}
[ -s "$password_file" ] || {
  echo "dashboard database password file not found: $password_file" >&2
  exit 1
}

dashboard_password="$(cat "$password_file")"
case "$dashboard_password" in
  "" | *[!0-9a-f]*)
    echo "dashboard_database_password must be a nonblank lowercase hex value" >&2
    exit 1
    ;;
esac

postgres_compose() {
  docker compose --project-directory "$postgres_dir" \
    -f "$postgres_dir/docker-compose.yml" "$@"
}

# Create or rotate the login without putting its password in process arguments.
{
  printf "\\set dashboard_password '%s'\n" "$dashboard_password"
  cat <<'SQL'
SELECT format(
    'CREATE ROLE marketmind_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'dashboard_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind_ro')
\gexec
SELECT format(
    'ALTER ROLE marketmind_ro LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    :'dashboard_password'
)
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind_ro')
\gexec
REVOKE ALL PRIVILEGES ON DATABASE marketmind FROM marketmind_ro;
GRANT CONNECT ON DATABASE marketmind TO marketmind_ro;
SQL
} | postgres_compose exec -T postgres \
  psql --set ON_ERROR_STOP=1 --username postgres --dbname postgres

# The application creates its schema on first startup. Wait for that migration
# before restricting the role to the one table the dashboard is allowed to read.
attempt=0
while ! postgres_compose exec -T postgres \
  psql --set ON_ERROR_STOP=1 --username postgres --dbname marketmind \
    -Atqc "SELECT to_regclass('public.agent_runs') IS NOT NULL" | grep -qx t; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 45 ]; then
    echo "agent_runs was not created within 90 seconds; inspect the marketmind logs" >&2
    exit 1
  fi
  sleep 2
done

postgres_compose exec -T postgres \
  psql --set ON_ERROR_STOP=1 --username postgres --dbname marketmind <<'SQL'
REVOKE ALL PRIVILEGES ON SCHEMA public FROM marketmind_ro;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM marketmind_ro;
GRANT USAGE ON SCHEMA public TO marketmind_ro;
GRANT SELECT ON TABLE public.agent_runs TO marketmind_ro;
SQL

echo "Dashboard read-only role is ready."
