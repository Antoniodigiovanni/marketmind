#!/usr/bin/env bash
set -euo pipefail

release_source=${1:-}
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${MARKETMIND_INSTALL_DIR:-$(dirname "$SCRIPT_DIR")}"
staging=
temporary_container=

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
ok() { printf '✓ %s\n' "$*"; }
info() { printf '› %s\n' "$*"; }

cleanup() {
  if [[ -n "$temporary_container" ]]; then
    docker rm -f "$temporary_container" >/dev/null 2>&1 || true
  fi
  if [[ -n "$staging" ]]; then rm -rf "$staging"; fi
}

release_value() {
  local file=$1 key=$2
  awk -F= -v key="$key" '$1 == key {sub(/^[^=]*=/, ""); print; exit}' "$file"
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

stage_archive() {
  local archive=$1 archive_dir archive_name checksum checksum_target entry normalized
  archive_dir=$(cd -- "$(dirname -- "$archive")" && pwd)
  archive_name=$(basename -- "$archive")
  archive="$archive_dir/$archive_name"
  checksum="$archive.sha256"
  [[ -f "$archive" ]] || die "release archive not found: $archive"
  [[ -f "$checksum" ]] || die "release checksum not found: $checksum"

  checksum_target=$(awk 'NR == 1 {name=$2; sub(/^\*/, "", name); print name}' "$checksum")
  [[ "$checksum_target" == "$archive_name" ]] || \
    die "checksum file does not describe $archive_name"
  [[ $(awk 'END {print NR}' "$checksum") -eq 1 ]] || \
    die "checksum file must contain one entry"
  (cd "$archive_dir" && sha256sum --check "$(basename -- "$checksum")")

  mapfile -t archive_entries < <(tar -tzf "$archive")
  for entry in "${archive_entries[@]}"; do
    normalized=${entry#./}
    case "$entry" in /*) die "unsafe path in release archive: $entry" ;; esac
    case "/$normalized/" in
      *'/../'*) die "unsafe path in release archive: $entry" ;;
    esac
  done
  tar -xzf "$archive" -C "$staging"

  release_tag=$(release_value "$staging/release.env" RELEASE_TAG)
  [[ "$archive_name" == "marketmind-$release_tag-deploy.tar.gz" ]] || \
    die "archive name does not match RELEASE_TAG=$release_tag"
}

stage_image() {
  local selector=$1 current_image image_repository tagged_image resolved_image postgres_image
  local image_release_tag
  if [[ "$selector" != latest ]]; then
    [[ "$selector" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?$ ]] || \
      die "invalid release tag: $selector"
  fi
  [[ -f "$INSTALL_DIR/release.env" ]] || \
    die "$INSTALL_DIR/release.env is missing; use a release archive for the first install"

  current_image=$(release_value "$INSTALL_DIR/release.env" MARKETMIND_IMAGE)
  postgres_image=$(release_value "$INSTALL_DIR/release.env" MARKETMIND_POSTGRES_IMAGE)
  [[ "$current_image" == *@sha256:* ]] || \
    die "current MARKETMIND_IMAGE is not pinned by digest"
  [[ -n "$postgres_image" ]] || die "current release.env has no MARKETMIND_POSTGRES_IMAGE"
  image_repository=${current_image%@sha256:*}
  tagged_image="$image_repository:$selector"

  info "Pulling $tagged_image"
  docker pull "$tagged_image" || \
    die "image pull failed; authenticate with docker login ghcr.io and rerun"
  resolved_image=$(docker image inspect \
    --format '{{range .RepoDigests}}{{println .}}{{end}}' "$tagged_image" | \
    awk -v repository="$image_repository" 'index($0, repository "@sha256:") == 1 {print; exit}')
  [[ "$resolved_image" == "$image_repository@sha256:"* ]] || \
    die "could not resolve $tagged_image to a repository digest"
  image_release_tag=$(docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.version"}}' "$tagged_image")
  [[ "$image_release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?$ ]] || \
    die "$tagged_image has no valid release-version label"
  if [[ "$selector" != latest && "$selector" != "$image_release_tag" ]]; then
    die "$tagged_image identifies itself as $image_release_tag"
  fi

  temporary_container=$(docker create --entrypoint /bin/true "$resolved_image")
  docker cp "$temporary_container:/app/.env.example" "$staging/.env.example"
  docker cp "$temporary_container:/app/docker-compose.prod.yml" \
    "$staging/docker-compose.prod.yml"
  docker cp "$temporary_container:/app/docker-compose.postgres.yml" \
    "$staging/docker-compose.postgres.yml"
  docker cp "$temporary_container:/app/deploy" "$staging/deploy"
  docker rm "$temporary_container" >/dev/null
  temporary_container=
  printf 'MARKETMIND_IMAGE=%s\nMARKETMIND_POSTGRES_IMAGE=%s\nRELEASE_TAG=%s\n' \
    "$resolved_image" "$postgres_image" "$image_release_tag" > "$staging/release.env"
  ok "Prepared $image_release_tag from its digest-pinned container image"
}

[[ $(id -u) -ne 0 ]] || die "Run this as the deployment account, not with sudo."
[[ -n "$release_source" ]] || \
  die "usage: update.sh latest | vX.Y.Z | marketmind-vX.Y.Z-deploy.tar.gz"
for command in awk cp curl date docker flock install mktemp sha256sum sudo tar; do
  command -v "$command" >/dev/null 2>&1 || die "required command not found: $command"
done
[[ -f "$INSTALL_DIR/.env" ]] || die "$INSTALL_DIR/.env does not exist; run install.sh first"

staging=$(mktemp -d)
trap cleanup EXIT
case "$release_source" in
  latest|v*) stage_image "$release_source" ;;
  *) stage_archive "$release_source" ;;
esac
for required in release.env .env.example docker-compose.prod.yml docker-compose.postgres.yml \
  deploy/install.sh deploy/dashboard.sh deploy/update.sh deploy/marketmind.sh; do
  [[ -f "$staging/$required" ]] || die "release archive is missing $required"
done

release_tag=$(release_value "$staging/release.env" RELEASE_TAG)
release_image=$(release_value "$staging/release.env" MARKETMIND_IMAGE)
[[ "$release_tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?$ ]] || \
  die "release.env has an invalid RELEASE_TAG"
[[ "$release_image" == *@sha256:* ]] || \
  die "release.env MARKETMIND_IMAGE is not pinned by digest"

install -d -m 0700 "$INSTALL_DIR/state/refresh" "$INSTALL_DIR/state/deployment-releases"
exec 9> "$INSTALL_DIR/state/refresh/deployment.lock"
flock -n 9 || die "another deployment update is running"

current_tag=$(release_value "$INSTALL_DIR/release.env" RELEASE_TAG 2>/dev/null || true)
current_tag=${current_tag:-unknown}
backup_dir="$INSTALL_DIR/state/deployment-releases/${current_tag}-$(date +%Y%m%dT%H%M%S)"
install -d -m 0700 "$backup_dir"
for path in release.env .env.example docker-compose.prod.yml docker-compose.postgres.yml deploy; do
  if [[ -e "$INSTALL_DIR/$path" ]]; then cp -a "$INSTALL_DIR/$path" "$backup_dir/"; fi
done
cp -a "$INSTALL_DIR/.env" "$backup_dir/runtime.env"
ok "Saved previous deployment files under $backup_dir"

install -m 0644 "$staging/release.env" "$INSTALL_DIR/release.env"
install -m 0644 "$staging/.env.example" "$INSTALL_DIR/.env.example"
install -m 0644 "$staging/docker-compose.prod.yml" "$INSTALL_DIR/docker-compose.prod.yml"
install -m 0644 "$staging/docker-compose.postgres.yml" \
  "$INSTALL_DIR/docker-compose.postgres.yml"
cp -a "$staging/deploy/." "$INSTALL_DIR/deploy/"
set_env MARKETMIND_IMAGE "$release_image"

cd "$INSTALL_DIR"
info "Pulling $release_image"
docker pull "$release_image" || \
  die "image pull failed; authenticate with docker login ghcr.io and rerun"
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml pull marketmind
docker compose -f docker-compose.prod.yml up -d --wait --wait-timeout 120 marketmind

mcp_status=
for ((attempt = 1; attempt <= 30; attempt++)); do
  mcp_status=$(curl -sS -o /dev/null -w '%{http_code}' \
    http://127.0.0.1:8000/mcp 2>/dev/null || true)
  if [[ "$mcp_status" == 401 ]]; then break; fi
  sleep 2
done
[[ "$mcp_status" == 401 ]] || die "updated MCP server did not become ready within 60 seconds"

if [[ $(cat state/dashboard-enabled 2>/dev/null || true) == yes ]]; then
  ./deploy/dashboard.sh enable
fi
./deploy/install.sh --refresh-systemd
printf '%s\n' "$release_tag" > state/current-release

ok "Updated Marketmind to $release_tag"
docker compose -f docker-compose.prod.yml ps
info "Application logs: docker compose -f docker-compose.prod.yml logs --tail=100 marketmind"
