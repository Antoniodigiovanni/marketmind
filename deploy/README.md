# Marketmind deployment

The normal homelab deployment runs only the Marketmind containers and connects
them to an existing PostgreSQL instance. Marketmind gets its own database and
roles; it does not share another application's database or tables. If the host
has no PostgreSQL instance, the optional managed-database Compose file starts
one for Marketmind.

A `v*` Git tag publishes an amd64 application image to GHCR and attaches a
matching deployment archive to the GitHub release. The archive contains both
Compose files, `.env.example`, host scripts, systemd units, and `release.env`.
The server does not need a source checkout or a local build.

## Requirements

- Linux amd64 with Docker Engine, Compose, systemd, `flock`, `sha256sum`,
  `tar`, and `logrotate`.
- An external Docker network shared with PostgreSQL. This guide calls it
  `marketmind-database`; it publishes no port.
- PostgreSQL 16 or later with pgvector installed. The managed image already
  includes pgvector.
- A deployment account with Docker access and ownership of Marketmind's local
  state and secret files.

`jq`, `rclone`, and an off-host destination are needed only for Marketmind's
optional backup job. Keep that timer disabled for the initial deployment.

## 1. Download and verify a release

Replace `vX.Y.Z` with a published release tag:

```bash
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" /srv/marketmind
cd /srv/marketmind
release_tag=vX.Y.Z
release_base="https://github.com/Antoniodigiovanni/marketmind/releases/download/$release_tag"
curl -fLO "$release_base/marketmind-$release_tag-deploy.tar.gz"
curl -fLO "$release_base/marketmind-$release_tag-deploy.tar.gz.sha256"
sha256sum --check "marketmind-$release_tag-deploy.tar.gz.sha256"
tar -tzf "marketmind-$release_tag-deploy.tar.gz"
tar -xzf "marketmind-$release_tag-deploy.tar.gz"
cat release.env
```

Confirm that `RELEASE_TAG` matches the requested tag. If the repository or
package is private, authenticate the downloads and run `docker login ghcr.io`
with read-package access. Updating the bundle leaves `.env`, `secrets/`,
`data/raw/`, and `state/` alone.

## 2. Prepare local directories

```bash
cd /srv/marketmind
install -d -m 0700 data/raw state state/universe state/processed state/refresh secrets
sudo install -d -m 0700 -o root -g root /var/log/marketmind
cp -n .env.example .env
chmod 0600 .env
docker network inspect marketmind-database >/dev/null 2>&1 || \
  docker network create marketmind-database
```

Copy `MARKETMIND_IMAGE` from `release.env` into `.env`. Set
`MARKETMIND_UID=$(id -u)`, `MARKETMIND_GID=$(id -g)`, and the SEC contact
values. `.env` contains non-secret configuration; passwords belong in files
under `secrets/`.

Choose exactly one database path below.

## 3A. Attach to an existing PostgreSQL instance

This is the recommended path when several projects share one PostgreSQL
instance. Give each project a separate database and owner role:

```text
one PostgreSQL instance
├── marketmind database / marketmind role
├── project_b database / project_b role
└── project_c database / project_c role
```

Add the existing PostgreSQL container to the external network in the Compose
project that owns it. The persistent form is:

```yaml
services:
  postgres:
    networks:
      marketmind_database:
        aliases:
          - postgres-shared

networks:
  marketmind_database:
    external: true
    name: marketmind-database
```

Recreate that PostgreSQL service after changing its Compose file. As a
temporary test, the equivalent command is:

```bash
docker network connect --alias postgres-shared marketmind-database <postgres-container>
```

Use the database administrator account to create Marketmind's role, database,
and extensions. Generate a password with `openssl rand -hex 32`, save it in a
password manager, and enter the same value at the `\password` prompt:

```sql
CREATE ROLE marketmind LOGIN;
\password marketmind
CREATE DATABASE marketmind OWNER marketmind;
\connect marketmind
CREATE EXTENSION vector;
CREATE EXTENSION pg_trgm;
```

The server package must provide pgvector before `CREATE EXTENSION vector` can
succeed. Marketmind owns only its database. Do not point it at a database that
already contains another project's tables.

Set these values in `.env`:

```dotenv
MARKETMIND_DATABASE_NETWORK=marketmind-database
MARKETMIND_DATABASE_DSN=postgresql://marketmind@postgres-shared:5432/marketmind
MARKETMIND_DASHBOARD_DSN=postgresql://marketmind_ro@postgres-shared:5432/marketmind
```

Create `secrets/app_pgpass` using the exact DSN hostname and database password:

```text
postgres-shared:5432:marketmind:marketmind:<database-password>
```

## 3B. Create a managed PostgreSQL instance

Use this path only when no shared PostgreSQL instance exists. Copy both image
references from `release.env` into `.env`, use `postgres` in both DSNs, and
keep `MARKETMIND_DATABASE_NETWORK=marketmind-database`:

```dotenv
MARKETMIND_DATABASE_DSN=postgresql://marketmind@postgres:5432/marketmind
MARKETMIND_DASHBOARD_DSN=postgresql://marketmind_ro@postgres:5432/marketmind
```

Generate one database password. Put the plain password in
`secrets/postgres_password` and create `secrets/app_pgpass` as:

```text
postgres:5432:marketmind:marketmind:<database-password>
```

The managed PostgreSQL volume persists across container replacement. Changing
the secret later does not change the password inside an initialized database;
alter the database role at the same time.

