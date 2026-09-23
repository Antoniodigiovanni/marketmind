#!/usr/bin/env bash
set -euo pipefail

# Interactive first-host installer. Run this as the deployment account; it
# requests sudo for host packages and host-level systemd and logrotate files.

if (( BASH_VERSINFO[0] < 4 )); then
  printf 'Error: this installer requires Bash 4 or newer.\n' >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="$(dirname "$SCRIPT_DIR")"
SYSTEMD_DIR=/etc/systemd/system
POSTGRES_INSTALL_DIR=/srv/postgres
DEPLOY_USER=$(id -un)
DEPLOY_GROUP=$(id -gn)
TIMER_NAMES=(daily universe profiles financials esef backup)

declare -A TIMER_LABELS=(
  [daily]="Daily refresh and catch-up"
  [universe]="Weekly universe refresh"
  [profiles]="Annual company profile refresh"
  [financials]="Quarterly company financials"
  [esef]="Monthly European filings"
  [backup]="Nightly off-host backup"
)
declare -A TIMER_DETAILS=(
  [daily]=$'The main freshness job. It resolves identities already in the stored universe,\nupdates prices, FX, macro data, dividends and short interest, ingests news and facts,\nstructures views, and fetches recent SEC filings. It catches up at most 7 missed days\nand can make many model-provider and market-data requests.'
  [universe]=$'Downloads the official S&P 500 and STOXX 600 constituent lists, records\nadditions and removals, and synchronizes identifiers and Yahoo listings. It does not\nrun the daily price/news pipeline. Weekly is enough because membership changes rarely.'
  [profiles]=$'Rebuilds the descriptive company profile for every active company and skips news.\nThis is slow and model-provider intensive, so the default cadence is annual.'
  [financials]=$'Reloads complete SEC Company Facts histories for tracked US issuers. Quarterly\nmatches the reporting cycle and avoids an expensive full-universe sweep every day.'
  [esef]=$'Scans filings.xbrl.org for new European filings from the configured countries and\narchives new packages. The broad index scan is monthly because filings are periodic.'
  [backup]=$'Creates a PostgreSQL dump plus an archive of raw and state files, verifies checksums,\nand copies the set to an rclone destination. Requires jq, rclone, and an off-host\ndestination. It remains disabled by default until a restore has been tested.'
)
declare -A TIMER_DEFAULT_ENABLED=(
  [daily]=yes [universe]=yes [profiles]=yes [financials]=yes [esef]=yes [backup]=no
)
declare -A TIMER_DEFAULT_SCHEDULE=(
  [daily]="*-*-* 06:10:00"
  [universe]="Mon *-*-* 06:00:00"
  [profiles]="*-01-05 06:30:00"
  [financials]="*-02,05,08,11-01 04:00:00"
  [esef]="*-*-01 05:00:00"
  [backup]="*-*-* 02:30:00"
)
declare -A SELECTED_ENABLED
declare -A SELECTED_SCHEDULE

if [[ -t 1 && -z "${NO_COLOR:-}" ]]; then
  bold=$'\033[1m'
  cyan=$'\033[36m'
  green=$'\033[32m'
  yellow=$'\033[33m'
  reset=$'\033[0m'
else
  bold= cyan= green= yellow= reset=
fi

say() { printf '%s\n' "$*"; }
heading() { printf '\n%s%s%s\n' "$bold" "$1" "$reset"; }
info() { printf '%s›%s %s\n' "$cyan" "$reset" "$*"; }
ok() { printf '%s✓%s %s\n' "$green" "$reset" "$*"; }
warn() { printf '%s!%s %s\n' "$yellow" "$reset" "$*" >&2; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

prompt_text() {
  local prompt=$1 default=${2:-} answer
  if [[ -n "$default" ]]; then
    read -r -p "$prompt [$default]: " answer
    printf '%s' "${answer:-$default}"
  else
    read -r -p "$prompt: " answer
    printf '%s' "$answer"
  fi
}

prompt_secret() {
  local prompt=$1 answer
  read -r -s -p "$prompt: " answer
  printf '\n' >&2
  printf '%s' "$answer"
}

prompt_yes_no() {
  local prompt=$1 default=$2 answer suffix
  if [[ "$default" == yes ]]; then suffix='Y/n'; else suffix='y/N'; fi
  while true; do
    read -r -p "$prompt [$suffix]: " answer
    answer=${answer:-$default}
    case "${answer,,}" in
      y|yes) return 0 ;;
      n|no) return 1 ;;
      *) warn "Please answer yes or no." ;;
    esac
  done
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "Required command not found: $1"
}

