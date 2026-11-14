# Клиент для Raspberry Pi 3B

Что делает: слушает слово активации → пишет фразу до паузы → отправляет на сервер →
проигрывает ответ → управляет сказкой (играть / пауза / стоп / перемотка / громкость).
Сам выключает сказку ночью по таймеру, раз в минуту сверяет правила с сервером,
переподключает Bluetooth, если Станция отвалилась.

## 1. Система

- **Raspberry Pi OS Lite 64-bit (Bookworm)** — 64 бит обязательно: под 32 бит нет onnxruntime.
- Pi лучше подключить к роутеру кабелем: на Pi 3 Wi-Fi и Bluetooth делят антенну, звук может заикаться.
- USB-микрофон (или ReSpeaker 2-Mic HAT).

```bash
git clone … && cd kids-speaker/pi-client    # или скопируй папку pi-client на Pi
./install.sh
```

## 2. Bluetooth со Станцией Лайт

```bash
bluetoothctl
  power on
  agent on
  default-agent
  scan on
# скажи Станции: «Алиса, включи Bluetooth» — она появится в списке
  pair  XX:XX:XX:XX:XX:XX
  trust XX:XX:XX:XX:XX:XX
  connect XX:XX:XX:XX:XX:XX
  quit

pactl list short sinks     # должен появиться bluez_output....
pw-play /usr/share/sounds/alsa/Front_Center.wav   # если файла нет — любой wav
```

Впиши MAC в `config.toml` → `[bluetooth] speaker_mac` — клиент будет переподключаться сам.
Проверь, принимает ли Станция переподключение, когда она не в режиме Bluetooth
(после перезагрузки Pi, после выключения Станции). Если нет — придётся раз сказать
«Алиса, включи Bluetooth».

Громкость на самой Станции выставь кнопками ~70–80%, дальше управляет Pi.

## 3. Микрофон

```bash
.venv/bin/python -m sounddevice          # список устройств
arecord -d 3 -f S16_LE -r 16000 t.wav && pw-play t.wav
```

Если нужен не микрофон по умолчанию — впиши номер или часть имени в `[audio] mic`.

### Временно: старый Android-телефон как микрофон

1. Поставь на телефон **IP Webcam** (Google Play), в настройках включи аудио,
   видео можно выключить или поставить минимальное разрешение.
2. Нажми «Запустить сервер», запомни адрес вида `http://192.168.1.60:8080`.
3. В роутере закрепи за телефоном постоянный IP.
4. В `config.toml`: `stream_url = "http://192.168.1.60:8080/audio.wav"`.
5. Проверка на Pi: `ffmpeg -t 5 -i http://192.168.1.60:8080/audio.wav t.wav && pw-play t.wav`.

Телефон держи на зарядке, в настройках батареи убери IP Webcam из оптимизации,
иначе Android усыпит его ночью. Задержка по Wi-Fi — доли секунды, на работу не влияет.

## 4. Запуск

```bash
nano config.toml                          # адрес сервера, логин/пароль, MAC
.venv/bin/python client.py                # вручную, видно лог
systemctl --user start kids-speaker       # как сервис
journalctl --user -u kids-speaker -f
```

Скажи «Hey Jarvis», дождись сигнала, скажи «включи сказку про колобка».
Сигналы: высокий — слушаю, короткий — услышал, низкий двойной — сервер недоступен.

## Знакомство

Каждый член семьи один раз говорит: «Hey Jarvis» → «давай познакомимся». Колонка спросит имя,
возраст и попросит повторить несколько фраз — всё одним разговором, без слова активации
(колонка сама ждёт ответа до `expect_reply_sec`). После этого она узнаёт человека по голосу.
Новый профиль появляется в `https://<сервер>/admin` с ролью «Новичок» — там родитель назначает
роль и ограничения или объединяет его с уже заведённым профилем («Старший»).

## Слово активации

Готовые модели openWakeWord — английские (`hey_jarvis`, `alexa`, `hey_mycroft`).
Для начала хватит, потом можно обучить свою модель на русское слово (у openWakeWord есть
ноутбук для обучения) и указать путь к `.onnx` в `[wakeword] model`.
Порог `threshold`: ложные срабатывания — выше, не слышит издалека — ниже.

## Если что-то не так

- `pip install` падает на `tflite-runtime` → `pip install --no-deps openwakeword` и затем
  `pip install onnxruntime tqdm scipy scikit-learn requests`.
- Нет звука из сервиса, а вручную есть → проверь `loginctl show-user $USER | grep Linger` (должно быть yes).
- Звук заикается → кабель вместо Wi-Fi, либо USB-донгл Bluetooth.
- Колонка срабатывает на сказку → повысь `threshold` или отодвинь микрофон от Станции.
- Обрезаются первые слоги ответа → Bluetooth-синк засыпает в паузах. Отключи suspend:
  ```bash
  mkdir -p ~/.config/wireplumber/wireplumber.conf.d
  cat > ~/.config/wireplumber/wireplumber.conf.d/51-no-suspend.conf <<'CONF'
  monitor.bluez.rules = [
    { matches = [ { node.name = "~bluez_output.*" } ]
      actions = { update-props = { session.suspend-timeout-seconds = 0 } } }
  ]
  CONF
  systemctl --user restart wireplumber
  ```
- `401` в логе → неверные `user`/`password` в `[server]` (это basic-auth сервера на titan).
  Проверка: `curl -u speaker:ПАРОЛЬ https://ks.dameni.org/health`.
