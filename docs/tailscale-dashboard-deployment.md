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
download its matching host bundle with the commands in `deploy/README.md`,
then set `MARKETMIND_IMAGE` in `.env` to the digest in `release.env`. No source
checkout is needed on the server.

Confirm that the dashboard service is present:

```bash
docker compose --profile dashboard -f docker-compose.prod.yml config --services
```

The output should include:

```text
marketmind
dashboard
```

## 3. Generate two secrets

Generate the browser-authentication secret:

```bash
openssl rand -hex 32
```

Save it in your password manager as the dashboard token.

Generate a different secret for the read-only PostgreSQL account:

```bash
openssl rand -hex 32
```

Save it as the dashboard database password. Do not reuse the main PostgreSQL
password.

## 4. Create the read-only database account

Connect through the administration path for the shared PostgreSQL instance.
For the optional managed database, use the combined Compose command documented
in `deploy/README.md`. At the PostgreSQL prompt, run:

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

When `\password` prompts you, paste the dashboard database password twice.
The account deliberately receives access only to `agent_runs`.

## 5. Create the dashboard secret files

Write the two credentials into owner-only files without placing them in shell
history:

```bash
cd /srv/marketmind
umask 077
read -rsp 'Dashboard token: ' browser_secret; echo
printf '%s\n' "$browser_secret" > secrets/dashboard_token
unset browser_secret
read -rsp 'Read-only database password: ' database_secret; echo
read -rp 'Database hostname from MARKETMIND_DASHBOARD_DSN: ' database_host
printf '%s:5432:marketmind:marketmind_ro:%s\n' \
  "$database_host" "$database_secret" > secrets/dashboard_pgpass
unset database_host
unset database_secret
chmod 0600 secrets/dashboard_token secrets/dashboard_pgpass
```

Keep `MARKETMIND_DASHBOARD_DSN` in `.env` pointed at the read-only role.
Validate the Compose configuration:

```bash
docker compose -f docker-compose.prod.yml config --quiet
```

No output from `config --quiet` means the configuration is valid.

## 6. Start the dashboard

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

## 7. Test locally on the server

Without credentials, the expected response is `401`:

```bash
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://127.0.0.1:8001/
```

Test with the dashboard token without placing it in shell history:

```bash
read -rsp "Dashboard token: " dashboard_token
echo
curl -sS -o /dev/null -w '%{http_code}\n' \
  --user "ops:${dashboard_token}" \
  http://127.0.0.1:8001/
unset dashboard_token
```

The expected response is `200`.

## 8. Expose it through Tailscale Serve

Run on the production server:

```bash
tailscale serve --bg http://127.0.0.1:8001
```

The first invocation may provide an administrator link for enabling tailnet
HTTPS. Complete that step if requested.

Inspect the active configuration:

```bash
tailscale serve status
```

It should resemble:

```text
https://marketmind-server.example-tailnet.ts.net
|-- / proxy http://127.0.0.1:8001
```

Use Tailscale Serve, not Tailscale Funnel. Serve keeps the endpoint inside the
tailnet; Funnel would make it public.

## 9. Test from another device

On another device connected to the same tailnet, open the HTTPS URL displayed
by `tailscale serve status`.

When the browser requests credentials, use:

- Username: any value, such as `ops`.
- Password: `MARKETMIND_DASHBOARD_TOKEN`.

Verify that:

- The overview page loads.
- Recent job runs appear.
- Incorrect credentials are rejected.
- The dashboard is served at `/`, not `/marketmind/dashboard`.

## 10. Optionally expose legacy log files

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

Then edit the logrotate configuration:

```bash
sudo nano /etc/logrotate.d/marketmind
```

Change:

```text
create 0600 root root
```

to the following, replacing `<deploy-group>` with the output of `id -gn`:

```text
create 0640 root <deploy-group>
```

Restart the dashboard:

```bash
docker compose -f docker-compose.prod.yml restart dashboard
```

This step is optional because job logs may contain detailed error messages.

## 11. Review tailnet access

If this is a personal tailnet containing only your devices, the existing
access policy may be sufficient. If other people belong to the tailnet,
restrict TCP port 443 on this server to your user or an operator group.

Keep the dashboard password enabled even with restrictive tailnet rules.

## Troubleshooting

- `401`: credentials are absent or the dashboard token is incorrect.
- `502` from the Tailscale URL: check the dashboard container and the local
  `curl` tests in steps 6 and 7.
- Connection denied: check that both devices are connected to Tailscale and
  review the tailnet access policy.
- Missing legacy log files: complete optional step 10 or read job output
  over SSH.

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
tailscale serve off
```

Stop the dashboard container:

```bash
cd /srv/marketmind
docker compose -f docker-compose.prod.yml stop dashboard
```

This does not stop the shared PostgreSQL service, scheduled jobs, or the MCP server.
