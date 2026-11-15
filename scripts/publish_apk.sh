#!/usr/bin/env bash
# Собрать подписанное приложение для телефона и выложить на сервер: /admin → «Приложение для телефона».
# Запускать на компьютере разработчика (Git Bash на Windows), где стоят JDK 17 и Android SDK.
# Ключ подписи: ~/.kids-speaker/keystore.jks + keystore.properties (создаётся один раз, см. README) —
# без того же ключа обновление не встанет поверх установленного приложения.
#   KS_SSH=titan@5.253.188.127 KS_DIR=workspace/kids-speaker scripts/publish_apk.sh
set -euo pipefail
cd "$(dirname "$0")/../android-client"
KS_SSH="${KS_SSH:-titan@5.253.188.127}"
KS_DIR="${KS_DIR:-workspace/kids-speaker}"
KEYS="${HOME}/.kids-speaker"
[ -f "$KEYS/keystore.properties" ] || { echo "нет $KEYS/keystore.properties — см. README «Клиент на Android»"; exit 1; }
cp "$KEYS/keystore.properties" keystore.properties
CODE=$(git rev-list --count HEAD)
NAME="0.$CODE-$(git rev-parse --short HEAD)"
./gradlew --console=plain -q testDebugUnitTest assembleRelease -PversionCode="$CODE" -PversionName="$NAME"
APK=app/build/outputs/apk/release/app-release.apk
printf '{"version": "%s", "code": %s}\n' "$NAME" "$CODE" > app/build/version.json
ssh "$KS_SSH" "mkdir -p $KS_DIR/data/app"
scp -q "$APK" "$KS_SSH:$KS_DIR/data/app/kids-speaker.apk.tmp"
scp -q app/build/version.json "$KS_SSH:$KS_DIR/data/app/version.json"
ssh "$KS_SSH" "mv $KS_DIR/data/app/kids-speaker.apk.tmp $KS_DIR/data/app/kids-speaker.apk"
# копия ключа подписи — в бэкап сервера (data/app попадает в архив)
scp -q "$KEYS/keystore.jks" "$KEYS/keystore.properties" "$KS_SSH:$KS_DIR/data/app/"
echo "выложено: $NAME ($(du -h "$APK" | cut -f1))"