release_value() {
  local key=$1
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$INSTALL_DIR/release.env"
}

config_value() {
  local file=$1 key=$2
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); gsub(/^"|"$/, ""); print; exit}' \
    "$file"
}

env_value() {
  config_value "$INSTALL_DIR/.env" "$1"
}

set_config_value() {
  local file=$1 key=$2 value=$3 tmp
  tmp=$(mktemp)
  awk -v key="$key" -v value="$value" '
    BEGIN { found = 0 }
    $0 ~ "^" key "=" { print key "=" value; found = 1; next }
    { print }
    END { if (!found) print key "=" value }
  ' "$file" > "$tmp"
  cat "$tmp" > "$file"
  rm -f "$tmp"
}

set_env() {
  set_config_value "$INSTALL_DIR/.env" "$1" "$2"
}

write_secret() {
  local path=$1 value=$2 mode=${3:-0600}
  (umask 077; printf '%s\n' "$value" > "$path")
  chmod "$mode" "$path"
}

ensure_secret() {
  local filename=$1 label=$2 value
  if [[ -s "$INSTALL_DIR/secrets/$filename" ]]; then
    ok "Keeping existing $label."
    return
  fi
  while [[ -z "${value:-}" ]]; do value=$(prompt_secret "$label"); done
  write_secret "$INSTALL_DIR/secrets/$filename" "$value"
}

installed_schedule() {
  local name=$1 installed="$SYSTEMD_DIR/marketmind-$name.timer"
  if sudo test -r "$installed"; then
    sudo awk -F= '$1 == "OnCalendar" {value=$2} END {print value}' "$installed"
  fi
}

timer_is_enabled() {
  systemctl is-enabled --quiet "marketmind-$1.timer" 2>/dev/null
}

validate_calendar() {
  systemd-analyze calendar "$1" >/dev/null 2>&1
}

configure_timers() {
  local name label enabled schedule current detail_line
  heading "Schedules"
  say "Press Enter to accept each current default. Times use the homelab's local timezone."
  for name in "${TIMER_NAMES[@]}"; do
    label=${TIMER_LABELS[$name]}
    enabled=${TIMER_DEFAULT_ENABLED[$name]}
    if sudo test -e "$SYSTEMD_DIR/marketmind-$name.timer"; then
      if timer_is_enabled "$name"; then enabled=yes; else enabled=no; fi
    fi
    printf '\n%s%s%s\n' "$bold" "$label" "$reset"
    while IFS= read -r detail_line; do
      printf '  %s\n' "$detail_line"
    done <<< "${TIMER_DETAILS[$name]}"
    printf '  Bundled schedule: %s\n' "${TIMER_DEFAULT_SCHEDULE[$name]}"
    if prompt_yes_no "Enable $label?" "$enabled"; then
      SELECTED_ENABLED[$name]=yes
      current=$(installed_schedule "$name")
      schedule=${current:-${TIMER_DEFAULT_SCHEDULE[$name]}}
      while true; do
        schedule=$(prompt_text "When should it run? (systemd OnCalendar)" "$schedule")
        if validate_calendar "$schedule"; then break; fi
        warn "systemd does not recognize that calendar expression."
      done
      SELECTED_SCHEDULE[$name]=$schedule
      systemd-analyze calendar "$schedule" | sed -n 's/^ *Next elapse: /  Next run: /p'
    else
      SELECTED_ENABLED[$name]=no
      current=$(installed_schedule "$name")
      SELECTED_SCHEDULE[$name]=${current:-${TIMER_DEFAULT_SCHEDULE[$name]}}
    fi
  done
}

