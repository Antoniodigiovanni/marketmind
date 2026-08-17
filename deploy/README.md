# marketmind deployment

One-command deployment for a hardened Linux host (systemd). The harness and
marketmind run on the same box; marketmind exposes **no network listener** —
the harness spawns the MCP server over stdio, and all jobs run as an isolated
system user via hardened systemd units.

## Requirements

- Linux with systemd (Ubuntu/Debian recommended).
- Docker with the Compose plugin (`docker compose version` must succeed) —
  runs the production Postgres container. Not installed by these scripts;
  install it yourself first (e.g. Docker's official install script or your
  distro's package).
- Root access for the installer.
- Network access to GitHub (public clone by default; see "Private repo").

## Install

```bash
sudo bash deploy/install.sh
```

Or as a one-liner (public repo):

```bash
curl -fsSL https://raw.githubusercontent.com/Antoniodigiovanni/marketmind/main/deploy/install.sh | sudo bash
```

The installer is idempotent — rerun it to update the checkout, re-sync
dependencies, and refresh the systemd units.

## Updating

For an existing install, one command:

```bash
sudo /usr/local/sbin/marketmind-update
```

It pulls the configured branch/tag, re-syncs dependencies, applies schema
changes, reinstalls any changed units, re-arms the timers, and verifies.

The persistent config lives at `/srv/marketmind/deploy.conf` (written on first
install, read by both scripts, survives reruns):

```bash
BRANCH=main
REPO_URL=git@github.com:Antoniodigiovanni/marketmind.git
ENABLE_AUTO_UPDATE=0
```

- **Pin a stable release**: set `BRANCH=v0.1.0` (any git tag) and run
  `update.sh`; install and update both accept branch or tag.
- **One-off override**: `sudo BRANCH=main /usr/local/sbin/marketmind-update`.
- **Auto-update (opt-in)**: set `ENABLE_AUTO_UPDATE=1` in `deploy.conf` and run
  the update once; a weekly Monday 05:30 timer then applies updates
  unattended. It runs as root by design (it reinstalls units), so leave it
  off unless you want unattended code changes.

### Private repo

The raw-URL one-liner only works for a public repo. For a private repo, clone
with a credential method you already trust and run the script locally:

```bash
git clone git@github.com:Antoniodigiovanni/marketmind.git
cd marketmind
sudo bash deploy/install.sh   # or: REPO_URL=git@github.com:... sudo -E bash deploy/install.sh
```

## What it does

1. Creates a system user `marketmind` (no shell) owning the install.
2. Clones/updates the repo at `/srv/marketmind`.
3. Installs the `uv` binary (static, arch-aware) to `/usr/local/bin`.
4. `uv sync --frozen` (dependencies locked by `uv.lock`).
5. Installs `.env` from `ENV_FILE`; **aborts** when no `.env` exists or when
   it still contains an example placeholder (`AI_API_KEY`, `FRED_API_KEY`,
   `SEC_CONTACT_NAME`/`_EMAIL`, `MARKETMIND_DB_PASSWORD`,
   `MARKETMIND_DATABASE_DSN`).
6. Starts the production Postgres container (`docker-compose.prod.yml`,
   persistent named volume, bound to `127.0.0.1` only) and applies
   `data/db/schema_postgres.sql` against it.
7. Initialises `data/db/research.duckdb` from `schema.sql`.
8. Installs hardened systemd units and enables timers.
9. Installs the root-owned update script to `/usr/local/sbin/marketmind-update`.

`update.sh` repeats steps 6–7 on every run (`docker compose ... up -d --wait`
is idempotent; the schema apply re-runs the same `CREATE TABLE IF NOT
EXISTS`/`CREATE INDEX IF NOT EXISTS` DDL, safe against an already-current
database), so a later migration to `schema_postgres.sql` is picked up by the
next `marketmind-update` run same as any other code change.

### Overrides

```bash
APP_USER=custom  INSTALL_DIR=/opt/marketmind  BRANCH=company-profiles \
ENV_FILE=/path/to/secrets.env sudo -E bash deploy/install.sh
```

## Schedules (systemd timers)

| Timer | Schedule | Job |
|---|---|---|
| `marketmind-daily.timer` | daily 06:10 (local) | `data/fetch.py` + `agent.ingestion` |
| `marketmind-universe.timer` | Mondays 06:00 | `data.universe.update` + `data.universe.resolve` (fills `symbol_map`, which the daily price fetch requires) |
| `marketmind-profiles.timer` | annually on 5 Jan, 06:30 | `agent.ingestion --backfill --profiles-only` |
| `marketmind-backup.timer` | nightly 02:30 | `deploy/backup.sh` → `/srv/marketmind-backups/` (14-day retention) |
| `marketmind-postgres-backup.timer` | nightly 02:35 | `pg_dump` of the Postgres database → `/srv/marketmind-backups/` (14-day retention); runs as root, see "Hardening notes" |
| `marketmind-update.timer` | Mondays 05:30 (opt-in) | `/usr/local/sbin/marketmind-update` self-update |

Adjust with `systemctl edit marketmind-daily.timer` (override `OnCalendar`).

The annual company-profile refresh runs on 5 January and rewrites a new profile
version for every company without touching news processing. It can also be
triggered manually:

```bash
sudo -u marketmind env HOME=/srv/marketmind UV_CACHE_DIR=/srv/marketmind/.cache \
  bash -c 'cd /srv/marketmind && /usr/local/bin/uv run python -m agent.ingestion "$(date +%F)" --backfill --profiles-only'
```

Note: the profile refresh requires the versioned company-profiles feature
(the `--profiles-only` flag) to be deployed.

## Harness MCP wiring

The harness client should spawn the server as the marketmind user, from the
install directory:

```bash
sudo -u marketmind env HOME=/srv/marketmind UV_CACHE_DIR=/srv/marketmind/.cache \
  bash -c 'cd /srv/marketmind && exec /usr/local/bin/uv run --frozen --no-sync python -m server.mcp'
```

No ports are opened; the server speaks MCP stdio on the spawned process's
stdin/stdout.

## Hardening notes

- All services run as `marketmind` with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, `RestrictSUIDSGID`, and `UMask=0077`.
- Only `data/` and the uv cache are writable; everything else is read-only.
- `.env` is `0600`, owned by `marketmind`.
- Jobs run `uv run --frozen --no-sync`: with `ProtectSystem=strict` the virtual
  environment is read-only, so uv must never try to re-lock or re-sync at run
  time — it would fail the unit with a read-only filesystem error.
- The root-run update script lives at `/usr/local/sbin/marketmind-update`, not
  in the install tree. That tree is writable by `marketmind`, the user that
  processes untrusted network input; a root `ExecStart=` pointing into it would
  be one file write away from privilege escalation.
- Backups live outside the install tree at `/srv/marketmind-backups/`; move
  them off-box (e.g. `rclone`/`restic`) for real disaster recovery.

## Operations

```bash
journalctl -u marketmind-daily.service -e      # job logs
systemctl list-timers | grep marketmind        # next runs
sudo -u marketmind env HOME=/srv/marketmind bash -c \
  'cd /srv/marketmind && /usr/local/bin/uv run --frozen --no-sync python -m data.db.init'  # db check
```

## Rollback

`data/` (DuckDB, raw Parquet/NDJSON) and the Postgres database are backed up
and restored separately — restoring one does not touch the other.

`data/` backups are plain tarballs:

```bash
sudo tar -xzf /srv/marketmind-backups/marketmind-YYYY-MM-DD-HHMM.tar.gz -C /srv/marketmind
sudo chown -R marketmind:marketmind /srv/marketmind/data
```

Postgres backups are `pg_dump -Fc` custom-format dumps, restored into a
running (empty) container:

```bash
sudo docker compose -f /srv/marketmind/docker-compose.prod.yml \
  --project-directory /srv/marketmind exec -T postgres \
  pg_restore -U marketmind -d marketmind --clean --if-exists \
  < /srv/marketmind-backups/marketmind-postgres-YYYY-MM-DD-HHMM.dump
```

Code rollback: `sudo BRANCH=<previous-tag> bash deploy/install.sh`.
