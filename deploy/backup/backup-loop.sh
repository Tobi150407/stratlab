#!/bin/sh
# Periodic logical backups of the StrategyLab database.
# Writes compressed custom-format dumps to /backups, verifies each one, prunes old ones.
set -eu
INTERVAL="${BACKUP_INTERVAL_SECONDS:-21600}"   # default every 6 hours
RETENTION="${BACKUP_RETENTION_DAYS:-14}"

while true; do
  ts=$(date -u +%Y%m%dT%H%M%SZ)
  out="/backups/strategylab-$ts.dump"
  echo "{\"component\":\"backup\",\"msg\":\"starting backup $out\"}"
  if pg_dump -Fc -Z 6 -f "$out.partial" && pg_restore --list "$out.partial" >/dev/null; then
    mv "$out.partial" "$out"
    ln -sf "$(basename "$out")" /backups/latest.dump
    echo "{\"component\":\"backup\",\"msg\":\"backup ok\",\"file\":\"$out\",\"bytes\":$(wc -c < "$out")}"
    # heartbeat row so the dashboard shows backup freshness
    psql -q -c "INSERT INTO heartbeats(component,status,detail,started_at,last_seen_at)
                VALUES('backup','ok','{\"last_file\":\"$(basename "$out")\"}',now(),now())
                ON CONFLICT (component) DO UPDATE SET status='ok', detail=EXCLUDED.detail, last_seen_at=now();" || true
  else
    rm -f "$out.partial"
    echo "{\"component\":\"backup\",\"level\":\"ERROR\",\"msg\":\"backup FAILED\"}"
    psql -q -c "INSERT INTO notifications(kind,severity,title,body,data,delivery_status,delivery_attempts,read,created_at)
                VALUES('job_failed','critical','Database backup failed','pg_dump returned an error; see backup container logs','{}','pending',0,false,now());" || true
  fi
  find /backups -name 'strategylab-*.dump' -mtime +"$RETENTION" -delete
  # optional off-site copy: mount a synced folder (rclone/s3fs) at BACKUP_DIR, or add a cron on the host
  sleep "$INTERVAL"
done