install_system_packages() {
  local packages=("$@")
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y "${packages[@]}"
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y "${packages[@]}"
  elif command -v yum >/dev/null 2>&1; then
    sudo yum install -y "${packages[@]}"
  elif command -v zypper >/dev/null 2>&1; then
    sudo zypper --non-interactive install "${packages[@]}"
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -Sy --needed --noconfirm "${packages[@]}"
  else
    die "Install these backup dependencies and rerun: ${packages[*]}"
  fi
}

configure_backup() {
  local remote remote_name tmp missing=()
  [[ "${SELECTED_ENABLED[backup]}" == yes ]] || return 0
  command -v jq >/dev/null 2>&1 || missing+=(jq)
  command -v rclone >/dev/null 2>&1 || missing+=(rclone)
  if (( ${#missing[@]} )); then
    info "Installing backup dependencies: ${missing[*]}"
    install_system_packages "${missing[@]}"
  fi
  command -v jq >/dev/null 2>&1 || die "jq installation did not provide the jq command."
  command -v rclone >/dev/null 2>&1 || die "rclone installation did not provide the rclone command."
  if sudo test -s /etc/marketmind/backup.conf; then
    ok "Keeping the existing backup destination."
    return
  fi
  heading "Backup destination"
  say "Use an existing rclone remote and path, for example: offsite:marketmind"
  while true; do
    remote=$(prompt_text "rclone destination")
    if [[ "$remote" == ?*:* ]]; then break; fi
    warn "Use an rclone remote and path such as offsite:marketmind."
  done
  remote_name=${remote%%:*}
  if ! rclone listremotes | grep -Fxq "$remote_name:"; then
    warn "The rclone remote '$remote_name' is not configured. Opening rclone setup."
    rclone config
  fi
  rclone listremotes | grep -Fxq "$remote_name:" || \
    die "The rclone remote '$remote_name' is still not configured."
  rclone lsd "$remote_name:" >/dev/null || \
    die "Cannot connect to the rclone remote '$remote_name'."
  tmp=$(mktemp)
  printf 'MARKETMIND_BACKUP_REMOTE=%s\n' "$remote" > "$tmp"
  sudo install -d -m 0700 /etc/marketmind
  sudo install -d -m 0700 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" /srv/marketmind-backups
  sudo install -o root -g root -m 0600 "$tmp" /etc/marketmind/backup.conf
  rm -f "$tmp"
}

next_quiet_time() {
  local today candidate now_epoch candidate_epoch
  today=$(date +%F)
  candidate="$today 22:00:00"
  now_epoch=$(date +%s)
  candidate_epoch=$(date -d "$candidate" +%s)
  if (( candidate_epoch <= now_epoch + 300 )); then
    date -d 'tomorrow 22:00:00' '+%Y-%m-%d %H:%M:%S'
  else
    printf '%s' "$candidate"
  fi
}

configure_backfill() {
  local answer schedule existing_schedule
  BACKFILL_MODE=skip
  BACKFILL_SCHEDULE=
  if [[ -f "$INSTALL_DIR/state/refresh/initial-backfill.complete" ]]; then
    ok "Initial historical backfill is already complete."
    return
  fi
  heading "Initial historical backfill"
  say "This imports filing history in the background and can take several hours."
  say "It is resumable and writes progress to the system journal."
  existing_schedule=
  if sudo test -r "$SYSTEMD_DIR/marketmind-initial-backfill.timer"; then
    existing_schedule=$(sudo awk -F= \
      '$1 == "OnCalendar" {value=$2} END {print value}' \
      "$SYSTEMD_DIR/marketmind-initial-backfill.timer")
  fi
  while true; do
    read -r -p "Start it [scheduled/now/custom/skip] [scheduled]: " answer
    answer=${answer:-scheduled}
    case "${answer,,}" in
      s|scheduled)
        BACKFILL_MODE=scheduled
        BACKFILL_SCHEDULE=${existing_schedule:-$(next_quiet_time)}
        info "Backfill will start at $BACKFILL_SCHEDULE."
        return
        ;;
      n|now)
        BACKFILL_MODE=now
        return
        ;;
      c|custom)
        while true; do
          schedule=$(prompt_text "One-time systemd calendar expression" "$(next_quiet_time)")
          if validate_calendar "$schedule"; then break; fi
          warn "systemd does not recognize that calendar expression."
        done
        BACKFILL_MODE=scheduled
        BACKFILL_SCHEDULE=$schedule
        return
        ;;
      skip|none)
        return
        ;;
      *) warn "Choose scheduled, now, custom, or skip." ;;
    esac
  done
}

