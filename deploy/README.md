# Marketmind deployment

Marketmind connects to a shared PostgreSQL instance through its own database
and role. On a new homelab, the installer creates PostgreSQL as an independent
Compose project under `/srv/postgres`. Other projects can use that instance,
and Marketmind updates or shutdowns cannot stop it.

A `v*` Git tag publishes an amd64 application image to GHCR and attaches a
matching deployment archive to the GitHub release. The archive contains the
application and shared-PostgreSQL Compose files, `.env.example`, an interactive
installer, host scripts, systemd units, and `release.env`.
The server does not need a source checkout or a local build.

## Requirements

- Linux amd64 with Bash 4+, Docker Engine, Compose, systemd, `curl`, `flock`,
  `sha256sum`, `tar`, and `logrotate`.
- An external Docker network shared with PostgreSQL. This guide calls it
  `postgres-network`; it publishes no port.
- PostgreSQL 16 or later with pgvector installed. The bundled shared image
  includes pgvector.
- A deployment account with Docker access and ownership of Marketmind's local
  state and secret files.

`jq`, `rclone`, and an off-host destination are needed only for Marketmind's
optional backup job. When that job is selected, the installer installs missing
`jq` and `rclone` packages through the host package manager. You still need an
account or reachable storage location for the off-host destination. Keep the
timer disabled until a restore has been tested.

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

Confirm that `RELEASE_TAG` matches the requested tag. A private repository
returns `404` to unauthenticated `curl`; download the two assets with an
authenticated `gh release download` on a trusted workstation and copy them to
the host. A private package also requires `docker login ghcr.io` with
read-package access. Updating the bundle leaves `.env`, `secrets/`, `data/raw/`,
and `state/` alone.

## 2. Run the interactive installer

Run the installer as the normal deployment account, not through `sudo`:

```bash
cd /srv/marketmind
./deploy/install.sh
```

The installer checks Docker access before asking configuration questions. It
requests `sudo` when it installs optional system packages or writes host-level
systemd and logrotate files. It discovers the current account's UID and GID,
prepares the local directories, copies the release's pinned image into `.env`,
creates missing random secrets, creates or connects to the independent shared
database service, starts the application, and installs the schedule.
The generated job services run as that account. Existing secrets, data, and
timer choices are retained when the installer is run again.

The installer also offers the read-only operations dashboard. With the bundled
shared PostgreSQL service, it creates a separate `marketmind_ro` login, limits
that login to `SELECT` on `agent_runs`, generates the browser token and database
password, and starts the optional Compose profile. With an existing PostgreSQL
instance, its administrator must create `marketmind_ro` first; the installer
then records that role's password and starts the dashboard. Choosing no stops a
previously enabled dashboard without deleting its credentials.

For every recurring job, the installer explains its work and expected cost,
asks whether to enable it, and presents the current schedule as the default.
A custom value can be any calendar expression accepted by
`systemd-analyze calendar`. The bundled defaults are:

| Job | Enabled | Default schedule |
|---|---:|---|
| Daily refresh | Yes | Every day at 06:10 |
| Universe refresh | Yes | Monday at 06:00 |
| Company profiles | Yes | 5 January at 06:30 |
| Company financials | Yes | 1 February, May, August, and November at 04:00 |
| European filings | Yes | First day of each month at 05:00 |
| Off-host backup | No | Every day at 02:30 |

The installer also offers to run the initial historical filing backfill. Its
default is a persistent, one-time systemd timer for the next 22:00 local time;
`now`, a custom calendar time, and `skip` are also available. Installation does
not wait for the backfill. The four stages run in order under a shared lock,
are resumable after interruption, and write a completion marker only after all
stages succeed.

```bash
journalctl -fu marketmind-initial-backfill.service
```

## 3. Connect Hermes through MCP

When Hermes runs directly on the same homelab, add this entry to the
`~/.hermes/config.yaml` belonging to the account that runs Hermes:

```yaml
mcp_servers:
  marketmind:
    url: "http://127.0.0.1:8000/mcp"
    headers:
      Authorization: "Bearer ${MARKETMIND_MCP_TOKEN}"
    timeout: 300
    supports_parallel_tool_calls: false
```

Put the contents of `/srv/marketmind/secrets/mcp_token` in that account's
`~/.hermes/.env` without adding the value to `config.yaml`:

```dotenv
MARKETMIND_MCP_TOKEN=<contents of /srv/marketmind/secrets/mcp_token>
```

Keep both files owner-only, restart Hermes, and verify discovery:

```bash
chmod 0600 ~/.hermes/config.yaml ~/.hermes/.env
hermes mcp test marketmind
```

