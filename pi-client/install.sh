#!/usr/bin/env bash
# Установка клиента на Raspberry Pi OS Lite (Bookworm, 64-bit).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"

echo "== Пакеты"
sudo apt-get update
sudo apt-get install -y python3-venv python3-dev mpv libportaudio2 ffmpeg \
  pipewire pipewire-pulse wireplumber libspa-0.2-bluetooth pulseaudio-utils bluez

echo "== Python-окружение"
python3 -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --upgrade pip
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"
"$DIR/.venv/bin/python" -c "import openwakeword.utils as u; u.download_models()"

[ -f "$DIR/config.toml" ] || cp "$DIR/config.toml.example" "$DIR/config.toml"

echo "== Автозапуск (пользовательский сервис — так ему доступен PipeWire)"
mkdir -p ~/.config/systemd/user
sed "s|__DIR__|$DIR|g" "$DIR/kids-speaker.service" > ~/.config/systemd/user/kids-speaker.service
sudo loginctl enable-linger "$USER"   # пользовательские сервисы стартуют без входа в систему
systemctl --user daemon-reload
systemctl --user enable pipewire pipewire-pulse wireplumber kids-speaker

cat <<EOF

Готово. Дальше:
  1. Подключи Станцию по Bluetooth (см. README-pi.md) и впиши её MAC в config.toml
  2. Впиши адрес сервера в config.toml
  3. Проверка вручную:   $DIR/.venv/bin/python $DIR/client.py
  4. Запуск сервиса:     systemctl --user start kids-speaker
     Логи:               journalctl --user -u kids-speaker -f
EOF
