# Production deployment

StrategyLab runs on one small Linux VM using Docker Compose. A 2 vCPU / 4 GB instance is
enough: Hetzner CX22, DigitalOcean 4 GB, AWS t3.medium or Lightsail, or similar.

## 1. Provision the server

1. Create an Ubuntu 24.04 VM. Allow inbound SSH (22) and HTTPS (443) only.
2. Install Docker:

   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. Make Docker start on boot. With `restart: always`, this brings the whole stack back after a reboot.

   ```bash
   sudo systemctl enable --now docker
   ```

4. Turn on automatic security updates (optional but recommended):

   ```bash
   sudo apt install unattended-upgrades
   ```

## 2. Deploy

```bash
git clone https://github.com/Tobi150407/stratlab.git /opt/strategylab
cd /opt/strategylab
cp .env.example .env
```

Edit `.env`. At minimum, set:

| Variable | Value |
|---|---|
| `POSTGRES_PASSWORD` | Output of `openssl rand -hex 24` |
| `SL_API_TOKEN` | Output of `openssl rand -hex 32` |
| `SL_DATA_PROVIDER` | `twelvedata`, plus `SL_TWELVEDATA_API_KEY`, for real prices |
| `SL_WEBHOOK_URL` | A Slack or Discord incoming webhook, and/or the SMTP settings |
| `SL_DEADMAN_URL` | A healthchecks.io ping URL (see below) |

Then start it:

```bash
docker compose up -d --build
docker compose ps        # every service should be "healthy" within about a minute
```

The `migrate` service creates the tables and default research schedules. It is safe to
run on every deploy.

## 3. HTTPS

The API binds to `127.0.0.1:8000`. Put Caddy in front of it, which handles TLS automatically:

```bash
sudo apt install caddy
echo 'strategylab.example.com {
  reverse_proxy 127.0.0.1:8000
}' | sudo tee /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Read endpoints (status and dashboard) are open. Every state-changing endpoint needs
`Authorization: Bearer $SL_API_TOKEN`. To make the dashboard private as well, add
`basicauth` to the Caddy site block.

## 4. External monitoring

These cover the one failure the app can't report itself: the whole server going down.

1. **Dead-man's switch.** Create a free check on healthchecks.io with a 2-minute period and put its ping URL in `SL_DEADMAN_URL`. The watchdog pings it every 30 seconds while the database is reachable. If the pings stop, healthchecks.io alerts you.
2. **Uptime monitor.** Point UptimeRobot or Better Stack at `https://<host>/health/ready`. It returns 503 when the database is down or a core service has stopped.
3. **Prometheus (optional).** Scrape `/metrics`.

## 5. Updating

```bash
cd /opt/strategylab && git pull
docker compose up -d --build
```

Containers are replaced one at a time. The engine resumes from its last processed candle,
so no signals are lost during a deploy. The worker gets up to 5 minutes to finish a
running job before it is stopped.

## 6. Logs

```bash
docker compose logs -f engine
docker compose logs --since 1h | grep '"level": "ERROR"'
```

Logs are JSON lines, rotated at 20 MB × 10 files per service. To centralise them, set up
a Docker logging driver such as `loki`, `awslogs` or `gcplogs`, or run Vector or Promtail
on the host.

## 7. Managed platforms

The same image runs on Render, Railway, Fly.io or ECS:

- Create one service per process, with the command set to `python -m app.cli <engine|worker|scheduler|watchdog|api>`.
- Use a managed PostgreSQL 16 and set `SL_DATABASE_URL=postgresql+psycopg://...`.
- Run `python -m app.cli init-db` as a release or pre-deploy command.
- Mark the background processes as workers, which have no HTTP port.
- Don't scale `engine` beyond 1. Extra replicas only act as standbys.
- Use the platform's own scheduled database backups instead of the `backup` container.
