# marketmind VPS deployment

The VPS pulls a published image and downloads its matching release bundle. It
never needs a source checkout or a local image build. A `v*` Git tag publishes
an amd64 image to GHCR and attaches `marketmind-<tag>-deploy.tar.gz` and its
checksum to the GitHub release. `release.env` in that archive pins the app and
PostgreSQL images by digest. Inspect the workflow run before using a release.

## Requirements

Linux amd64 VPS with Docker Engine and Compose, systemd, `flock`, `sha256sum`,
`tar`, and `logrotate`. `jq`, PostgreSQL client tools (`pg_restore`), and
`rclone` are needed when backups are configured. The deployment account needs
Docker access and owns `data/raw`, `state`, and application secret files. Log
in to GHCR if the published package is private.

## 1. Download a release

Create `/srv/marketmind` with the deployment account as owner. Replace
`vX.Y.Z` below with the actual published tag, then download both release
assets directly from GitHub Releases:

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

Check that `RELEASE_TAG` matches the downloaded tag. The archive contains
Compose, `.env.example`, `deploy/`, and `release.env`; it contains no
credentials. On an update, extraction leaves `.env`, `secrets/`, `data/raw/`,
and `state/` alone.
If the repository or GHCR package is private, authenticate the download and
`docker login ghcr.io` with a token that has read access.

## 2. Configure host state and credentials

```bash
cd /srv/marketmind
install -d -m 0700 data/raw state state/universe state/processed state/refresh secrets
sudo install -d -m 0700 -o root -g root /srv/marketmind-backups /var/log/marketmind
cp -n .env.example .env
chmod 0600 .env
```

Edit `.env`: copy both full `@sha256:` image references from `release.env` to
`MARKETMIND_IMAGE` and `MARKETMIND_POSTGRES_IMAGE`; set `MARKETMIND_UID` and
`MARKETMIND_GID` to `id -u` and `id -g`; set SEC contact details; keep the
`postgres` hostname in the two DSNs. `.env` holds only non-secret Compose
values. Do not store credentials in it.

Create these untracked files in `secrets/`. Generate independent random
passwords with `openssl rand -hex 32`, record them in a password manager, and
write files with `umask 077`:

| File | Content | Service |
|---|---|---|
| `postgres_password` | Database password | PostgreSQL |
| `app_pgpass` | `postgres:5432:marketmind:marketmind:<database password>` | MCP and jobs |
| `mcp_token` | Bearer token | MCP |
| `llm_api_key` | Model provider key | MCP and jobs |
| `fred_api_key` | FRED key | MCP and jobs |
| `dashboard_pgpass` | `postgres:5432:marketmind:marketmind_ro:<read-only password>` | Optional dashboard |
| `dashboard_token` | Browser password | Optional dashboard |

`app_pgpass` and the optional `dashboard_pgpass` must be owned by the
container's numeric UID and mode `0600`, as libpq requires. Keep application
secret files owner-only as well. Set `postgres_password` to mode `0644` inside
the owner-only `secrets/` directory: the PostgreSQL entrypoint drops to its
own UID before reading `POSTGRES_PASSWORD_FILE`, and Compose file-backed
secrets retain host file permissions. The private directory prevents other
host users from traversing to that file; only the PostgreSQL container gets
it mounted. Run `chmod 0644 secrets/postgres_password` before starting Compose.
Use hex database passwords so `.pgpass` needs no escaping. The PostgreSQL image
reads `POSTGRES_PASSWORD_FILE`; the application and dashboard read only their
mounted secrets. Changing the password on an already initialized database
requires changing the database role password as well as these files.

```bash
docker compose -f docker-compose.prod.yml config --quiet
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
docker compose -f docker-compose.prod.yml logs --tail=100 marketmind
```

The MCP server applies versioned migrations at startup. Confirm a clean start
and send an authenticated MCP `initialize` request to
`http://127.0.0.1:8000/mcp` with `Authorization: Bearer <mcp_token>` before
enabling timers. The host port is loopback-only.

## 3. Back up and prove recovery

This step is optional for the initial deployment. Skip it if no offsite
destination is available yet, and leave `marketmind-backup.timer` disabled.
The application and other scheduled jobs do not depend on rclone. Until this
step is completed, the VPS is the only copy of new data.

Configure root's rclone remote to point outside the VPS. For a remote named
`offsite`, create the service configuration:

