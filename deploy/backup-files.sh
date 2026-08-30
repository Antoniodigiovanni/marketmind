#!/bin/sh
set -eu

install_dir="${MARKETMIND_INSTALL_DIR:-/srv/marketmind}"
backup_dir="${MARKETMIND_BACKUP_DIR:-/srv/marketmind-backups}"
keep_days="${MARKETMIND_BACKUP_KEEP_DAYS:-14}"
compose_file="$install_dir/docker-compose.prod.yml"
was_running=""

umask 077
mkdir -p "$backup_dir"
find "$backup_dir" -name "marketmind-*.tmp" -mtime +1 -delete

if [ -n "$(docker compose -f "$compose_file" ps --status running -q marketmind)" ]; then
    was_running=1
    docker compose -f "$compose_file" stop marketmind
fi

restart_service() {
    if [ -n "$was_running" ]; then
        docker compose -f "$compose_file" start marketmind
    fi
}
trap restart_service EXIT
trap 'exit 1' HUP INT TERM

stamp="$(date +%F-%H%M)"
backup_path="$backup_dir/marketmind-$stamp.tar.gz"
tar -czf "$backup_path.tmp" -C "$install_dir" data/raw state
tar -tzf "$backup_path.tmp" >/dev/null
mv "$backup_path.tmp" "$backup_path"
find "$backup_dir" -name "marketmind-*.tar.gz" -mtime "+$keep_days" -delete

trap - EXIT HUP INT TERM
restart_service
