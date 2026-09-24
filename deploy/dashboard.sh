#!/usr/bin/env bash
set -euo pipefail

action=${1:-}
subaction=${2:-}
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MARKETMIND_INSTALL_DIR:-$(dirname "$SCRIPT_DIR")}"
POSTGRES_INSTALL_DIR="${MARKETMIND_POSTGRES_DIR:-/srv/postgres}"
COMPOSE=(docker compose -f "$INSTALL_DIR/docker-compose.prod.yml" --profile dashboard)

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ok() { printf '✓ %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }

wait_until_ready() {
  local attempt status=
  for ((attempt = 1; attempt <= 30; attempt++)); do
    status=$(curl -sS -o /dev/null -w '%{http_code}' \
      http://127.0.0.1:8001/ 2>/dev/null || true)
    if [[ "$status" == 401 ]]; then return 0; fi
    sleep 2
  done
  die "dashboard did not respond with HTTP 401 within 60 seconds"
}

write_secret() {
  local path=$1 value=$2
  (umask 077; printf '%s\n' "$value" > "$path")
  chmod 0600 "$path"
}

env_value() {
  local key=$1
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' \
    "$INSTALL_DIR/.env"
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
  local password=
  if [[ ! -s "$INSTALL_DIR/secrets/dashboard_database_password" ]]; then
    password=$(awk -F: \
      '$1 == "postgres-shared" && $3 == "marketmind" && $4 == "marketmind_ro" \
        {print $5; exit}' "$INSTALL_DIR/secrets/dashboard_pgpass" 2>/dev/null || true)
    if [[ ! "$password" =~ ^[0-9a-f]+$ ]]; then
      password=$(openssl rand -hex 32)
    fi
    write_secret "$INSTALL_DIR/secrets/dashboard_database_password" "$password"
  fi
  password=$(< "$INSTALL_DIR/secrets/dashboard_database_password")
  write_secret "$INSTALL_DIR/secrets/dashboard_pgpass" \
    "postgres-shared:5432:marketmind:marketmind_ro:$password"
  unset password
}

prepare_browser_credential() {
  if [[ ! -s "$INSTALL_DIR/secrets/dashboard_token" ]]; then
    write_secret "$INSTALL_DIR/secrets/dashboard_token" "$(openssl rand -hex 32)"
  fi
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
  for command in awk curl docker install mktemp openssl; do
    command -v "$command" >/dev/null 2>&1 || die "$command is required"
  done
  docker info >/dev/null 2>&1 || die "the current account cannot access the Docker daemon"
  [[ -f "$INSTALL_DIR/.env" ]] || die "$INSTALL_DIR/.env does not exist"
  [[ -f "$INSTALL_DIR/docker-compose.prod.yml" ]] || \
    die "$INSTALL_DIR/docker-compose.prod.yml does not exist"
  install -d -m 0700 "$INSTALL_DIR/secrets" "$INSTALL_DIR/state"

  dashboard_dsn=$(env_value MARKETMIND_DASHBOARD_DSN)
  if [[ -z "$dashboard_dsn" ]]; then
    dashboard_dsn=postgresql://marketmind_ro@postgres-shared:5432/marketmind
    set_env MARKETMIND_DASHBOARD_DSN "$dashboard_dsn"
  fi
  prepare_browser_credential

  mode=$(cat "$INSTALL_DIR/state/deployment-mode" 2>/dev/null || true)
  if [[ "$mode" == shared-local ]]; then
    prepare_shared_credentials
    "$SCRIPT_DIR/provision-dashboard-db.sh" \
      "$POSTGRES_INSTALL_DIR" "$INSTALL_DIR/secrets/dashboard_database_password"
  elif [[ ! -s "$INSTALL_DIR/secrets/dashboard_pgpass" ]]; then
    prepare_external_credentials "$dashboard_dsn"
  fi
  chmod 0600 "$INSTALL_DIR/secrets/dashboard_token" \
    "$INSTALL_DIR/secrets/dashboard_pgpass"

  "${COMPOSE[@]}" config --quiet
  "${COMPOSE[@]}" up -d --wait --wait-timeout 60 dashboard
  wait_until_ready
  printf 'yes\n' > "$INSTALL_DIR/state/dashboard-enabled"
  ok "Dashboard is running at http://127.0.0.1:8001"
  printf '  From another computer: ssh -N -L 8001:127.0.0.1:8001 %s@%s\n' \
    "$(id -un)" "$(hostname)"
  printf '  Browser password: cat %s/secrets/dashboard_token\n' "$INSTALL_DIR"
}

disable_dashboard() {
  "${COMPOSE[@]}" stop dashboard >/dev/null 2>&1 || true
  install -d -m 0700 "$INSTALL_DIR/state"
  printf 'no\n' > "$INSTALL_DIR/state/dashboard-enabled"
  ok "Dashboard is disabled; its credentials were retained."
}

dashboard_status() {
  "${COMPOSE[@]}" ps dashboard
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

main() {
  [[ $(id -u) -ne 0 ]] || die "run this as the deployment account, not with sudo"
  case "$action" in
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