```bash
sudo install -d -m 0700 /etc/marketmind
printf 'MARKETMIND_BACKUP_REMOTE=offsite:marketmind\n' | \
  sudo tee /etc/marketmind/backup.conf >/dev/null
sudo chmod 0600 /etc/marketmind/backup.conf
sudo rclone lsd offsite:
```

For a manual test, invoke the backup service after installing the systemd
units below, but before enabling its timer: `sudo systemctl start
marketmind-backup.service`. It creates one timestamped `.ready` directory
with `postgres.dump`, `files.tar.gz`, checksums, and a manifest. The script
holds the shared data-job lock, validates the files, copies them offsite, and
checks the remote bytes by downloading them. Local sets are retained for 14
days. Set remote retention in the storage provider. The database dump is internally
consistent; concurrent MCP writes can occur on either side of the file
snapshot. Pause external writes for a strict cutover snapshot.

Fetch a set back from the remote and perform an isolated restore drill:

```bash
sudo rclone copy offsite:marketmind/marketmind-<stamp>.ready \
  /srv/marketmind-backups/marketmind-<stamp>.ready --checksum
sudo /srv/marketmind/deploy/restore-check.sh \
  /srv/marketmind-backups/marketmind-<stamp>.ready
```

The drill checks hashes, extracts files into a temporary directory, restores
PostgreSQL into a disposable database, queries `agent_runs`, and removes the
disposable database. Repeat it periodically and after changing backup tooling.

## 4. Install the systemd schedule

On an existing host, stop old cron and timers before enabling these units so
each job has only one scheduler. Install after the MCP smoke check. Enable the
backup timer only after completing the optional backup and restore step:

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
# Later, after configuring the offsite remote and passing a restore drill:
# sudo systemctl start marketmind-backup.service
# sudo systemctl enable --now marketmind-backup.timer
systemctl list-timers 'marketmind-*'
```

Daily refresh runs at 06:10; universe Monday 06:00; profiles 5 January
06:30; financials 1 February/May/August/November 04:00; backup nightly
02:30, all in the host's timezone. `Persistent=true` starts one missed
activation after reboot. `agent.catchup` then queries the last successful
daily date in `agent_runs` and processes missing dates in order, capped at
seven days. With no success row it starts today. Longer outages fail loudly
and require an operator-selected backfill; old RSS headlines may no longer
be available.

```bash
systemctl status marketmind-daily.timer
journalctl -u marketmind-daily.service -n 100 --no-pager
journalctl -u marketmind-backup.service -n 100 --no-pager
```

## Optional operations dashboard

The dashboard reads `agent_runs` and selected logs through a separate
SELECT-only database role. Create that role after the main service applies
its migrations:

```bash
docker compose -f docker-compose.prod.yml exec postgres psql -U marketmind -d marketmind
```

```sql
CREATE ROLE marketmind_ro LOGIN;
\password marketmind_ro
GRANT CONNECT ON DATABASE marketmind TO marketmind_ro;
GRANT USAGE ON SCHEMA public TO marketmind_ro;
GRANT SELECT ON agent_runs TO marketmind_ro;
\q
```

Write the read-only password into `secrets/dashboard_pgpass` in the format
above and an independent browser password into `secrets/dashboard_token`,
both owner-only. Start the optional profile:

```bash
docker compose -f docker-compose.prod.yml --profile dashboard up -d dashboard
```

The dashboard is published on loopback only. Reach it using
`ssh -N -L 8001:127.0.0.1:8001 <deploy-user>@<host>` or on an authorized
tailnet with `tailscale serve --bg http://127.0.0.1:8001`. Inspect with
`tailscale serve status`; stop sharing with `tailscale serve off`. Do not
publish port 8001 on a LAN or public interface, and do not use
`tailscale funnel`. The dashboard token is the HTTP Basic password. The
service gets neither the write-role password nor source credentials.

## Updating and rollback

For an update, download and verify the next release bundle, inspect
`release.env`, extract it, update the image references in `.env`, and run
Compose `config --quiet`, `pull`, and `up -d`. Reinstall the systemd unit
files and restart the optional dashboard if enabled. Keep the prior archive
and image digests for rollback. Migrations may be incompatible with an older
image; for such a rollback, stop services and timers, restore the matching
backup set, then start the prior release. Choose the database dump, files,
and image as a set before overwriting live data.
