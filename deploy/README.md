# marketmind deployment

Docker-native: one image for the whole app, `docker compose` for the
always-on services (Postgres, the MCP server), plain cron for scheduled
jobs. No systemd and no install/update scripts.

## Requirements

- Docker with the Compose plugin (`docker compose version` must succeed).
- `cron` (present on essentially every Linux distro by default).
- `flock` from util-linux (used to prevent overlapping scheduled runs).
- `logrotate` for bounded scheduled-job logs.
- Network access to GitHub for the initial clone.
- A long-lived deployment account with permission to run Docker. Its numeric
  UID/GID owns the checkout and is also used inside the app container.

## Fresh setup

```bash
deploy_user="$(id -un)"
deploy_group="$(id -gn)"
sudo install -d -m 0755 -o "$deploy_user" -g "$deploy_group" \
  /srv/marketmind
sudo install -d -m 0700 -o root -g root \
  /srv/marketmind-backups /var/log/marketmind
git clone git@github.com:Antoniodigiovanni/marketmind.git /srv/marketmind
cd /srv/marketmind
install -d -m 0700 data/raw state state/universe state/processed state/refresh
cp .env.example .env
# edit .env: set real credentials and set MARKETMIND_UID/GID to the output
# of `id -u` / `id -g`. Keep MARKETMIND_DATABASE_DSN pointed at
# postgresql://marketmind@postgres:5432/marketmind inside Compose.
chmod 0600 .env

docker compose -f docker-compose.prod.yml up -d --build
# Verify the MCP initialize request described below before enabling schedules.
sudo install -o root -g root -m 0644 deploy/crontab /etc/cron.d/marketmind
sudo install -o root -g root -m 0644 deploy/logrotate /etc/logrotate.d/marketmind
```

`docker compose up -d --build` builds the image, starts Postgres, waits for
it to be healthy, then starts the app. The app applies pending versioned
Postgres migrations on every start. The UID/GID mapping lets the non-root
container write `data/raw/` and
`state/` without changing ownership behind the host operator's back. The rest
of its filesystem is read-only; temporary files live on a restricted `tmpfs`,
Linux capabilities are dropped, privilege escalation is disabled, and
generated files use a private `0077` umask.

The root-owned `/etc/cron.d/marketmind` file installs the scheduled jobs
(§"Schedule" below) without replacing or modifying other root cron entries.
Install it only after the authenticated MCP smoke test succeeds; a running
container is not by itself proof that schema initialization completed.

## Migrating an existing systemd install

Do not use the fresh-setup sequence over an existing installation. The old
timers remain installed under `/etc/systemd/system` even after their source
files disappear from the checkout, and enabling cron first would run every
scheduled job twice.

1. Record `id -u marketmind` and `id -g marketmind`; use those values for
   `MARKETMIND_UID` and `MARKETMIND_GID` so the existing `data/` ownership is
   preserved.
2. Stop and disable all five old timers before taking the cutover backup or
   installing cron:

   ```bash
   sudo systemctl disable --now \
     marketmind-daily.timer marketmind-universe.timer \
     marketmind-profiles.timer marketmind-backup.timer \
     marketmind-update.timer
   ```

3. Stop the old service while files are being copied. Back up
   `/srv/marketmind/data`, then move derived files and current universe
   snapshots into `state/` before
   `git pull` can replace tracked snapshots:

   ```bash
   sudo -u marketmind install -d -m 0700 \
     /srv/marketmind/state /srv/marketmind/state/universe \
     /srv/marketmind/state/processed /srv/marketmind/state/refresh
   sudo -u marketmind cp -a /srv/marketmind/data/processed/. \
     /srv/marketmind/state/processed/
   sudo -u marketmind cp /srv/marketmind/data/universe/*.csv \
     /srv/marketmind/state/universe/
   ```

   Omit the processed-data `cp` command if that source does not exist. Take a
   `pg_dump -Fc` from the current PostgreSQL DSN before continuing.
4. Update the checkout and `.env`. Add the database password, the
   container-reachable DSN, MCP bearer token, port if non-default, and the
   recorded UID/GID. Change `/srv/marketmind-backups` to root ownership and
   mode `0700`; scheduled backups now run from the isolated root cron file.

   ```bash
   sudo chown root:root /srv/marketmind-backups
   sudo chmod 0700 /srv/marketmind-backups
   ```
5. Start only the new database first. If step 3 produced a Postgres dump,
   restore it into the named volume before starting the app:

   ```bash
   docker compose -f docker-compose.prod.yml up -d postgres
   docker compose -f docker-compose.prod.yml exec -T postgres \
     pg_restore -U marketmind -d marketmind --clean --if-exists \
     < /srv/marketmind-backups/pre-compose-postgres.dump
   docker compose -f docker-compose.prod.yml up -d --build marketmind
   ```

   With no existing PostgreSQL database, the new one starts empty; run the
   relevant historical backfills before treating it as production-ready.
6. Change the MCP client from a spawned stdio process to the authenticated
   HTTP endpoint below, send an `initialize` request, and check both service
   logs. Install `/etc/cron.d/marketmind` only after that smoke test succeeds.
7. Once the new schedule has run successfully, remove the obsolete unit files:

   ```bash
   sudo rm -f \
     /etc/systemd/system/marketmind-daily.{service,timer} \
     /etc/systemd/system/marketmind-universe.{service,timer} \
     /etc/systemd/system/marketmind-profiles.{service,timer} \
     /etc/systemd/system/marketmind-backup.{service,timer} \
     /etc/systemd/system/marketmind-update.{service,timer} \
     /usr/local/sbin/marketmind-update
   sudo systemctl daemon-reload
   ```

