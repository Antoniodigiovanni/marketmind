#!/usr/bin/env bash
set -euo pipefail

# Debian/Ubuntu host installer. PostgreSQL is independently managed: this
# script may bootstrap a new local cluster, but never replaces an existing one.
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_dir=$(dirname "$script_dir")
export MARKETMIND_INSTALL_DIR="$install_dir"

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ask() {
  local reply
  read -r -p "$1 [y/N]: " reply
  [[ "${reply,,}" == y || "${reply,,}" == yes ]]
}
setting() { awk -F= -v key="$1" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' "$install_dir/.env"; }
set_setting() {
  local key=$1 value=$2 tmp
  tmp=$(mktemp)
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $0 ~ "^" key "=" { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$install_dir/.env" > "$tmp"
  cat "$tmp" > "$install_dir/.env"
  rm -f "$tmp"
}
secret() {
  local path=$1
  if [[ ! -s "$path" ]]; then (umask 077; openssl rand -hex 32 > "$path"); fi
  chmod 0600 "$path"
}

[[ $(id -u) != 0 ]] || fail 'Run as the deployment account, not root.'
[[ -t 0 ]] || fail 'An interactive terminal is required.'
[[ -f "$install_dir/.env.example" ]] || fail 'Extract the native release archive first.'
[[ "$install_dir" != *[[:space:]]* ]] || fail 'Install path cannot contain whitespace.'
for cmd in apt-get apt-cache curl flock openssl psql pg_isready sudo systemctl uv; do
  command -v "$cmd" >/dev/null || {
    if [[ "$cmd" == psql || "$cmd" == pg_isready ]]; then
      sudo apt-get update
      sudo apt-get install -y postgresql-client
    else
      fail "Missing $cmd; install it and rerun. For uv see https://docs.astral.sh/uv/"
    fi
  }
done
sudo -v
install -d -m 0700 "$install_dir/secrets" "$install_dir/state" \
  "$install_dir/state/refresh" "$install_dir/state/universe" \
  "$install_dir/state/processed" "$install_dir/data/raw"
install -d -m 0755 "$install_dir/releases"
if [[ ! -f "$install_dir/.env" ]]; then
  install -m 0600 "$install_dir/.env.example" "$install_dir/.env"
fi
chmod 0600 "$install_dir/.env"

# An existing server is never installed over, upgraded, restarted, or modified
# unless the operator provides administrator access and explicitly opts in.
if ! command -v pg_lsclusters >/dev/null 2>&1 && \
   ! pg_isready -q -h 127.0.0.1 -p 5432; then
  if ask 'No local PostgreSQL was detected. Install PostgreSQL and pgvector with apt?'; then
    sudo apt-get update
    major=$(apt-cache depends postgresql | awk \
      '/^  Depends: postgresql-[0-9]+$/ {sub(/^.*postgresql-/, ""); print; exit}')
    [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 16 )) || \
      fail 'apt does not offer a supported PostgreSQL server (16 or newer).'
    if ! apt-cache show "postgresql-$major-pgvector" >/dev/null 2>&1; then
      fail "postgresql-$major-pgvector is unavailable in apt. Configure a trusted repository or provide an existing pgvector server; rerun afterward."
    fi
    sudo apt-get install -y "postgresql-$major" "postgresql-client-$major" \
      "postgresql-$major-pgvector"
    sudo systemctl enable --now postgresql
    printf 'shared-local\n' > "$install_dir/state/deployment-mode"
    export MARKETMIND_ADMIN_SERVICE=local-postgres
  fi
fi

if [[ -z "$(setting SEC_CONTACT_NAME)" || "$(setting SEC_CONTACT_NAME)" == 'Your Name' ]]; then
  read -r -p 'SEC contact name: ' contact_name
  [[ -n "$contact_name" && "$contact_name" != *$'\n'* && "$contact_name" != *'"'* ]] || fail 'Invalid SEC contact name.'
  set_setting SEC_CONTACT_NAME "\"$contact_name\""
fi
if [[ -z "$(setting SEC_CONTACT_EMAIL)" || "$(setting SEC_CONTACT_EMAIL)" == you@example.com ]]; then
  read -r -p 'SEC contact email: ' contact_email
  [[ "$contact_email" == *@* && "$contact_email" != *[[:space:]]* ]] || fail 'Invalid SEC contact email.'
  set_setting SEC_CONTACT_EMAIL "$contact_email"
fi

current_dsn=$(setting MARKETMIND_DATABASE_DSN)
read -r -p "Application PostgreSQL DSN [$current_dsn]: " dsn
dsn=${dsn:-$current_dsn}
[[ "$dsn" =~ ^postgresql://marketmind@([a-zA-Z0-9.-]+)(:([0-9]+))?/marketmind$ ]] || \
  fail 'Expected postgresql://marketmind@HOST:PORT/marketmind (no password in URL).'
host=${BASH_REMATCH[1]}
port=${BASH_REMATCH[3]:-5432}
set_setting MARKETMIND_DATABASE_DSN "$dsn"
set_setting MARKETMIND_DASHBOARD_DSN "postgresql://marketmind_ro@$host:$port/marketmind"

secret "$install_dir/secrets/mcp_token"
if [[ ! -s "$install_dir/secrets/marketmind_database_password" ]]; then
  if [[ -z "${MARKETMIND_ADMIN_SERVICE:-}" ]]; then
    read -r -s -p 'Existing marketmind database role password: ' password
    printf '\n'
    [[ -n "$password" ]] || fail 'Database role password is required.'
    (umask 077; printf '%s\n' "$password" > \
      "$install_dir/secrets/marketmind_database_password")
    unset password
  else
    secret "$install_dir/secrets/marketmind_database_password"
  fi
fi
if [[ ! -s "$install_dir/secrets/app_pgpass" ]]; then
  password=$(< "$install_dir/secrets/marketmind_database_password")
  password=${password//\\/\\\\}
  password=${password//:/\\:}
  (umask 077; printf '%s:%s:marketmind:marketmind:%s\n' "$host" "$port" "$password" \
    > "$install_dir/secrets/app_pgpass")
  unset password
fi
chmod 0600 "$install_dir/secrets/app_pgpass"
for spec in 'llm_api_key:Model provider API key' 'fred_api_key:FRED API key'; do
  file=${spec%%:*}
  [[ -s "$install_dir/secrets/$file" ]] && continue
  read -r -s -p "${spec#*:}: " value
  printf '\n'
  [[ -n "$value" ]] || fail "${spec#*:} is required."
  (umask 077; printf '%s\n' "$value" > "$install_dir/secrets/$file")
  unset value
done

if [[ $(cat "$install_dir/state/deployment-mode" 2>/dev/null || true) == shared-local ]]; then
  export MARKETMIND_ADMIN_SERVICE=local-postgres
fi
if [[ -n "${MARKETMIND_ADMIN_SERVICE:-}" ]]; then
  if ask 'Provision the dedicated marketmind database and role using the configured administrator connection?'; then
    "$script_dir/provision-marketmind-db.sh" \
      "$install_dir/secrets/marketmind_database_password"
  fi
else
  printf '%s\n' 'Existing server: provision the marketmind database/role and vector + pg_trgm' \
    'extensions as described in deploy/README.md. Set MARKETMIND_ADMIN_SERVICE' \
    'to a libpq administrator service to have this installer provision them.'
fi
PGPASSFILE="$install_dir/secrets/app_pgpass" \
  psql -X -w -v ON_ERROR_STOP=1 -At -d "$dsn" \
  -c "SELECT count(*) = 2 FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')" | \
  grep -qx t || fail 'Application connection or required extension check failed.'

# Install from a verified native release archive. The update command stages a
# locked environment and atomically switches the MCP service with health check.
if [[ ! -L "$install_dir/releases/current" ]]; then
  archive=$(find "$install_dir" -maxdepth 1 -name 'marketmind-v*-native.tar.gz' -print -quit)
  [[ -n "$archive" && -f "$archive.sha256" ]] || \
    fail 'Place the matching native release .tar.gz and .sha256 in the install directory.'
  "$script_dir/update.sh" "$archive"
else
  sudo systemctl restart marketmind-mcp.service
fi

# Seed immutable baselines once; an existing mutable universe is never replaced.
for name in sp500.csv eurostoxx600.csv delisted.csv; do
  if [[ ! -e "$install_dir/state/universe/$name" ]]; then
    install -m 0600 "$install_dir/releases/current/data/universe/$name" \
      "$install_dir/state/universe/$name"
  fi
done
install -d -m 0700 "$install_dir/state/refresh/logs"

# Job units are host processes with the same pinned release and shared state.
for name in daily universe profiles financials esef initial-backfill; do
  job=$name
  [[ "$name" == initial-backfill ]] && job=backfill
  target="/etc/systemd/system/marketmind-$name.service"
  tmp=$(mktemp)
  printf '[Unit]\nDescription=Marketmind %s job\nWants=network-online.target\nAfter=network-online.target\n\n' "$name" > "$tmp"
  printf '[Service]\nType=oneshot\nUser=%s\nGroup=%s\nEnvironmentFile=%s/.env\nEnvironment=MARKETMIND_INSTALL_DIR=%s\nExecStart=%s/releases/current/deploy/native-run.sh %s\nNoNewPrivileges=true\nUMask=0077\n' \
    "$(id -un)" "$(id -gn)" "$install_dir" "$install_dir" "$install_dir" "$job" >> "$tmp"
  sudo install -m 0644 "$tmp" "$target"
  rm -f "$tmp"
done
if ! id marketmind-dashboard >/dev/null 2>&1; then
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin marketmind-dashboard
fi
unit_tmp=$(mktemp)
sed "s|/srv/marketmind|$install_dir|g" \
  "$script_dir/systemd/marketmind-dashboard.service" > "$unit_tmp"
sudo install -m 0644 "$unit_tmp" /etc/systemd/system/marketmind-dashboard.service
rm -f "$unit_tmp"
backup_unit=$(mktemp)
printf '[Unit]\nDescription=Marketmind coordinated backup\nAfter=network-online.target\n\n' > "$backup_unit"
printf '[Service]\nType=oneshot\nUser=%s\nGroup=%s\nEnvironmentFile=%s/.env\nEnvironmentFile=-/etc/marketmind/backup.conf\nEnvironment=MARKETMIND_INSTALL_DIR=%s\nExecStart=%s/releases/current/deploy/run-job.sh backup\nNoNewPrivileges=true\nUMask=0077\n' \
  "$(id -un)" "$(id -gn)" "$install_dir" "$install_dir" "$install_dir" >> "$backup_unit"
sudo install -m 0644 "$backup_unit" /etc/systemd/system/marketmind-backup.service
rm -f "$backup_unit"
for name in daily universe profiles financials esef backup; do
  unit="marketmind-$name.timer"
  if [[ ! -f "/etc/systemd/system/$unit" ]]; then
    sudo install -m 0644 "$script_dir/systemd/$unit" "/etc/systemd/system/$unit"
  fi
done
sudo systemctl daemon-reload
sudo ln -sfn "$install_dir/deploy/marketmind.sh" /usr/local/bin/marketmind
printf '%s\n' 'Marketmind is installed. Verify authenticated MCP access before enabling job timers.'
printf '%s\n' 'New job timers, backups, and the optional dashboard remain disabled.'
