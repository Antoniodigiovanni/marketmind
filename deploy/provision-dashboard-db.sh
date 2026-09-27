#!/bin/sh
set -eu

password_file="${1:-/srv/marketmind/secrets/dashboard_database_password}"
admin_service="${MARKETMIND_ADMIN_SERVICE:?set MARKETMIND_ADMIN_SERVICE to local-postgres or a libpq administrator service}"
case "$admin_service" in
  *[!a-zA-Z0-9_.-]* | '') echo 'invalid admin service name' >&2; exit 1 ;;
esac
[ -s "$password_file" ] || { echo "password file not found: $password_file" >&2; exit 1; }
[ "$(stat -c %a "$password_file")" = 600 ] || {
  echo 'dashboard password file must be mode 0600' >&2; exit 1;
}
password="$(cat "$password_file")"
case "$password" in
  '' | *[!0-9a-f]*) echo 'dashboard password must be lowercase hex' >&2; exit 1 ;;
esac
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
# Refuse to change a database owned by another project or a role that already
# has other privileges through membership. Keep the existing role's password
# on reruns if the same secret is supplied; never change unrelated roles.
admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn" <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
                 WHERE d.datname = 'marketmind' AND r.rolname = 'marketmind') THEN
    RAISE EXCEPTION 'marketmind database must exist and belong to marketmind';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'marketmind_ro'
             AND (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication)) THEN
    RAISE EXCEPTION 'marketmind_ro has unexpected privileges';
  END IF;
  IF EXISTS (SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member
             WHERE r.rolname = 'marketmind_ro') THEN
    RAISE EXCEPTION 'marketmind_ro belongs to another role';
  END IF;
END $$;
SQL
{
  printf "\\set dashboard_password '%s'\n" "$password"
  cat <<'SQL'
SELECT format('CREATE ROLE marketmind_ro LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'dashboard_password')
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind_ro')
\gexec
SELECT format('ALTER ROLE marketmind_ro LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L', :'dashboard_password')
WHERE EXISTS (SELECT FROM pg_roles WHERE rolname = 'marketmind_ro')
\gexec
REVOKE ALL PRIVILEGES ON DATABASE marketmind FROM marketmind_ro;
GRANT CONNECT ON DATABASE marketmind TO marketmind_ro;
SQL
} | admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn"
unset password
# Schema migrations must have completed before grants are applied. A missing
# table is an error, not a reason to broaden privileges.
admin_psql -X -w -q -v ON_ERROR_STOP=1 -d "$admin_conn dbname=marketmind" <<'SQL'
DO $$ BEGIN
  IF to_regclass('public.agent_runs') IS NULL THEN
    RAISE EXCEPTION 'public.agent_runs missing; run database migrations first';
  END IF;
END $$;
REVOKE ALL PRIVILEGES ON SCHEMA public FROM marketmind_ro;
REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM marketmind_ro;
GRANT USAGE ON SCHEMA public TO marketmind_ro;
GRANT SELECT ON TABLE public.agent_runs TO marketmind_ro;
SQL
printf '%s\n' 'Dashboard read-only role is ready.'
