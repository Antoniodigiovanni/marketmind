#!/usr/bin/env bash
set -euo pipefail

# Install a verified release archive from any download directory. This script
# is published alongside the archive; the application never runs as root.
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
[[ $# -eq 1 ]] || die 'usage: marketmind-vX.Y.Z-install.sh /path/to/marketmind-vX.Y.Z-native.tar.gz'
[[ $(id -u) -ne 0 ]] || die 'run as the deployment account, not root'
[[ -t 0 ]] || die 'an interactive terminal is required'
for cmd in awk basename cp dirname id install mktemp python3 realpath rm sha256sum sudo tar wc; do
  command -v "$cmd" >/dev/null 2>&1 || die "required command not found: $cmd"
done
archive=$(realpath -- "$1")
[[ -f "$archive" && -f "$archive.sha256" ]] || die 'archive and its .sha256 file are required'
name=$(basename -- "$archive")
[[ "$name" =~ ^marketmind-(v[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9][A-Za-z0-9.-]*)?)-native\.tar\.gz$ ]] || \
  die 'invalid archive filename'
tag=${BASH_REMATCH[1]}
[[ $(wc -l < "$archive.sha256") -eq 1 ]] || die 'checksum must contain exactly one line'
[[ $(awk '{print $2}' "$archive.sha256") == "$name" ]] || die 'checksum filename mismatch'
(cd "$(dirname -- "$archive")" && sha256sum --check -- "$name.sha256") || \
  die 'archive checksum verification failed'

# Check members before extracting: reject traversal, links, special files, and
# mutable runtime paths even when a checksum accompanies a malformed archive.
python3 - "$archive" <<'PY'
import sys
import tarfile
from pathlib import PurePosixPath

allowed = {"agent", "data", "server", "dashboard", "deploy"}
files = {".env.example", "pyproject.toml", "uv.lock", "release.env"}
with tarfile.open(sys.argv[1], "r:gz") as archive:
    seen = set()
    for member in archive:
        path = PurePosixPath(member.name)
        parts = path.parts
        if (
            path.is_absolute()
            or ".." in parts
            or not parts
            or parts[0] != "marketmind"
            or member.name in seen
            or not (member.isfile() or member.isdir())
            or (len(parts) > 1 and parts[1] not in allowed | files)
            or (len(parts) > 2 and parts[1] == "data" and parts[2] == "raw")
            or (len(parts) > 2 and parts[1] in files)
        ):
            raise SystemExit(f"unsafe archive member: {member.name}")
        seen.add(member.name)
PY
staging=$(mktemp -d)
trap 'rm -rf -- "$staging"' EXIT
tar -xzf "$archive" -C "$staging" --no-same-owner --no-same-permissions
payload="$staging/marketmind"
[[ -f "$payload/deploy/install.sh" && -f "$payload/deploy/update.sh" && \
   -f "$payload/pyproject.toml" && -f "$payload/uv.lock" && \
   -f "$payload/release.env" ]] || die 'release archive is incomplete'
[[ $(awk -F= '$1 == "RELEASE_TAG" {print $2; exit}' "$payload/release.env") == "$tag" ]] || \
  die 'archive filename and release tag do not match'

install_dir=${MARKETMIND_INSTALL_DIR:-/srv/marketmind}
[[ "$install_dir" == /* && "$install_dir" != *[[:space:]]* && ! -L "$install_dir" ]] || \
  die 'install directory must be an absolute non-symlink path without whitespace'
[[ ! -L "$install_dir/releases/current" ]] || \
  die 'Marketmind is already installed; use marketmind update instead'
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" "$install_dir"
for directory in agent data server dashboard deploy; do
  [[ ! -L "$install_dir/$directory" ]] || die "refusing symlinked $directory directory"
done
cp -a "$payload/." "$install_dir/"
if [[ "$archive" != "$install_dir/$name" ]]; then
  install -m 0644 "$archive" "$install_dir/$name"
  install -m 0644 "$archive.sha256" "$install_dir/$name.sha256"
fi
"$install_dir/deploy/install.sh"
