#!/bin/sh
set -eu

install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
backup_dir="${MARKETMIND_BACKUP_DIR:-/srv/marketmind-backups}"
keep_days="${MARKETMIND_BACKUP_KEEP_DAYS:-14}"
umask 077
mkdir -p "$backup_dir"
find "$backup_dir" -name "marketmind-*.tmp" -mtime +1 -delete
trap 'exit 1' HUP INT TERM

stamp="$(date +%F-%H%M)"
backup_path="$backup_dir/marketmind-$stamp.tar.gz"
tar -czf "$backup_path.tmp" -C "$install_dir" data/raw state
tar -tzf "$backup_path.tmp" >/dev/null
mv "$backup_path.tmp" "$backup_path"
find "$backup_dir" -name "marketmind-*.tar.gz" -mtime "+$keep_days" -delete

trap - EXIT HUP INT TERM
