#!/usr/bin/env bash
set -euo pipefail

action=${1:-}
subaction=${2:-}
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MARKETMIND_INSTALL_DIR:-$(dirname "$SCRIPT_DIR")}"
DASHBOARD_UNIT=marketmind-dashboard.service

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ok() { printf '✓ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

wait_until_ready() {
  local attempt status=
  for ((attempt = 1; attempt <= 30; attempt++)); do
    status=$(curl -sS -o /dev/null -w '%{http_code}' \
      http://127.0.0.1:8001/ 2>/dev/null || true)
    if [[ "$status" == 200 ]]; then return 0; fi
    sleep 2
  done
  die "dashboard did not respond with HTTP 200 within 60 seconds"
}

write_secret() {
  local path=$1 value=$2
  (umask 077; printf '%s\n' "$value" > "$path")
  chmod 0600 "$path"
}

env_value() {
  local key=$1 file="$INSTALL_DIR/.env"
  if [[ -n "${CREDENTIALS_DIRECTORY:-}" ]]; then file="$CREDENTIALS_DIRECTORY/dashboard_env"; fi
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' \
    "$file"
}

set_env() {
  local key=$1 value=$2 tmp
  tmp=$(mktemp)
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $0 ~ "^" key "=" { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$INSTALL_DIR/.env" > "$tmp"
  cat "$tmp" > "$INSTALL_DIR/.env"
  rm -f "$tmp"
}

prepare_shared_credentials() {
  local dashboard_dsn=$1
  local password=
  if [[ ! -s "$INSTALL_DIR/secrets/dashboard_database_password" ]]; then
    password=$(awk -F: \
      '$3 == "marketmind" && $4 == "marketmind_ro" \
        {print $5; exit}' "$INSTALL_DIR/secrets/dashboard_pgpass" 2>/dev/null || true)
    if [[ ! "$password" =~ ^[0-9a-f]+$ ]]; then
      password=$(openssl rand -hex 32)
    fi
    write_secret "$INSTALL_DIR/secrets/dashboard_database_password" "$password"
  fi
  password=$(< "$INSTALL_DIR/secrets/dashboard_database_password")
  local authority host port
  authority=${dashboard_dsn#*://}
  authority=${authority%%/*}
  host=${authority%%:*}
  port=${authority#*:}
  if [[ "$port" == "$authority" ]]; then port=5432; fi
  write_secret "$INSTALL_DIR/secrets/dashboard_pgpass" \
    "$host:$port:marketmind:marketmind_ro:$password"
  unset password
}

prepare_external_credentials() {
  local dashboard_dsn=$1 authority database_host database_port password escaped_password
  [[ -t 0 ]] || die "create secrets/dashboard_pgpass for the existing marketmind_ro role"
  printf '%s\n' \
    "The PostgreSQL administrator must first create marketmind_ro with CONNECT on marketmind," \
    "USAGE on public, and SELECT on agent_runs only."
  while [[ -z "${password:-}" ]]; do
    read -r -s -p "Password already assigned to marketmind_ro: " password
    printf '\n' >&2
  done
  authority=${dashboard_dsn#*://}
  authority=${authority%%/*}
  authority=${authority#*@}
  database_host=${authority%%:*}
  database_port=${authority#*:}
  if [[ "$database_port" == "$authority" ]]; then database_port=5432; fi
  escaped_password=${password//\\/\\\\}
  escaped_password=${escaped_password//:/\\:}
  write_secret "$INSTALL_DIR/secrets/dashboard_pgpass" \
    "$database_host:$database_port:marketmind:marketmind_ro:$escaped_password"
  unset password escaped_password
}

enable_dashboard() {
  local mode dashboard_dsn
  local command
  for command in awk curl install mktemp openssl psql systemctl; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required"
  done
  [[ -f "$INSTALL_DIR/.env" ]] || die "$INSTALL_DIR/.env does not exist"
  install -d -m 0700 "$INSTALL_DIR/secrets" "$INSTALL_DIR/state"

  dashboard_dsn=$(env_value MARKETMIND_DASHBOARD_DSN)
  if [[ -z "$dashboard_dsn" ]]; then
    die "set MARKETMIND_DASHBOARD_DSN to a host-reachable read-only database address"
  fi
  mode=$(cat "$INSTALL_DIR/state/deployment-mode" 2>/dev/null || true)
  if [[ "$mode" == shared-local ]]; then
    prepare_shared_credentials "$dashboard_dsn"
    export MARKETMIND_ADMIN_SERVICE=local-postgres
    "$SCRIPT_DIR/provision-dashboard-db.sh" \
      "$INSTALL_DIR/secrets/dashboard_database_password"
  elif [[ ! -s "$INSTALL_DIR/secrets/dashboard_pgpass" ]]; then
    prepare_external_credentials "$dashboard_dsn"
  fi
  chmod 0600 "$INSTALL_DIR/secrets/dashboard_pgpass"
  command -v setfacl >/dev/null || die "install the acl package to enable read-only dashboard logs"
  sudo setfacl -m u:marketmind-dashboard:x "$INSTALL_DIR/state" \
    "$INSTALL_DIR/state/refresh"
  install -d -m 0700 "$INSTALL_DIR/state/refresh/logs"
  sudo setfacl -R -m u:marketmind-dashboard:rX "$INSTALL_DIR/state/refresh/logs"
  sudo setfacl -d -m u:marketmind-dashboard:rX "$INSTALL_DIR/state/refresh/logs"
  sudo systemctl enable "$DASHBOARD_UNIT"
  sudo systemctl restart "$DASHBOARD_UNIT"
  wait_until_ready
  printf 'yes\n' > "$INSTALL_DIR/state/dashboard-enabled"
  ok "Dashboard is running at http://127.0.0.1:8001"
  printf '  From another computer: ssh -N -L 8001:127.0.0.1:8001 %s@%s\n' \
    "$(id -un)" "$(hostname)"
}

disable_dashboard() {
  sudo systemctl stop "$DASHBOARD_UNIT"
  install -d -m 0700 "$INSTALL_DIR/state"
  printf 'no\n' > "$INSTALL_DIR/state/dashboard-enabled"
  ok "Dashboard is disabled; its database credential was retained."
}

dashboard_status() {
  systemctl status --no-pager "$DASHBOARD_UNIT"
}

tailscale_connected() {
  command -v tailscale >/dev/null 2>&1 && [[ -n $(tailscale ip -4 2>/dev/null) ]]
}

enable_tailscale() {
  local previous_port port=${1:-$(cat "$INSTALL_DIR/state/dashboard-tailscale-port" 2>/dev/null || true)}
  port=${port:-443}
  [[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) || \
    die "Tailscale HTTPS port must be between 1 and 65535"
  [[ $(cat "$INSTALL_DIR/state/dashboard-enabled" 2>/dev/null || true) == yes ]] || \
    die "enable the dashboard before exposing it through Tailscale"
  tailscale_connected || die "Tailscale is not installed and connected on this host"
  wait_until_ready
  previous_port=$(cat "$INSTALL_DIR/state/dashboard-tailscale-port" 2>/dev/null || true)
  if [[ $(cat "$INSTALL_DIR/state/dashboard-tailscale-enabled" 2>/dev/null || true) == yes && \
        -n "$previous_port" && "$previous_port" != "$port" ]]; then
    if ! sudo tailscale serve --https="$previous_port" --set-path=/ off; then
      die "could not remove the previous Marketmind mapping on HTTPS port $previous_port"
    fi
    printf 'no\n' > "$INSTALL_DIR/state/dashboard-tailscale-enabled"
  fi
  if ! sudo tailscale serve --bg --https="$port" --set-path=/ http://127.0.0.1:8001; then
    die "Tailscale Serve could not publish the dashboard"
  fi
  printf '%s\n' "$port" > "$INSTALL_DIR/state/dashboard-tailscale-port"
  printf 'yes\n' > "$INSTALL_DIR/state/dashboard-tailscale-enabled"
  ok "Dashboard is available privately through Tailscale HTTPS on port $port."
  sudo tailscale serve status
}

disable_tailscale() {
  local port
  if [[ $(cat "$INSTALL_DIR/state/dashboard-tailscale-enabled" 2>/dev/null || true) != yes ]]; then
    ok "Marketmind has no managed Tailscale Serve mapping."
    return 0
  fi
  port=$(cat "$INSTALL_DIR/state/dashboard-tailscale-port" 2>/dev/null || true)
  port=${port:-443}
  command -v tailscale >/dev/null 2>&1 || \
    die "Tailscale is unavailable; the saved Serve mapping was not changed"
  if ! sudo tailscale serve --https="$port" --set-path=/ off; then
    warn "Tailscale Serve could not remove the dashboard mapping."
    return 1
  fi
  printf 'no\n' > "$INSTALL_DIR/state/dashboard-tailscale-enabled"
  ok "Removed Marketmind's Tailscale Serve mapping."
}

tailscale_status() {
  printf 'Marketmind-managed mapping: %s\n' \
    "$(cat "$INSTALL_DIR/state/dashboard-tailscale-enabled" 2>/dev/null || printf 'no')"
  printf 'HTTPS port: %s\n' \
    "$(cat "$INSTALL_DIR/state/dashboard-tailscale-port" 2>/dev/null || printf '443')"
  if command -v tailscale >/dev/null 2>&1; then
    sudo tailscale serve status
  else
    warn "Tailscale is not installed."
  fi
}

disable_dashboard_and_exposure() {
  if [[ $(cat "$INSTALL_DIR/state/dashboard-tailscale-enabled" 2>/dev/null || true) == yes ]]; then
    disable_tailscale || warn "Could not remove the Tailscale mapping; run the command again later."
  fi
  disable_dashboard
}

serve_dashboard() {
  [[ -n "${CREDENTIALS_DIRECTORY:-}" ]] || die "systemd credentials are required"
  export MARKETMIND_DASHBOARD_DSN="$(env_value MARKETMIND_DASHBOARD_DSN)"
  [[ -n "$MARKETMIND_DASHBOARD_DSN" ]] || die "dashboard DSN is not configured"
  [[ "$MARKETMIND_DASHBOARD_DSN" == *marketmind_ro@* ]] || \
    die "dashboard DSN must use marketmind_ro"
  export MARKETMIND_DASHBOARD_HOST=127.0.0.1
  export MARKETMIND_DASHBOARD_PORT="$(env_value MARKETMIND_DASHBOARD_PORT)"
  export MARKETMIND_DASHBOARD_PORT="${MARKETMIND_DASHBOARD_PORT:-8001}"
  export MARKETMIND_WEB_MCP_URL="http://127.0.0.1:$(env_value MARKETMIND_MCP_PORT)/mcp"
  if [[ "$MARKETMIND_WEB_MCP_URL" == 'http://127.0.0.1:/mcp' ]]; then
    export MARKETMIND_WEB_MCP_URL=http://127.0.0.1:8000/mcp
  fi
  export MARKETMIND_DASHBOARD_LOG_DIRS="$INSTALL_DIR/state/refresh/logs:/var/log/marketmind"
  export PGPASSFILE="$CREDENTIALS_DIRECTORY/dashboard_pgpass"
  export MARKETMIND_MCP_TOKEN_FILE="$CREDENTIALS_DIRECTORY/mcp_token"
  export HOME=/tmp UV_CACHE_DIR=/tmp/marketmind-uv-cache PYTHONDONTWRITEBYTECODE=1
  cd "$INSTALL_DIR/releases/current"
  exec .venv/bin/python -m dashboard.app
}

main() {
  [[ $(id -u) -ne 0 ]] || die "run this as the deployment account, not with sudo"
  case "$action" in
    serve) serve_dashboard ;;
    enable) enable_dashboard ;;
    disable) disable_dashboard_and_exposure ;;
    status) dashboard_status ;;
    tailscale)
      case "$subaction" in
        enable) enable_tailscale "${3:-}" ;;
        disable) disable_tailscale ;;
        status) tailscale_status ;;
        *) die "usage: dashboard.sh tailscale enable [https-port]|disable|status" ;;
      esac
      ;;
    *) die "usage: dashboard.sh enable|disable|status|tailscale" ;;
  esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
