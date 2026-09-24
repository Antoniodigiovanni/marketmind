#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH=$(readlink -f -- "${BASH_SOURCE[0]}")
INSTALL_DIR="${MARKETMIND_INSTALL_DIR:-$(dirname "$(dirname "$SCRIPT_PATH")")}"

usage() {
  cat <<'EOF'
Usage:
  marketmind update [vX.Y.Z]
  marketmind dashboard enable|disable|status
  marketmind dashboard tailscale enable [https-port]|disable|status
  marketmind version
  marketmind help

With no version, `marketmind update` installs the most recently published release.
EOF
}

case "${1:-help}" in
  update)
    exec "$INSTALL_DIR/deploy/update.sh" "${2:-latest}"
    ;;
  dashboard)
    shift
    exec "$INSTALL_DIR/deploy/dashboard.sh" "$@"
    ;;
  version)
    if [[ -s "$INSTALL_DIR/state/current-release" ]]; then
      cat "$INSTALL_DIR/state/current-release"
    else
      printf 'unknown\n'
    fi
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    printf 'Error: unknown command: %s\n\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac
