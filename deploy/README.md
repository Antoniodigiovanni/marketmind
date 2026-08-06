# marketmind deployment

One-command deployment for a hardened Linux host (systemd). The harness and
marketmind run on the same box; marketmind exposes **no network listener** —
the harness spawns the MCP server over stdio, and all jobs run as an isolated
system user via hardened systemd units.

## Requirements

- Linux with systemd (Ubuntu/Debian recommended).
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
sudo bash /srv/marketmind/deploy/update.sh
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
- **One-off override**: `sudo BRANCH=main bash /srv/marketmind/deploy/update.sh`.
- **Auto-update (opt-in)**: set `ENABLE_AUTO_UPDATE=1` in `deploy.conf` and run
  `update.sh` once; a weekly Monday 05:30 timer then applies updates
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
5. Installs `.env` — from `ENV_FILE` if given, otherwise from `.env.example`
   (rerun after adding real secrets: `AI_API_KEY`, `FRED_API_KEY`).
6. Initialises `data/db/research.duckdb` from `schema.sql`.
7. Installs hardened systemd units and enables timers.

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
| `marketmind-update.timer` | Mondays 05:30 (opt-in) | `deploy/update.sh` self-update |

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
  bash -c 'cd /srv/marketmind && exec /usr/local/bin/uv run python -m server.mcp'
```

No ports are opened; the server speaks MCP stdio on the spawned process's
stdin/stdout.

## Hardening notes

- All services run as `marketmind` with `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, `RestrictSUIDSGID`, and `UMask=0077`.
- Only `data/` and the uv cache are writable; everything else is read-only.
- `.env` is `0600`, owned by `marketmind`.
- Backups live outside the install tree at `/srv/marketmind-backups/`; move
  them off-box (e.g. `rclone`/`restic`) for real disaster recovery.

## Operations

```bash
journalctl -u marketmind-daily.service -e      # job logs
systemctl list-timers | grep marketmind        # next runs
sudo -u marketmind env HOME=/srv/marketmind bash -c \
  'cd /srv/marketmind && /usr/local/bin/uv run python -m data.db.init'  # manual db check
```

## Rollback

Backups are plain tarballs of `data/`:

```bash
sudo tar -xzf /srv/marketmind-backups/marketmind-YYYY-MM-DD-HHMM.tar.gz -C /srv/marketmind
sudo chown -R marketmind:marketmind /srv/marketmind/data
```

Code rollback: `sudo BRANCH=<previous-tag> bash deploy/install.sh`.
