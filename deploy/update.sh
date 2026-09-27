#!/usr/bin/env bash
set -euo pipefail

source_arg=${1:-latest}
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
install_dir=${MARKETMIND_INSTALL_DIR:-$(dirname "$script_dir")}
repo=${MARKETMIND_RELEASE_REPOSITORY:-Antoniodigiovanni/marketmind}
staging= previous= switched= new_dir= service_installed= override_installed=
service=marketmind-mcp.service

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
value() { awk -F= -v key="$2" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$1"; }
valid_tag() { [[ "$1" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?$ ]]; }

rollback() {
  local status=$?
  if (( status != 0 )) && [[ -n "$switched" ]]; then
    printf 'Update failed; restoring previous release.\n' >&2
    if [[ -n "$previous" ]]; then
      ln -sfn "$previous" "$install_dir/releases/.rollback-link"
      mv -Tf "$install_dir/releases/.rollback-link" "$install_dir/releases/current"
      sudo systemctl restart "$service" || true
    else
      sudo systemctl stop "$service" || true
      rm -f "$install_dir/releases/current"
      if [[ -n "$service_installed" ]]; then
        sudo systemctl disable "$service" || true
        sudo rm -f "/etc/systemd/system/$service"
        sudo systemctl daemon-reload || true
      fi
    fi
  fi
  if (( status != 0 )) && [[ -n "$override_installed" ]]; then
    sudo rm -f "/etc/systemd/system/$service.d/native-release.conf"
    sudo systemctl daemon-reload || true
    if [[ -z "$service_installed" ]]; then sudo systemctl restart "$service" || true; fi
  fi
  if (( status != 0 )) && [[ -n "$new_dir" ]]; then rm -rf "$new_dir"; fi
  if [[ -n "$staging" ]]; then rm -rf "$staging"; fi
  exit "$status"
}
trap rollback EXIT

for cmd in awk curl flock install ln mktemp mv python3 sha256sum sudo systemctl tar uv; do
  command -v "$cmd" >/dev/null || die "required command not found: $cmd"
done
[[ $(id -u) -ne 0 ]] || die 'Run as the deployment account, not root.'
[[ -f "$install_dir/.env" ]] || die 'missing runtime .env; configure the native host first'
[[ -d "$install_dir/data/raw" ]] || die 'missing shared data/raw directory'
[[ "$install_dir" != *[[:space:]]* ]] || die 'installation path cannot contain spaces'
install -d -m 0700 "$install_dir/state/refresh"
install -d -m 0755 "$install_dir/releases"
exec 9> "$install_dir/state/refresh/deployment.lock"
flock -n 9 || die 'another update is running'
staging=$(mktemp -d)

if [[ "$source_arg" == latest || "$source_arg" == v* ]]; then
  [[ "$repo" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die 'invalid release repository'
  if [[ "$source_arg" == latest ]]; then
    tag=$(curl -fsSL --retry 3 "https://api.github.com/repos/$repo/releases/latest" | \
      python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
  else
    tag=$source_arg
  fi
  valid_tag "$tag" || die "invalid release tag: $tag"
  base="https://github.com/$repo/releases/download/$tag/marketmind-$tag-native.tar.gz"
  archive="$staging/marketmind-$tag-native.tar.gz"
  curl -fL --retry 3 -o "$archive" "$base"
  curl -fL --retry 3 -o "$archive.sha256" "$base.sha256"
else
  archive=$source_arg
  [[ -f "$archive" && -f "$archive.sha256" ]] || die 'archive and matching .sha256 required'
  cp -p -- "$archive" "$archive.sha256" "$staging/"
  archive="$staging/$(basename -- "$archive")"
fi

name=$(basename -- "$archive")
[[ "$name" =~ ^marketmind-(v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?)-native\.tar\.gz$ ]] || die 'invalid archive name'
tag=${BASH_REMATCH[1]}
[[ $(wc -l < "$archive.sha256") -eq 1 ]] || die 'checksum must contain one line'
[[ $(awk '{print $2}' "$archive.sha256") == "$name" ]] || die 'checksum filename mismatch'
(cd "$staging" && sha256sum -c -- "$name.sha256") || die 'checksum verification failed'

# Never extract links, devices, hardlinks, duplicates, or paths outside the release root.
python3 - "$archive" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

with tarfile.open(sys.argv[1], 'r:gz') as archive:
    seen = set()
    for member in archive:
        path = PurePosixPath(member.name)
        if (path.is_absolute() or '..' in path.parts or not path.parts
                or path.parts[0] != 'marketmind' or member.name in seen
                or not (member.isfile() or member.isdir())):
            raise SystemExit(f'unsafe archive member: {member.name}')
        seen.add(member.name)
PY
mkdir "$staging/unpacked"
tar -xzf "$archive" -C "$staging/unpacked" --no-same-owner --no-same-permissions
payload="$staging/unpacked/marketmind"
[[ -f "$payload/pyproject.toml" && -f "$payload/uv.lock" && \
   -f "$payload/deploy/native-run.sh" && -f "$payload/deploy/install.sh" && \
   -f "$payload/release.env" ]] || die 'incomplete archive'
[[ $(value "$payload/release.env" RELEASE_TAG) == "$tag" ]] || die 'release tag mismatch'
[[ ! -e "$install_dir/releases/$tag" ]] || die "release $tag is already staged"

new_dir="$install_dir/releases/$tag"
install -d -m 0755 "$new_dir"
cp -a "$payload/." "$new_dir/"
ln -s "$install_dir/data/raw" "$new_dir/data/raw"
(cd "$new_dir" && uv sync --frozen --no-dev --python 3.12)
[[ -x "$new_dir/.venv/bin/python" ]] || die 'dependency installation failed'

# The native unit uses the existing runtime configuration and secrets.
unit="$staging/marketmind.service"
cat > "$unit" <<EOF
[Unit]
Description=Marketmind MCP server
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$(id -un)
Group=$(id -gn)
EnvironmentFile=$install_dir/.env
Environment=MARKETMIND_INSTALL_DIR=$install_dir
ExecStart=$install_dir/releases/current/deploy/native-run.sh server
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$install_dir/state $install_dir/data/raw
UMask=0077

[Install]
WantedBy=multi-user.target
EOF
# Keep runtime configuration and secrets untouched.
if [[ ! -f "/etc/systemd/system/$service" ]]; then
  sudo install -m 0644 "$unit" "/etc/systemd/system/$service"
  service_installed=1
else
  override="/etc/systemd/system/$service.d"
  sudo install -d -m 0755 "$override"
  if [[ -f "$override/native-release.conf" ]]; then
    grep -qF "ExecStart=$install_dir/releases/current/deploy/native-run.sh server" \
      "$override/native-release.conf" || die 'incompatible native service override'
  else
    printf '[Service]\nUser=%s\nGroup=%s\nEnvironmentFile=%s/.env\nEnvironment=MARKETMIND_INSTALL_DIR=%s\nWorkingDirectory=%s/releases/current\nExecStart=\nExecStart=%s/releases/current/deploy/native-run.sh server\n' \
      "$(id -un)" "$(id -gn)" "$install_dir" "$install_dir" "$install_dir" "$install_dir" | \
      sudo tee "$override/native-release.conf" >/dev/null
    override_installed=1
  fi
fi
sudo systemctl daemon-reload
if [[ -L "$install_dir/releases/current" ]]; then
  previous=$(readlink "$install_dir/releases/current")
fi
ln -s "$tag" "$install_dir/releases/.next-link"
mv -Tf "$install_dir/releases/.next-link" "$install_dir/releases/current"
switched=1
sudo systemctl enable "$service"
sudo systemctl restart "$service"

port=$(awk -F= '$1 == "MARKETMIND_MCP_PORT" {print $2; exit}' "$install_dir/.env")
port=${port:-8000}
[[ "$port" =~ ^[0-9]+$ ]] || die 'invalid MCP port'
ready=
for ((attempt=1; attempt<=30; attempt++)); do
  status=$(curl -sS --max-time 2 -o /dev/null -w '%{http_code}' \
    "http://127.0.0.1:$port/mcp" 2>/dev/null || true)
  if [[ "$status" == 401 ]]; then ready=1; break; fi
  sleep 2
done
[[ -n "$ready" ]] || die 'MCP health check failed'

# Override only the execution command; leave timer schedules and enablement unchanged.
for job in daily universe profiles financials esef initial-backfill; do
  [[ -f "/etc/systemd/system/marketmind-$job.service" ]] || continue
  override="/etc/systemd/system/marketmind-$job.service.d"
  sudo install -d -m 0755 "$override"
  job_arg=$job
  [[ "$job" == initial-backfill ]] && job_arg=backfill
  printf '[Service]\nEnvironmentFile=%s/.env\nEnvironment=MARKETMIND_INSTALL_DIR=%s\nExecStart=\nExecStart=%s/deploy/native-run.sh %s\n' \
    "$install_dir" "$install_dir" "$install_dir/releases/current" "$job_arg" | \
    sudo tee "$override/native.conf" >/dev/null
done
sudo systemctl daemon-reload
printf '%s\n' "$tag" > "$install_dir/state/current-release"
# Refresh host scripts after readiness; never overwrite runtime configuration.
for script in install.sh update.sh dashboard.sh run-job.sh native-run.sh \
  provision-marketmind-db.sh provision-dashboard-db.sh backup-set.sh restore-check.sh \
  marketmind.sh; do
  install -m 0755 "$new_dir/deploy/$script" "$install_dir/deploy/$script.next"
  mv -f "$install_dir/deploy/$script.next" "$install_dir/deploy/$script"
done
switched=
printf 'Updated Marketmind to %s (native service).\n' "$tag"