The URL uses loopback deliberately. If the client runs on another machine,
forward the endpoint first with
`ssh -N -L 8000:127.0.0.1:8000 <deploy-user>@<host>`, then use the same URL locally.
If Hermes runs inside a container, its `127.0.0.1` is a different network
namespace and it needs explicit container-network wiring instead.

The rest of this document is the manual equivalent and operational reference.

## Manual installation reference

### Prepare local directories

```bash
cd /srv/marketmind
install -d -m 0700 data/raw state state/universe state/processed state/refresh secrets
sudo install -d -m 0700 -o root -g root /var/log/marketmind
cp -n .env.example .env
chmod 0600 .env
docker network inspect postgres-network >/dev/null 2>&1 || \
  docker network create postgres-network
```

Copy `MARKETMIND_IMAGE` from `release.env` into `.env`. Set
`MARKETMIND_UID=$(id -u)`, `MARKETMIND_GID=$(id -g)`, and the SEC contact
values. `.env` contains non-secret configuration; passwords belong in files
under `secrets/`.

Choose exactly one database path below.

### Attach to an existing PostgreSQL instance

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
  postgres_network:
    external: true
    name: postgres-network
```

Recreate that PostgreSQL service after changing its Compose file. As a
temporary test, the equivalent command is:

```bash
docker network connect --alias postgres-shared postgres-network <postgres-container>
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
MARKETMIND_DATABASE_NETWORK=postgres-network
MARKETMIND_DATABASE_DSN=postgresql://marketmind@postgres-shared:5432/marketmind
MARKETMIND_DASHBOARD_DSN=postgresql://marketmind_ro@postgres-shared:5432/marketmind
```

Create `secrets/app_pgpass` using the exact DSN hostname and database password:

```text
postgres-shared:5432:marketmind:marketmind:<database-password>
```

### Bootstrap the independent shared PostgreSQL instance

Use this path when the homelab does not have PostgreSQL yet. The database stack
lives under `/srv/postgres`, separately from every application:

```text
/srv/postgres/       shared PostgreSQL lifecycle and administrator secret
/srv/marketmind/     Marketmind application and its database-role secret
```

Prepare the shared stack using the PostgreSQL image from `release.env`:

```bash
sudo install -d -m 0755 -o "$(id -un)" -g "$(id -gn)" /srv/postgres
install -d -m 0700 /srv/postgres/secrets
install -m 0644 docker-compose.postgres.yml /srv/postgres/docker-compose.yml
cat > /srv/postgres/.env <<EOF
POSTGRES_IMAGE=<MARKETMIND_POSTGRES_IMAGE from release.env>
POSTGRES_NETWORK=postgres-network
POSTGRES_DATA_VOLUME=postgres-shared-data
EOF
openssl rand -hex 32 > /srv/postgres/secrets/postgres_admin_password
chmod 0600 /srv/postgres/.env
chmod 0644 /srv/postgres/secrets/postgres_admin_password
docker compose --project-directory /srv/postgres \
  -f /srv/postgres/docker-compose.yml up -d --wait
```

Generate Marketmind's separate role password and provision its database:

```bash
openssl rand -hex 32 > secrets/marketmind_database_password
chmod 0600 secrets/marketmind_database_password
./deploy/provision-marketmind-db.sh \
  /srv/postgres /srv/marketmind/secrets/marketmind_database_password
```

The shared service publishes the same `postgres-shared` network alias used for
an existing instance, so the application configuration remains:

```dotenv
MARKETMIND_DATABASE_NETWORK=postgres-network
MARKETMIND_DATABASE_DSN=postgresql://marketmind@postgres-shared:5432/marketmind
MARKETMIND_DASHBOARD_DSN=postgresql://marketmind_ro@postgres-shared:5432/marketmind
```

Use the Marketmind role password in `secrets/app_pgpass`:

```text
postgres-shared:5432:marketmind:marketmind:<marketmind-database-password>
```

The shared PostgreSQL volume persists across container replacement. The
provisioning script enables `vector` and `pg_trgm` only in Marketmind's
database. Later projects join `postgres-network` and receive a separate role
and database. Manage PostgreSQL from `/srv/postgres`, independently of any one
application.

### Create application secrets

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

Keep `marketmind_database_password` and `app_pgpass` at `0600`. The PostgreSQL
administrator password belongs only under `/srv/postgres/secrets`; Marketmind
does not mount or retain it.

### Start and verify Marketmind

```bash
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
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