## 4. Create application secrets

For either database path, create these files after running `umask 077`:

| File | Content |
|---|---|
| `app_pgpass` | libpq password line shown in the selected database path |
| `mcp_token` | independent random bearer token |
| `llm_api_key` | model-provider key |
| `fred_api_key` | FRED key |

Generate random values with `openssl rand -hex 32`. Make the files readable
only by the numeric UID configured in `.env`; libpq rejects an `.pgpass` file
with permissive permissions:

```bash
chmod 0600 secrets/app_pgpass secrets/mcp_token secrets/llm_api_key secrets/fred_api_key
```

For managed mode only, keep the `secrets/` directory at `0700` and make
`secrets/postgres_password` mode `0644`. The PostgreSQL entrypoint changes to
its internal UID before reading the mounted file, while the private parent
directory prevents host users from traversing to it.

## 5. Start and verify Marketmind

With an existing shared PostgreSQL instance:

```bash
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

With the managed PostgreSQL instance:

```bash
docker compose -f docker-compose.prod.yml -f docker-compose.managed-db.yml config --quiet
docker compose -f docker-compose.prod.yml -f docker-compose.managed-db.yml pull
docker compose -f docker-compose.prod.yml -f docker-compose.managed-db.yml up -d
```

Then inspect the application:

```bash
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 marketmind
```

The MCP service applies versioned migrations on startup. It will fail if the
database is unreachable, an extension is missing, or the role lacks DDL rights
in its own database. Send an authenticated MCP `initialize` request to
`http://127.0.0.1:8000/mcp` with `Authorization: Bearer <mcp_token>`. The port
is bound to loopback.

## 6. Install the schedule

Install timers only after the MCP smoke test succeeds. On a host with an older
installation, remove its cron entry and disable old timers first:

```bash
sudo rm -f /etc/cron.d/marketmind
sudo systemctl disable --now marketmind-daily.timer marketmind-universe.timer \
  marketmind-profiles.timer marketmind-financials.timer marketmind-backup.timer \
  2>/dev/null || true
sudo install -o root -g root -m 0644 deploy/systemd/* /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/logrotate /etc/logrotate.d/marketmind
sudo systemctl daemon-reload
sudo systemctl enable --now marketmind-daily.timer marketmind-universe.timer \
  marketmind-profiles.timer marketmind-financials.timer
systemctl list-timers 'marketmind-*'
```

Daily refresh runs at 06:10; universe refresh Monday at 06:00; profiles on
5 January at 06:30; and financials on 1 February, May, August, and November at
04:00, in the host timezone. Persistent timers start one missed activation
after reboot. Daily catch-up is ordered and capped at seven days.

```bash
systemctl status marketmind-daily.timer
journalctl -u marketmind-daily.service -n 100 --no-pager
```

## Optional operations dashboard

Create a separate read-only role through the shared instance's normal
administration path. Use a different password from the application role:

```sql
CREATE ROLE marketmind_ro LOGIN;
\password marketmind_ro
GRANT CONNECT ON DATABASE marketmind TO marketmind_ro;
\connect marketmind
GRANT USAGE ON SCHEMA public TO marketmind_ro;
GRANT SELECT ON agent_runs TO marketmind_ro;
```

Create `secrets/dashboard_pgpass` with the hostname selected above:

```text
postgres-shared:5432:marketmind:marketmind_ro:<read-only-password>
```

For managed mode, replace `postgres-shared` with `postgres`. Put an independent
browser password in `secrets/dashboard_token`, make both files mode `0600`, and
start the profile:

```bash
docker compose -f docker-compose.prod.yml --profile dashboard up -d dashboard
```

The dashboard binds to loopback. Reach it with
`ssh -N -L 8001:127.0.0.1:8001 <deploy-user>@<host>` or an authorized tailnet.
It receives only the read-only role and its browser token.

```bash
tailscale serve --bg http://127.0.0.1:8001
```

## Optional backups

Leave `marketmind-backup.timer` disabled for now. When PostgreSQL is shared,
the preferred design is a centralized backup of every database plus a tested
per-database restore. Marketmind's optional job also produces a coordinated
set containing a custom-format dump of only the `marketmind` database and its
local `data/raw` and `state` directories.

To use that job, install `jq` and `rclone`, create `/srv/marketmind-backups`,
configure root's rclone remote, and write:

```bash
sudo install -d -m 0700 /etc/marketmind /srv/marketmind-backups
printf 'MARKETMIND_BACKUP_REMOTE=offsite:marketmind\n' | \
  sudo tee /etc/marketmind/backup.conf >/dev/null
sudo chmod 0600 /etc/marketmind/backup.conf
sudo systemctl start marketmind-backup.service
```

The backup script runs the pinned PostgreSQL image as a client on the shared
database network; it does not assume Marketmind owns the server container.
After fetching a `.ready` set, test it in an isolated temporary PostgreSQL
container:

```bash
sudo /srv/marketmind/deploy/restore-check.sh \
  /srv/marketmind-backups/marketmind-<stamp>.ready
```

Enable the timer only after that restore succeeds:

```bash
sudo systemctl enable --now marketmind-backup.timer
```

## Updating and rollback

Download and verify the next release bundle, inspect `release.env`, extract
it, and update the image digest in `.env`. Use the same Compose command as the
selected database path, then reinstall the systemd units. Keep the previous
release archive and image digest. A rollback across incompatible migrations
also requires restoring the matching database and local files; treat the
image, database dump, and files as one set.
