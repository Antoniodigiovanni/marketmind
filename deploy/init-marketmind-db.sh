#!/bin/sh
set -eu

# This runs only when the managed PostgreSQL volume is initialized. Reuse the
# dedicated password file to create the non-administrative application role.
# Hex passwords are required by the deployment guide.
database_password="$(cat /run/secrets/marketmind_database_password)"

case "$database_password" in
  "" | *[!0-9a-f]*)
    echo "marketmind_database_password must be a nonblank lowercase hex value" >&2
    exit 1
    ;;
esac

psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
  --set marketmind_password="$database_password" <<'SQL'
SELECT format('CREATE ROLE marketmind LOGIN PASSWORD %L', :'marketmind_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind')
\gexec
SELECT 'CREATE DATABASE marketmind OWNER marketmind'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'marketmind')
\gexec
SQL

psql --set ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname marketmind <<'SQL'
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
SQL
