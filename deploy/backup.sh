#!/usr/bin/env bash
# marketmind — nightly data backup (run by marketmind-backup.service).
set -euo pipefail

INSTALL_DIR="${INSTALL_DIR:-/srv/marketmind}"
BACKUP_DIR="${BACKUP_DIR:-/srv/marketmind-backups}"
KEEP_DAYS="${KEEP_DAYS:-14}"

stamp="$(date +%F-%H%M)"
mkdir -p "$BACKUP_DIR"
tar -czf "$BACKUP_DIR/marketmind-$stamp.tar.gz" -C "$INSTALL_DIR" data
find "$BACKUP_DIR" -name 'marketmind-*.tar.gz' -mtime "+$KEEP_DAYS" -delete
