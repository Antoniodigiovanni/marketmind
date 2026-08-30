#!/bin/sh
set -eu

# Market data, generated views, and local database files are private even when
# the host or container runtime has a more permissive default.
umask 077

if [ -n "${MARKETMIND_UNIVERSE_DIR:-}" ]; then
    mkdir -p "$MARKETMIND_UNIVERSE_DIR"
    for universe_file in sp500.csv eurostoxx600.csv delisted.csv; do
        source_file="/app/data/universe/$universe_file"
        target_file="$MARKETMIND_UNIVERSE_DIR/$universe_file"
        if [ ! -e "$target_file" ]; then
            cp "$source_file" "$target_file"
        fi
    done
fi

exec "$@"
