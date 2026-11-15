#!/usr/bin/env bash
# Restore the StrategyLab database from a backup dump.
#   ./deploy/backup/restore.sh backups/strategylab-20261115T000000Z.dump
# Stops the trading/research services, restores, restarts. Paper positions resume from
# the restored state; candles missed since the backup are re-fetched automatically.
set -euo pipefail
DUMP="${1:?usage: restore.sh <dumpfile>}"
[ -f "$DUMP" ] || { echo "no such file: $DUMP"; exit 1; }
set -a; . ./.env; set +a
DB="${POSTGRES_DB:-strategylab}"; USER="${POSTGRES_USER:-strategylab}"

echo ">> stopping application services"
docker compose stop engine worker scheduler watchdog api

echo ">> safety copy of current database"
docker compose exec -T db pg_dump -U "$USER" -Fc "$DB" > "backups/pre-restore-$(date -u +%Y%m%dT%H%M%SZ).dump"

echo ">> restoring $DUMP"
docker compose exec -T db psql -U "$USER" -d postgres -c "DROP DATABASE IF EXISTS ${DB}_restore;" -c "CREATE DATABASE ${DB}_restore;"
docker compose exec -T db pg_restore -U "$USER" -d "${DB}_restore" --no-owner < "$DUMP"
docker compose exec -T db psql -U "$USER" -d postgres \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='${DB}';" \
  -c "ALTER DATABASE ${DB} RENAME TO ${DB}_old_$(date -u +%s);" \
  -c "ALTER DATABASE ${DB}_restore RENAME TO ${DB};"

echo ">> starting services"
docker compose up -d
echo ">> done. Old database kept as ${DB}_old_*; drop it once you've verified the restore."
