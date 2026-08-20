# marketmind deployment

Docker-native: one image for the whole app, `docker compose` for the
always-on services (Postgres, the MCP server), plain cron for scheduled
jobs. No systemd, no install/update scripts — setup is four commands.

## Requirements

- Docker with the Compose plugin (`docker compose version` must succeed).
- `cron` (present on essentially every Linux distro by default).
- Network access to GitHub for the initial clone.

## Setup

```bash
git clone git@github.com:Antoniodigiovanni/marketmind.git /srv/marketmind
cd /srv/marketmind
cp .env.example .env
# edit .env: set real values for AI_API_KEY, FRED_API_KEY, SEC_CONTACT_NAME,
# SEC_CONTACT_EMAIL, MARKETMIND_DB_PASSWORD, MARKETMIND_DATABASE_DSN (same
# password in both), MARKETMIND_MCP_TOKEN

docker compose -f docker-compose.prod.yml up -d --build
crontab deploy/crontab
```

`docker compose up -d --build` builds the image, starts Postgres, waits for
it to be healthy, then starts the app. The app applies the Postgres schema
and initialises `research.duckdb` itself on every start — nothing else to
run first.

`crontab deploy/crontab` installs the scheduled jobs (§"Schedule" below).
It **replaces** the current crontab wholesale — if you have other cron
entries on this box, merge them into `deploy/crontab` first
(`crontab -l >> deploy/crontab`, then edit) rather than installing over
them.

## Updating

```bash
cd /srv/marketmind
git pull
docker compose -f docker-compose.prod.yml up -d --build
```

Rebuilds the image from the pulled code and restarts both services. Schema
changes apply themselves on the new container's startup — no separate
migration step.

## Harness MCP wiring

The MCP server listens on `127.0.0.1:${MARKETMIND_MCP_PORT:-8000}`
(streamable-http transport, not stdio — the harness connects to it like it
would connect to any remote MCP server, rather than spawning a subprocess).
Every request must carry the shared secret from `.env` as a bearer token:

```
POST http://127.0.0.1:8000/mcp
Authorization: Bearer <MARKETMIND_MCP_TOKEN>
```

This is a change the harness side must implement — out of scope for this
repository. No ports beyond `127.0.0.1` are opened; nothing off-box can
reach it.

## Schedule (`deploy/crontab`)

| When | Job |
|---|---|
| daily 06:10 | price/macro fetch + news ingest |
| Mondays 06:00 | universe refresh + symbol resolution |
| annually, 5 Jan 06:30 | company profile refresh (`--backfill --profiles-only`) |
| nightly 02:30 | `data/` tarball → `/srv/marketmind-backups/` (14-day retention) |
| nightly 02:35 | Postgres `pg_dump` → `/srv/marketmind-backups/` (14-day retention) |

Every job runs `docker compose run --rm marketmind <command>` against the
same image the always-on services use — no separate job-runner tooling.
`docker compose run` waits for Postgres to be healthy first (compose's own
`depends_on`), so there's no extra readiness check in any cron line.

Edit `deploy/crontab` and reinstall with `crontab deploy/crontab` to change
a schedule.

**Known trade-off:** plain cron has no equivalent of systemd's
`Persistent=true` (a missed run — box was off at 06:10 — just doesn't
happen; cron doesn't catch it up later) or `RandomizedDelaySec` (startup
jitter). Accepted for a single-box deploy.

## Operations

```bash
docker compose -f docker-compose.prod.yml ps                    # service status
docker compose -f docker-compose.prod.yml logs -f marketmind    # MCP server logs
docker compose -f docker-compose.prod.yml logs -f postgres      # Postgres logs
crontab -l                                                      # confirm the schedule is installed
```

Every ingest run also writes an audit row to `agent_runs` (counters +
`error` column), queryable via the `get_ingest_status` MCP tool or directly
against Postgres.

## Rollback

`data/` (DuckDB, raw Parquet/NDJSON) and the Postgres database are backed
up and restored separately — restoring one does not touch the other.

`data/` backups are plain tarballs:

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

Code rollback: `git checkout <previous-tag> && docker compose -f
docker-compose.prod.yml up -d --build`.

Move backups off-box (`rclone`/`restic`) for real disaster recovery — a
same-machine copy is a local safety net, not a backup strategy.