Before enabling timers on a new database, populate filing history. The daily
SEC job starts from a short checkpoint window and cannot recover older filings
that preceded the deployment. These resumable commands first resolve company
identities, then load issuer filings and 13F history, complete Company Facts
history, and European filings:

```bash
docker compose -f docker-compose.prod.yml run --rm marketmind \
  uv run --frozen --no-sync python -m data.universe.resolve
docker compose -f docker-compose.prod.yml run --rm marketmind \
  uv run --frozen --no-sync python -m data.filings.backfill --issuers
docker compose -f docker-compose.prod.yml run --rm marketmind \
  uv run --frozen --no-sync python -m data.filings.backfill --financials
docker compose -f docker-compose.prod.yml run --rm marketmind \
  uv run --frozen --no-sync python -m data.filings.esef
```

The issuer and financial commands can take a long time for the complete
universe. Re-run either command after an interruption; stored filings and
checkpoints prevent completed work from being inserted again.

### Install the schedule

Install timers only after the MCP smoke test succeeds. On a host with an older
installation, remove its cron entry and disable old timers first:

```bash
sudo rm -f /etc/cron.d/marketmind
sudo systemctl disable --now marketmind-daily.timer marketmind-universe.timer \
  marketmind-profiles.timer marketmind-financials.timer marketmind-esef.timer \
  marketmind-backup.timer \
  2>/dev/null || true
sudo install -o root -g root -m 0644 deploy/systemd/* /etc/systemd/system/
sudo install -o root -g root -m 0644 deploy/logrotate /etc/logrotate.d/marketmind
sudo systemctl daemon-reload
sudo systemctl enable --now marketmind-daily.timer marketmind-universe.timer \
  marketmind-profiles.timer marketmind-financials.timer marketmind-esef.timer
systemctl list-timers 'marketmind-*'
```

Daily refresh, including SEC filings, runs at 06:10; universe refresh Monday at
06:00; profiles on 5 January at 06:30; financials on 1 February, May, August,
and November at 04:00; and European filings on the first of each month at
05:00, in the host timezone. Persistent timers start one missed activation
after reboot. Daily catch-up is ordered and capped at seven days.

```bash
systemctl status marketmind-daily.timer
journalctl -u marketmind-daily.service -n 100 --no-pager
```

## Optional operations dashboard

The interactive installer performs this section automatically when the
dashboard is selected. On an existing installation that uses the bundled
shared PostgreSQL service, the equivalent operation is one command:

```bash
cd /srv/marketmind
./deploy/dashboard.sh enable
```

Use `./deploy/dashboard.sh status` to inspect it and
`./deploy/dashboard.sh disable` to stop it without deleting credentials. The
commands below remain the manual reference for an externally managed
PostgreSQL instance.

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

Put an independent browser password in `secrets/dashboard_token`, make both
files mode `0600`, and start the profile:

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

When the backup timer is selected, the interactive installer installs missing
`jq` and `rclone` packages on supported apt, dnf, yum, zypper, and pacman hosts.
It opens `rclone config` if the named remote does not exist and verifies access
before saving the destination. The remote is configured for the deployment
account because the backup service runs as that account.

For the manual path, install `jq` and `rclone`, configure the deployment
account's rclone remote, create `/srv/marketmind-backups`, and write:

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

After a release is published, run the operator command as the deployment
account. The Docker login saved during installation supplies the
package-registry credential:

```bash
marketmind update
```

This installs the newest published release. Use
`marketmind update vX.Y.Z` when you deliberately want a particular release.
The updater pulls the selected image from GHCR, resolves it to an immutable
digest, and copies the matching Compose files and host scripts out of a stopped
temporary container. It saves the current deployment files under
`state/deployment-releases/`, installs the new bundle, updates the pinned image,
recreates the application and enabled dashboard, and refreshes systemd service
definitions while preserving enabled timers and their schedules. It does not
replace `.env`, secrets, raw data, state, or the independent PostgreSQL stack.

The release archive remains an offline fallback. Copy the archive and its
`.sha256` file into `/srv/marketmind`, then pass the archive path instead:

```bash
cd /srv/marketmind
./deploy/update.sh marketmind-vX.Y.Z-deploy.tar.gz
```

That path verifies both the checksum and archive structure before changing the
installation. The registry path needs only the package-read PAT already stored
by `docker login`; it does not need authenticated access to GitHub release
assets.

Inspect the result with:

```bash
cat state/current-release
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml logs --tail=100 marketmind
systemctl list-timers 'marketmind-*'
```

The old container image and saved deployment files remain available for a
code-only rollback. A rollback across incompatible database migrations also
requires restoring the matching database and local files; treat the image,
database dump, and files as one set.
