# Marketmind on Debian/Ubuntu

Marketmind runs as a Python application under systemd. **Docker is not needed
on the host.** PostgreSQL is a separate host service (or an existing reachable
server); Marketmind owns one database and role, not the PostgreSQL lifecycle.
The dashboard is optional and runs as a separate, read-only system user.

## Requirements

- Debian/Ubuntu, systemd, Bash 4+, `sudo`, `curl`, `tar`, `sha256sum`, `openssl`,
  `flock`, `python3`, and [uv](https://docs.astral.sh/uv/getting-started/installation/).
  The installer can install `postgresql-client` through apt if missing. uv
  installs the pinned Python 3.12 runtime and locked dependencies into a
  versioned release directory, so no system Python replacement is needed.
- PostgreSQL 16+ with pgvector, reachable over TCP (or use the explicit local
  PostgreSQL installation offer). Provide a distinct `marketmind` database and
  role; do not reuse another project's database. `pg_trgm` is also required.
- A non-root deployment account owning `/srv/marketmind` and its `data/raw`,
  `state`, and `secrets` directories. The installer uses `sudo` for systemd and
  optional apt packages. Only the MCP service binds `127.0.0.1:8000`.

PostgreSQL and the application have independent lifecycles. A local cluster
installed by apt is **not** stopped or upgraded by Marketmind updates. The
installer refuses to install a second cluster when PostgreSQL is already
present. The installer selects the apt default PostgreSQL major version (16+)
and requires its matching `postgresql-<major>-pgvector` package. If unavailable,
configure a trusted package repository or attach to an existing pgvector server. Package installation is never automatic.

## First installation

Download both assets for a release (use an authenticated workstation for a
private repository), verify them, then extract the archive:

```bash
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" /srv/marketmind
cd /srv/marketmind
release=vX.Y.Z
base="https://github.com/Antoniodigiovanni/marketmind/releases/download/$release"
curl -fLO "$base/marketmind-$release-native.tar.gz"
curl -fLO "$base/marketmind-$release-native.tar.gz.sha256"
sha256sum -c "marketmind-$release-native.tar.gz.sha256"
tar -xzf "marketmind-$release-native.tar.gz" --strip-components=1
./deploy/install.sh
```

Use a fresh directory for a first installation; do not unpack a release over
an existing application. An existing Docker deployment requires a separate
migration: verify an off-host backup, stop old timers/services, migrate or
attach its database, and update host-reachable DSNs and pgpass entries before
running the native installer. Do not run both stacks against the same state.
The installer stages the verified archive again in
`releases/<tag>`, creates its locked virtual environment, starts the MCP service,
and checks for an authenticated HTTP `401` on the loopback `/mcp` endpoint.
Application schema migrations run during MCP startup. Downloads and state
remain outside the versioned release. Secrets are 0600 files and do not enter
release artifacts. The daily and periodic timers are installed with their
bundled schedules, but remain disabled on a new host; backup is also disabled.
Existing timer choices are preserved on reruns. Verify authenticated MCP
initialization and logs before enabling jobs. An initial historical backfill
is manual:

```bash
sudo systemctl enable --now marketmind-daily.timer  # only after verifying MCP
sudo systemctl start marketmind-initial-backfill.service
journalctl -fu marketmind-initial-backfill.service
```

### Existing PostgreSQL

Provide `MARKETMIND_ADMIN_SERVICE` as the name of a libpq service entry with
superuser access to the **maintenance** database (`postgres`) to let the
installer provision the dedicated role, database, and extensions. Store the
service and its `.pgpass` outside the release directory with owner-only
permissions. Do not place administrator credentials in `.env` or the
application's secret files. Alternatively have the PostgreSQL administrator
prepare the database and role before installation:

```sql
CREATE ROLE marketmind LOGIN;
-- Set its password using psql's interactive \password marketmind.
CREATE DATABASE marketmind OWNER marketmind;
\connect marketmind
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
```

The existing-server path asks for the assigned application role password and
writes `secrets/app_pgpass` for the DSN host and port. Select a hostname
reachable **from the host**, not a Docker network alias. Install/verify the
pgvector server package before creating the extension. PostgreSQL must permit
password authentication from the application's host. If `app_pgpass` already
exists it is retained; update it yourself when changing a DSN or password.
For a new local cluster accepted at the installer prompt, `local-postgres`
uses `sudo -u postgres psql` over the peer-authenticated local socket to
provision only the Marketmind role/database; it does not export a superuser
password. The application connects to `127.0.0.1` using its own role.

## Daily operation and updates

```bash
systemctl status marketmind-mcp.service
journalctl -u marketmind-mcp.service -n 100
systemctl list-timers 'marketmind-*'
marketmind update                  # latest published native release
marketmind update vX.Y.Z          # selected tag
```

For a private repository, transfer the two release assets from an authenticated
workstation and run `./deploy/update.sh /path/to/marketmind-vX.Y.Z-native.tar.gz`.
`marketmind update` verifies the release archive and checksum, installs locked
dependencies into a new versioned directory, switches `releases/current`, and
checks MCP readiness. It restores the previous release if readiness fails.
Database migrations are not automatically rolled back; take a verified backup
before upgrades. `.env`, credentials, raw data, state, timer schedules, and the
PostgreSQL service remain untouched. Updates require network access to GitHub
and package indexes unless dependencies are already cached. The application
runs as the deployment account; do not store other projects' secrets in that
account. Keep PostgreSQL access limited to the host or a private network.

## Optional dashboard and backups

`marketmind dashboard enable` provisions a `marketmind_ro` login only if the
installer bootstrapped the local database; with an existing server, an
administrator must create that login with `CONNECT` on `marketmind`, `USAGE`
on `public`, and `SELECT` on `agent_runs` only. The dashboard service runs as
`marketmind-dashboard` with systemd credentials and filesystem read ACLs for
job logs; install the `acl` package before enabling. It binds to
`127.0.0.1:8001`. Use SSH forwarding or a private Tailscale Serve mapping,
never a public tunnel. `marketmind dashboard disable` stops the service.

The backup timer is deliberately disabled. To enable, install `jq`, `rclone`,
and the PostgreSQL client utilities, configure an off-host rclone remote, put
`MARKETMIND_BACKUP_REMOTE=remote:path` in root-owned
`/etc/marketmind/backup.conf`, and prepare `/srv/marketmind-backups` writable
by the deployment account. Run a manual backup and restore it to a separate
empty database and file directory using `deploy/restore-check.sh`; only then
`sudo systemctl enable --now marketmind-backup.timer`. Backups include a
`pg_dump` of only Marketmind's database and an archive of raw/state files,
checksums, and an off-host verification. Never point a restore check at the
production database or state directory.