## Updating

```bash
cd /srv/marketmind
git pull
docker compose -f docker-compose.prod.yml up -d --build
sudo install -o root -g root -m 0644 deploy/crontab /etc/cron.d/marketmind
sudo install -o root -g root -m 0644 deploy/logrotate /etc/logrotate.d/marketmind
```

Rebuilds the image from the pulled code and recreates services whose image or
configuration changed. Pending versioned migrations apply on app startup under
a database advisory lock.
Migration files are immutable and checksum-verified; a database newer than
the checked-out code refuses to start instead of attempting an unsafe rollback.

## MCP client wiring

The MCP server listens on `127.0.0.1:${MARKETMIND_MCP_PORT:-8000}`
(streamable-http transport, not stdio — the client connects to it like it
would connect to any remote MCP server, rather than spawning a subprocess).
Every request must carry the shared secret from `.env` as a bearer token:

```
POST http://127.0.0.1:8000/mcp
Authorization: Bearer <MARKETMIND_MCP_TOKEN>
```

This is a change the client side must implement — out of scope for this
repository. No ports beyond `127.0.0.1` are opened; nothing off-box can
reach it.

## Schedule (`/etc/cron.d/marketmind`)

| When | Job |
|---|---|
| daily 06:10 | equity/benchmark price + macro fetch, news ingest, market-view structuring |
| Mondays 06:00 | universe refresh + symbol resolution |
| annually, 5 Jan 06:30 | company profile refresh (`--backfill --profiles-only`) |
| nightly 02:30 | `data/raw/` + mutable state tarball → `/srv/marketmind-backups/` (14-day retention) |
| nightly 02:35 | Postgres `pg_dump` → `/srv/marketmind-backups/` (14-day retention) |

Every job runs `docker compose run --rm marketmind <command>` against the
same image the always-on services use — no separate job-runner tooling.
`docker compose run` waits for Postgres to be healthy first (compose's own
`depends_on`), so there's no extra readiness check in any cron line.
Every data job and the filesystem backup take the same non-blocking lock at
`state/refresh/daily-refresh.lock`. The complete daily entry point owns that
lock inside the container, while the other cron jobs use the same bind-mounted
file from the host. MCP-triggered and scheduled runs therefore cannot overlap.
Backups are written with mode `0600` to temporary names and renamed into place
only after validation or `pg_dump` succeeds. The filesystem backup takes the
shared data-job lock and validates the archive without interrupting the MCP
service; structured state is protected separately by `pg_dump`.
Scheduled output is appended under `/var/log/marketmind/` and retained for 14
daily rotations; it does not depend on a local cron-mail setup.

Edit `deploy/crontab` and reinstall it with the `install` command from setup to
change a schedule.

Benchmark series use the existing daily entry and PostgreSQL price tables; no
new cron entry or schema migration is required. The first daily run after
deployment backfills missing benchmark history from 2015-01-01, and subsequent
runs are incremental. Operators can inspect stored coverage with the
`get_benchmarks` MCP tool before relying on a series.

**Known trade-off:** plain cron has no equivalent of systemd's
`Persistent=true` (a missed run — box was off at 06:10 — just doesn't
happen; cron doesn't catch it up later) or `RandomizedDelaySec` (startup
jitter). Accepted for a single-box deploy.

## Operations

```bash
docker compose -f docker-compose.prod.yml ps                    # service status
docker compose -f docker-compose.prod.yml logs -f marketmind    # MCP server logs
docker compose -f docker-compose.prod.yml logs -f postgres      # Postgres logs
cat /etc/cron.d/marketmind                                      # confirm the schedule is installed
tail -f /var/log/marketmind/daily.log                            # scheduled-job output
tail -f state/refresh/logs/daily-refresh-*.log                   # MCP-triggered refresh output
```

Every ingest run also writes an audit row to `agent_runs` (counters +
`error` column), queryable via the `get_ingest_status` MCP tool or directly
against Postgres.

## Rollback

`data/raw/`, derived files, universe snapshots, and the PostgreSQL database are
backed up on the same schedule; filesystem state and PostgreSQL are restored
separately.

Filesystem backups are plain tarballs containing `data/raw/` and `state/`:

```bash
tar -xzf /srv/marketmind-backups/marketmind-YYYY-MM-DD-HHMM.tar.gz -C /srv/marketmind
```

Postgres backups are `pg_dump -Fc` custom-format dumps, restored into a
running (empty) container:

```bash
docker compose -f docker-compose.prod.yml exec -T postgres \
  pg_restore -U marketmind -d marketmind --clean --if-exists \
  < /srv/marketmind-backups/marketmind-postgres-YYYY-MM-DD-HHMM.dump
```

Code rollback is safe only when every migration since the target tag is
backward-compatible. Otherwise stop the app and restore the matching Postgres
and filesystem backups before starting the older image. The migration ledger
deliberately refuses to run a checkout older than the database.

Disable `/etc/cron.d/marketmind` during any rollback so jobs cannot start from
a half-restored checkout. Afterward, install the schedule shipped by the target
release. Rolling back to a pre-Compose release also requires restoring its
systemd units and switching the MCP client back to stdio; checking out the tag
alone is not a complete rollback.

Move backups off-box (`rclone`/`restic`) for real disaster recovery — a
same-machine copy is a local safety net, not a backup strategy.
