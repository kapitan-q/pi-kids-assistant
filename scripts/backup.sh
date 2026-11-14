#!/usr/bin/env bash
# Ежедневный бэкап: дамп MySQL + состояние режимов. Хранится KEEP_DAYS дней в data/backups.
# cron: 0 4 * * * /home/titan/workspace/kids-speaker/scripts/backup.sh >> /home/titan/workspace/kids-speaker/data/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP_DAYS="${KEEP_DAYS:-14}"
OUT=data/backups
mkdir -p "$OUT"
TS=$(date +%Y%m%d-%H%M)
docker compose exec -T mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot --single-transaction --routines tales' \
  | gzip > "$OUT/tales-$TS.sql.gz"
tar czf "$OUT/state-$TS.tgz" config data/state
find "$OUT" -name 'tales-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
find "$OUT" -name 'state-*.tgz' -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) ok $(du -h "$OUT/tales-$TS.sql.gz" | cut -f1)"