prepare_shared_postgres() {
  local postgres_env="$POSTGRES_INSTALL_DIR/.env"
  sudo install -d -m 0755 -o "$DEPLOY_USER" -g "$DEPLOY_GROUP" "$POSTGRES_INSTALL_DIR"
  install -d -m 0700 "$POSTGRES_INSTALL_DIR/secrets"
  if [[ ! -f "$POSTGRES_INSTALL_DIR/docker-compose.yml" ]]; then
    install -m 0644 "$INSTALL_DIR/docker-compose.postgres.yml" \
      "$POSTGRES_INSTALL_DIR/docker-compose.yml"
  fi
  if [[ ! -f "$postgres_env" ]]; then
    (umask 077; : > "$postgres_env")
  fi
  chmod 0600 "$postgres_env"
  if [[ -z "$(config_value "$postgres_env" POSTGRES_IMAGE)" ]]; then
    set_config_value "$postgres_env" POSTGRES_IMAGE \
      "$(release_value MARKETMIND_POSTGRES_IMAGE)"
  fi
  if [[ -z "$(config_value "$postgres_env" POSTGRES_NETWORK)" ]]; then
    set_config_value "$postgres_env" POSTGRES_NETWORK "$database_network"
  fi
  if [[ -z "$(config_value "$postgres_env" POSTGRES_DATA_VOLUME)" ]]; then
    set_config_value "$postgres_env" POSTGRES_DATA_VOLUME postgres-shared-data
  fi

  if [[ ! -s "$POSTGRES_INSTALL_DIR/secrets/postgres_admin_password" ]]; then
    if [[ -s "$INSTALL_DIR/secrets/postgres_admin_password" ]]; then
      mv "$INSTALL_DIR/secrets/postgres_admin_password" \
        "$POSTGRES_INSTALL_DIR/secrets/postgres_admin_password"
    else
      write_secret "$POSTGRES_INSTALL_DIR/secrets/postgres_admin_password" \
        "$(openssl rand -hex 32)" 0644
    fi
  fi
  chmod 0644 "$POSTGRES_INSTALL_DIR/secrets/postgres_admin_password"
}

start_shared_postgres() {
  local postgres_compose=(
    docker compose --project-directory "$POSTGRES_INSTALL_DIR"
    -f "$POSTGRES_INSTALL_DIR/docker-compose.yml"
  )
  "${postgres_compose[@]}" config --quiet
  "${postgres_compose[@]}" pull
  "${postgres_compose[@]}" up -d --wait
  "$INSTALL_DIR/deploy/provision-marketmind-db.sh" \
    "$POSTGRES_INSTALL_DIR" "$INSTALL_DIR/secrets/marketmind_database_password"
}

