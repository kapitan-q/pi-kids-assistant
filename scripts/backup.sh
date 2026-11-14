#!/usr/bin/env bash
# Ежедневный бэкап: база Audiobookshelf (каталог, пользователи, прогресс) + режимы колонки.
# Сами аудиофайлы не бэкапятся (большие; оригиналы у владельца).
# cron: 0 4 * * * /home/titan/workspace/kids-speaker/scripts/backup.sh >> /home/titan/workspace/kids-speaker/data/backups/backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")/.."
KEEP_DAYS="${KEEP_DAYS:-14}"
OUT=data/backups
mkdir -p "$OUT"
TS=$(date +%Y%m%d-%H%M)
docker compose exec -T audiobookshelf tar czf - -C / config > "$OUT/abs-config-$TS.tgz"
# описания сказок (metadata.json) лежат рядом с аудио — их тоже стоит сохранить
find library -name metadata.json -print0 | tar czf "$OUT/library-meta-$TS.tgz" --null -T -
tar czf "$OUT/state-$TS.tgz" config data/state $(ls -d data/books data/core 2>/dev/null)
find "$OUT" \( -name 'abs-config-*.tgz' -o -name 'library-meta-*.tgz' -o -name 'state-*.tgz' -o -name 'tales-*.sql.gz' \) -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) ok $(du -h "$OUT/abs-config-$TS.tgz" | cut -f1)"
