#!/usr/bin/env bash
# marketmind — one-command update for an existing install.
#
# Usage (on the host, as root):
#   sudo bash /srv/marketmind/deploy/update.sh
#
# Reads /srv/marketmind/deploy.conf (BRANCH, REPO_URL, ENABLE_AUTO_UPDATE);
# env vars override the config for a one-off update.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/srv/marketmind}"
APP_USER="${APP_USER:-marketmind}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "error: run as root (sudo bash $INSTALL_DIR/deploy/update.sh)" >&2
  exit 1
fi
if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "error: no install found at $INSTALL_DIR — run install.sh first" >&2
  exit 1
fi

BRANCH_ENV="${BRANCH:-}"
REPO_URL_ENV="${REPO_URL:-}"
ENABLE_AUTO_UPDATE_ENV="${ENABLE_AUTO_UPDATE:-}"

BRANCH="main"
REPO_URL="git@github.com:Antoniodigiovanni/marketmind.git"
ENABLE_AUTO_UPDATE="0"
if [[ -f "$INSTALL_DIR/deploy.conf" ]]; then
  # shellcheck disable=SC1090
  source "$INSTALL_DIR/deploy.conf"
fi
BRANCH="${BRANCH_ENV:-$BRANCH}"
REPO_URL="${REPO_URL_ENV:-$REPO_URL}"
ENABLE_AUTO_UPDATE="${ENABLE_AUTO_UPDATE_ENV:-$ENABLE_AUTO_UPDATE}"

log() { printf '\033[1;34m[marketmind]\033[0m %s\n' "$*"; }

# git runs as root against a repo owned by $APP_USER — declare it safe so the
# ownership guard does not block update runs.
if ! git config --global --get safe.directory "$INSTALL_DIR" >/dev/null 2>&1; then
  git config --global --add safe.directory "$INSTALL_DIR"
fi

log "updating to $BRANCH"
git -C "$INSTALL_DIR" fetch --depth 1 origin "$BRANCH"
git -C "$INSTALL_DIR" checkout --force "$BRANCH"
if git -C "$INSTALL_DIR" rev-parse --verify -q "origin/$BRANCH" >/dev/null 2>&1; then
  git -C "$INSTALL_DIR" reset --hard "origin/$BRANCH"
fi
chown -R "$APP_USER":"$APP_USER" "$INSTALL_DIR"

export PATH="/usr/local/bin:$PATH"
run_as_app() { # run_as_app <command...>
  sudo -u "$APP_USER" env \
    PATH="$PATH" HOME="$INSTALL_DIR" UV_CACHE_DIR="$INSTALL_DIR/.cache" \
    bash -c 'cd "$1" && shift && exec "$@"' _ "$INSTALL_DIR" "$@"
}

log "syncing dependencies"
run_as_app uv sync --frozen

log "initialising database"
run_as_app uv run python -m data.db.init

log "installing systemd units"
install -o root -g root -m 0644 \
  "$INSTALL_DIR"/deploy/systemd/*.service \
  "$INSTALL_DIR"/deploy/systemd/*.timer \
  /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now \
  marketmind-daily.timer \
  marketmind-universe.timer \
  marketmind-backup.timer \
  marketmind-profiles.timer
if [[ "$ENABLE_AUTO_UPDATE" == "1" ]]; then
  log "enabling weekly auto-update timer"
  systemctl enable --now marketmind-update.timer
else
  systemctl disable --now marketmind-update.timer 2>/dev/null || true
fi

log "verifying"
systemctl is-active --quiet marketmind-daily.timer \
  || { echo "error: marketmind-daily.timer is not active" >&2; exit 1; }
short="$(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
log "updated: $BRANCH @ $short"
