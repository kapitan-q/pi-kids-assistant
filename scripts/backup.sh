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
# люди, роли, режимы (SQLite): целостный снимок через backup API — сам файл может быть посреди записи
docker compose exec -T gateway python -c "
import sqlite3; s = sqlite3.connect('/data/core/speaker.db'); d = sqlite3.connect('/data/core/speaker.backup.db')
s.backup(d); d.close(); s.close()" || echo "$(date -Is) WARN: снимок speaker.db не сделан"
# data/app — ключ подписи приложения для телефона (сам APK не нужен: пересобирается)
tar czf "$OUT/state-$TS.tgz" --exclude='speaker.db' --exclude='speaker.db-*' --exclude='*.apk' \
  config data/state $(ls -d data/books data/core data/app 2>/dev/null)
find "$OUT" \( -name 'abs-config-*.tgz' -o -name 'library-meta-*.tgz' -o -name 'state-*.tgz' \) -mtime +"$KEEP_DAYS" -delete
echo "$(date -Is) ok $(du -h "$OUT/abs-config-$TS.tgz" | cut -f1)"
