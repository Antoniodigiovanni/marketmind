# Dashboard deployment through Tailscale

This guide assumes marketmind is already running from `/srv/marketmind`. For a
fresh server, complete the base installation in `deploy/README.md` first.

## 1. Connect and verify the current deployment

```bash
ssh <deploy-user>@<server>
cd /srv/marketmind

docker compose -f docker-compose.prod.yml ps
tailscale status
```

If Tailscale is not connected, run:

```bash
sudo tailscale up
```

Follow the authentication URL it provides.

## 2. Select the dashboard image

Use the same published image as the MCP service. If updating to a new release,
download its matching host bundle with the commands in `deploy/README.md`, then
set `MARKETMIND_IMAGE` in `.env` to the digest in `release.env`. No source
checkout is needed on the server.

Confirm that the dashboard service is present:

```bash
docker compose --profile dashboard -f docker-compose.prod.yml config --services
```

The output should include `marketmind` and `dashboard`.

## 3. Create the read-only database account

Connect through the administration path for the shared PostgreSQL instance. For
the independent homelab stack, open the prompt with:

```bash
docker compose --project-directory /srv/postgres \
  -f /srv/postgres/docker-compose.yml exec postgres psql -U postgres
```

Generate a dashboard database password and save it in your password manager:

```bash
openssl rand -hex 32
```

At the PostgreSQL prompt, run:

```sql
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_roles WHERE rolname = 'marketmind_ro'
    ) THEN
        CREATE ROLE marketmind_ro LOGIN;
    END IF;
END
$$;

\password marketmind_ro

GRANT CONNECT ON DATABASE marketmind TO marketmind_ro;
GRANT USAGE ON SCHEMA public TO marketmind_ro;
GRANT SELECT ON agent_runs TO marketmind_ro;
\q
```

When `\password` prompts you, paste the dashboard database password twice. The
account deliberately receives access only to `agent_runs`.

## 4. Create the dashboard database secret

Write the read-only database credential into an owner-only file without placing
it in shell history:

```bash
cd /srv/marketmind
umask 077
read -rsp 'Read-only database password: ' database_secret; echo
read -rp 'Database hostname from MARKETMIND_DASHBOARD_DSN: ' database_host
printf '%s:5432:marketmind:marketmind_ro:%s\n' \
  "$database_host" "$database_secret" > secrets/dashboard_pgpass
unset database_host
unset database_secret
chmod 0600 secrets/dashboard_pgpass
```

Keep `MARKETMIND_DASHBOARD_DSN` in `.env` pointed at the read-only role.
Validate the Compose configuration:

```bash
docker compose -f docker-compose.prod.yml config --quiet
```

No output from `config --quiet` means the configuration is valid.

## 5. Start the dashboard

```bash
docker compose -f docker-compose.prod.yml --profile dashboard up -d dashboard
```

Check its state and recent logs:

```bash
docker compose -f docker-compose.prod.yml ps dashboard
docker compose -f docker-compose.prod.yml logs --tail=100 dashboard
```

The dashboard should be running and published on the host only at
`127.0.0.1:8001`. Do not open port 8001 in UFW, the cloud firewall, or the
Compose configuration.

## 6. Test locally on the server

The expected response is `200`:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8001/
```

## 7. Expose it through Tailscale Serve

Use the managed helper so Marketmind records and can remove only its own Serve
mapping:

```bash
marketmind dashboard tailscale enable
marketmind dashboard tailscale status
```

The enable command uses HTTPS port 443 by default. Pass another port when
needed, for example `marketmind dashboard tailscale enable 8443`. Use Tailscale
Serve, not Tailscale Funnel. Serve keeps the endpoint inside the tailnet;
Funnel would make it public.

## 8. Test from another device

On another device connected to the same tailnet, open the HTTPS URL displayed
by `marketmind dashboard tailscale status`.

Verify that:

- The overview page loads.
- Recent job runs appear.
- `/universe/` lists constituents through MCP using the same web session.
- The dashboard is served at `/`, not `/marketmind/dashboard`.

## 9. Optionally expose legacy log files

Refresh logs under `state/refresh/logs` should already be available. New
systemd job output is read with `journalctl`; root-owned log files left by an
older cron installation may appear as unreadable.

To let the dashboard read those existing files:

```bash
deploy_group="$(id -gn)"

sudo chgrp "$deploy_group" /var/log/marketmind
sudo chmod 0750 /var/log/marketmind

sudo find /var/log/marketmind \
  -maxdepth 1 -type f -name '*.log' \
  -exec chgrp "$deploy_group" {} + \
  -exec chmod 0640 {} +
```

Then edit the logrotate configuration so new files keep that group access.
This step is optional because job logs may contain detailed error messages.

## 10. Review tailnet access

Restrict the Tailscale Serve endpoint with tailnet ACLs, grants, or posture
rules so only the intended operator devices/users can reach the dashboard HTTPS
port on this server. The dashboard intentionally has no separate browser token;
Tailscale constrained access is the browser access boundary.

## Troubleshooting

- `502` from the Tailscale URL: check the dashboard container and the local
  `curl` test.
- Connection denied: check that both devices are connected to Tailscale and
  review the tailnet access policy.
- Missing legacy log files: complete optional step 9 or read job output over
  SSH.

Useful diagnostics:

```bash
tailscale status
tailscale serve status
docker compose -f docker-compose.prod.yml ps dashboard
docker compose -f docker-compose.prod.yml logs --tail=100 dashboard
```

## Rollback

Stop sharing the dashboard through Tailscale:

```bash
marketmind dashboard tailscale disable
```

Stop the dashboard container:

```bash
cd /srv/marketmind
docker compose -f docker-compose.prod.yml stop dashboard
```

This does not stop the shared PostgreSQL service, scheduled jobs, or the MCP server.
