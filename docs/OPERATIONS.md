# Operations runbook

## Health checks

| Check | Healthy |
|---|---|
| `GET /health/live` | 200 while the API process is up |
| `GET /health/ready` | 200 when the database is reachable and all core services are heartbeating; 503 otherwise |
| `GET /api/status` | Full JSON snapshot; this feeds the dashboard |
| `docker compose ps` | Every service shows `healthy` |

Each container's healthcheck runs `python -m app.cli healthcheck <component>`. It fails
when that component hasn't written a heartbeat for 90 seconds, and `autoheal` then restarts
the container.

## Alerts and what to do

| Alert | Meaning | Action |
|---|---|---|
| **Service outage: X** | A core process has stopped heartbeating | Usually recovers itself through restart and autoheal. If it repeats, run `docker compose logs X --since 30m`. |
| **Market data interruption / delayed** | The market is open but no fresh candles are arriving | Check the provider's status page and API quota, and `feed_status.last_error` on the dashboard. New entries are blocked automatically. Open positions keep their stops, which are checked against the candles that arrive after recovery. |
| **Strategy stopped operating** | The strategy raised an error, or wasn't evaluated even though data is live | Read `status_detail` on the dashboard. After fixing it, toggle `paper_active` off and on. |
| **Background job failed** | A job failed after all its retries | `GET /api/jobs/<id>` shows the traceback. Retry with `POST /api/jobs/<id>/retry`. |
| **Performance deterioration** | Recent paper results have fallen well below the strategy's validated baseline | Review the strategy, then deactivate or re-optimise it. |
| **Database backup failed** | `pg_dump` returned an error | Run `docker compose logs backup`. Check disk space with `df -h`. |

## Backups

- `pg_dump -Fc` runs every 6 hours (`BACKUP_INTERVAL_SECONDS`).
- Each dump is checked with `pg_restore --list` before it is kept.
- Dumps are kept for 14 days (`BACKUP_RETENTION_DAYS`) in `./backups`, with `latest.dump` linking to the newest.
- **Keep a copy off the server.** Either mount a synced folder as `BACKUP_DIR`, or add a host cron job:

  ```bash
  0 */6 * * * rclone sync /opt/strategylab/backups remote:strategylab-backups
  ```

- Test a restore at least once a quarter, on a staging copy.

## Database recovery

### Restore from a backup

```bash
cd /opt/strategylab
./deploy/backup/restore.sh backups/strategylab-YYYYMMDDTHHMMSSZ.dump
```

The script:

1. Stops the application services.
2. Takes a safety dump of the current database.
3. Restores into a new database and swaps it in.
4. Restarts everything.

The old database is kept as `strategylab_old_<ts>` until you drop it. When the engine
restarts, it fetches the candles missed since the backup was taken and processes them.
Entry signals on candles that are now stale are recorded as `rejected (stale)`, not
traded. Stops and targets on restored open positions are checked against the missed candles.

### Server lost completely

1. Provision a new server (see [DEPLOYMENT.md](DEPLOYMENT.md) steps 1 and 2) and restore `.env`.
2. Copy the latest off-server dump to `backups/`.
3. Start only the database: `docker compose up -d db`.
4. Run `./deploy/backup/restore.sh backups/<file>.dump`.

### Database container won't start

1. Run `docker compose logs db`. The usual cause is a full disk: run `df -h`, then delete old dumps or images.
2. The data lives in the `pgdata` volume, which survives `docker compose down`. Never run `docker compose down -v` in production, because `-v` deletes the volume.

## Routine tasks

Add or deactivate a strategy:

```bash
curl -X PATCH https://host/api/strategies/3 -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{"paper_active": false}'
```

Run a research job now:

```bash
curl -X POST https://host/api/jobs -H "Authorization: Bearer $T" \
  -H 'content-type: application/json' -d '{"kind":"validate","payload":{"days":120}}'
```

Load price history after adding a timeframe:

```bash
docker compose exec api python -m app.cli enqueue backfill '{"instrument":"GOLD","timeframe":"15m","days":90}'
```

Schedule a job (this example runs at 18:00 New York time on weekdays and stays correct through daylight saving changes):

```bash
curl -X POST https://host/api/schedules -H "Authorization: Bearer $T" -H 'content-type: application/json' \
  -d '{"name":"ny-close-report","kind":"compare","cron":"0 18 * * 1-5","timezone":"America/New_York"}'
```

Close a paper position by hand:

```bash
curl -X POST https://host/api/positions/<id>/close -H "Authorization: Bearer $T"
```

Ad-hoc exchange closure (for example, a national day of mourning): add the date to
`EXTRA_CLOSURES` in `app/market_hours.py` and redeploy.

## Capacity

The engine uses about 70 MB of RAM and almost no CPU. To speed up research, raise
`WORKER_CPUS`, or run more workers with `docker compose up -d --scale worker=2`. The
queue's `SKIP LOCKED` claiming means two workers never pick up the same job. Candles
take about 150 bytes per row, so a year of 5-minute bars for both instruments is roughly
30 MB.