render_service() {
  local source=$1 target=$2 escaped_dir
  escaped_dir=${INSTALL_DIR//|/\\|}
  sed "s|/srv/marketmind|$escaped_dir|g" "$source" | awk \
    -v user="$DEPLOY_USER" -v group="$DEPLOY_GROUP" '
      $0 == "[Service]" { print; print "User=" user; print "Group=" group; next }
      { print }
    ' > "$target"
}

render_timer() {
  local name=$1 schedule=$2 target=$3
  sed "s|^OnCalendar=.*|OnCalendar=$schedule|" \
    "$INSTALL_DIR/deploy/systemd/marketmind-$name.timer" > "$target"
}

install_systemd() {
  local staging name unit
  staging=$(mktemp -d)
  trap 'rm -rf "$staging"' RETURN
  for name in "${TIMER_NAMES[@]}"; do
    render_service "$INSTALL_DIR/deploy/systemd/marketmind-$name.service" \
      "$staging/marketmind-$name.service"
    render_timer "$name" "${SELECTED_SCHEDULE[$name]}" "$staging/marketmind-$name.timer"
  done
  render_service "$INSTALL_DIR/deploy/systemd/marketmind-initial-backfill.service" \
    "$staging/marketmind-initial-backfill.service"

  if [[ "$BACKFILL_MODE" == scheduled ]]; then
    cat > "$staging/marketmind-initial-backfill.timer" <<EOF
[Unit]
Description=marketmind one-time initial historical backfill

[Timer]
OnCalendar=$BACKFILL_SCHEDULE
Persistent=true
Unit=marketmind-initial-backfill.service

[Install]
WantedBy=timers.target
EOF
  fi

  sudo install -o root -g root -m 0644 "$staging"/*.service "$SYSTEMD_DIR/"
  for name in "${TIMER_NAMES[@]}"; do
    sudo install -o root -g root -m 0644 "$staging/marketmind-$name.timer" "$SYSTEMD_DIR/"
  done
  sudo install -o root -g root -m 0644 "$INSTALL_DIR/deploy/logrotate" \
    /etc/logrotate.d/marketmind
  if [[ "$BACKFILL_MODE" == scheduled ]]; then
    sudo install -o root -g root -m 0644 "$staging/marketmind-initial-backfill.timer" \
      "$SYSTEMD_DIR/"
  fi
  sudo systemctl daemon-reload

  for name in "${TIMER_NAMES[@]}"; do
    unit="marketmind-$name.timer"
    if [[ "${SELECTED_ENABLED[$name]}" == yes ]]; then
      sudo systemctl enable --now "$unit"
    else
      sudo systemctl disable --now "$unit" 2>/dev/null || true
    fi
  done

  case "$BACKFILL_MODE" in
    scheduled) sudo systemctl enable --now marketmind-initial-backfill.timer ;;
    now) sudo systemctl disable --now marketmind-initial-backfill.timer 2>/dev/null || true
         sudo systemctl start --no-block marketmind-initial-backfill.service ;;
    skip) sudo systemctl disable --now marketmind-initial-backfill.timer 2>/dev/null || true ;;
  esac
  rm -rf "$staging"
  trap - RETURN
}

main() {
  [[ $(id -u) -ne 0 ]] || die "Run this as your normal deployment account, not with sudo."
  [[ -t 0 ]] || die "This installer needs an interactive terminal."
  [[ "$INSTALL_DIR" != *[[:space:]]* ]] || die "The installation path cannot contain spaces."
  for command in awk date docker flock install openssl sed sudo systemctl systemd-analyze; do
    require_command "$command"
  done
  [[ -f "$INSTALL_DIR/release.env" ]] || die "release.env is missing; use the published bundle."
  [[ -f "$INSTALL_DIR/.env.example" ]] || die ".env.example is missing."
  [[ -f "$INSTALL_DIR/docker-compose.prod.yml" ]] || die "docker-compose.prod.yml is missing."
  [[ -f "$INSTALL_DIR/docker-compose.postgres.yml" ]] || \
    die "docker-compose.postgres.yml is missing."
  docker compose version >/dev/null 2>&1 || die "The Docker Compose plugin is not available."
  docker info >/dev/null 2>&1 || die \
    "Cannot access the Docker daemon as $DEPLOY_USER. Make 'docker info' work without sudo, then rerun."

  heading "Marketmind homelab installer"
  say "Install directory: $INSTALL_DIR"
  say "Deployment account: $DEPLOY_USER ($(id -u):$(id -g))"
  sudo -v

  heading "Local configuration"
  cd "$INSTALL_DIR"
  install -d -m 0700 data/raw state state/universe state/processed state/refresh secrets
  sudo install -d -m 0700 -o root -g root /var/log/marketmind
  if [[ ! -f .env ]]; then cp .env.example .env; fi
  chmod 0600 .env
  set_env MARKETMIND_IMAGE "$(release_value MARKETMIND_IMAGE)"
  set_env MARKETMIND_UID "$(id -u)"
  set_env MARKETMIND_GID "$(id -g)"

  contact_name=$(env_value SEC_CONTACT_NAME)
  if [[ -z "$contact_name" || "$contact_name" == "Your Name" ]]; then
    contact_name=$(prompt_text "SEC contact name")
    [[ -n "$contact_name" ]] || die "SEC contact name is required."
    set_env SEC_CONTACT_NAME "\"${contact_name//\"/\\\"}\""
  fi
  contact_email=$(env_value SEC_CONTACT_EMAIL)
  if [[ -z "$contact_email" || "$contact_email" == "you@example.com" ]]; then
    contact_email=$(prompt_text "SEC contact email")
    [[ "$contact_email" == *@* ]] || die "Enter a valid SEC contact email."
    set_env SEC_CONTACT_EMAIL "$contact_email"
  fi

  database_network=$(env_value MARKETMIND_DATABASE_NETWORK)
  database_network=${database_network:-postgres-network}
  shared_local=yes
  if [[ -f state/deployment-mode && $(< state/deployment-mode) == existing ]]; then
    shared_local=no
  fi
  if prompt_yes_no "Create or use the shared PostgreSQL service in /srv/postgres?" \
    "$shared_local"; then
    shared_local=yes
    printf 'shared-local\n' > state/deployment-mode
    if [[ ! -s secrets/marketmind_database_password ]]; then
      write_secret secrets/marketmind_database_password "$(openssl rand -hex 32)"
    fi
    chmod 0600 secrets/marketmind_database_password
    db_password=$(< secrets/marketmind_database_password)
    write_secret secrets/app_pgpass \
      "postgres-shared:5432:marketmind:marketmind:$db_password"
    unset db_password
    prepare_shared_postgres
  else
    shared_local=no
    printf 'existing\n' > state/deployment-mode
    [[ -s secrets/app_pgpass ]] || die \
      "Create secrets/app_pgpass for the existing PostgreSQL instance, then rerun the installer."
  fi
  if [[ ! -s secrets/mcp_token ]]; then
    write_secret secrets/mcp_token "$(openssl rand -hex 32)"
  fi
  ensure_secret llm_api_key "Model provider API key"
  ensure_secret fred_api_key "FRED API key"
  docker network inspect "$database_network" >/dev/null 2>&1 || \
    docker network create "$database_network" >/dev/null
  ok "Configuration and secrets are ready."

  configure_timers
  configure_backup
  configure_backfill

  heading "Starting containers"
  if ! docker pull "$(release_value MARKETMIND_IMAGE)"; then
    warn "The package requires registry authentication."
    while [[ -z "${gh_user:-}" ]]; do gh_user=$(prompt_text "GitHub username"); done
    gh_token=$(prompt_secret "GitHub token with read:packages")
    printf '%s' "$gh_token" | docker login ghcr.io -u "$gh_user" --password-stdin
    unset gh_token
  fi
  if [[ "$shared_local" == yes ]]; then
    start_shared_postgres
  fi
  docker compose -f docker-compose.prod.yml config --quiet
  docker compose -f docker-compose.prod.yml pull
  docker compose -f docker-compose.prod.yml up -d
  ok "Containers started."

  heading "Installing schedules"
  install_systemd
  ok "Systemd configuration installed."

  heading "Done"
  docker compose -f docker-compose.prod.yml ps
  if [[ "$shared_local" == yes ]]; then
    docker compose --project-directory "$POSTGRES_INSTALL_DIR" \
      -f "$POSTGRES_INSTALL_DIR/docker-compose.yml" ps
  fi
  say
  systemctl list-timers 'marketmind-*' --no-pager || true
  say
  info "MCP endpoint: http://127.0.0.1:8000/mcp"
  info "Follow service logs: docker compose -f docker-compose.prod.yml logs -f marketmind"
  if [[ "$BACKFILL_MODE" != skip ]]; then
    info "Follow backfill: journalctl -fu marketmind-initial-backfill.service"
  fi
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
