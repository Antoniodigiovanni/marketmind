#!/usr/bin/env bash
# marketmind — nightly Postgres backup (run by marketmind-postgres-backup.service).
#
# Runs as root, unlike every other marketmind job: pg_dump needs the Docker
# socket to reach the Postgres container, and the unprivileged marketmind
# app user must never have that access -- Docker socket access is root
# equivalent, and that user processes untrusted network input (RSS/SEC
# feeds). See deploy/README.md's hardening notes.
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/srv/marketmind}"
BACKUP_DIR="${BACKUP_DIR:-/srv/marketmind-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

stamp="$(date +%F-%H%M)"
mkdir -p "$BACKUP_DIR"
docker compose -f "$INSTALL_DIR/docker-compose.prod.yml" --project-directory "$INSTALL_DIR" \
  exec -T postgres pg_dump -U marketmind -Fc marketmind \
  > "$BACKUP_DIR/marketmind-postgres-$stamp.dump"
find "$BACKUP_DIR" -name 'marketmind-postgres-*.dump' -mtime "+$KEEP_DAYS" -delete
