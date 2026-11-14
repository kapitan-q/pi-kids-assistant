# Колонка-сказочница: серверная часть

```
Pi 3B (слово активации, микрофон, mpv → Bluetooth → Станция Лайт)
   │  HTTP
   ▼
gateway :8080 ──► stt    (faster-whisper, CPU)
             ├──► agent  (Claude Agent SDK) ──MCP──► tales (MCP + REST) ──► mysql
             ├──► tts    (Piper)
             └──► /media (mp3 из ./library), /speech (озвученные ответы)
```

## Запуск

```bash
cp .env.example .env        # вписать ANTHROPIC_API_KEY и PUBLIC_BASE_URL (IP сервера в локалке)
docker compose up -d --build
docker compose logs -f stt tts   # первый старт качает модели Whisper и голос Piper
curl http://localhost:8080/health
```

## Библиотека

Положи аудио в `./library` (можно по папкам: `народные/`, `пушкин/` — папка станет тегом).
Рядом можно положить одноимённые `.json` с метаданными и `.txt` с текстом:

```json
{"title": "Колобок", "author": "русская народная", "tags": "животные, короткая, на ночь",
 "age_min": 3, "age_max": 8, "description": "Колобок убегает от деда, бабки и зверей"}
```

```bash
docker compose exec tales python import_library.py
```

Повторный импорт обновляет записи, дубликатов не будет. База доступна снаружи на `localhost:3307`
(tales / пароль из .env) — удобно править через DBeaver/DataGrip.

## Проверка без Pi

```bash
# текстом
curl -s localhost:8080/text -H 'Content-Type: application/json' \
  -d '{"device_id":"test","text":"включи сказку про колобка"}' | jq

# голосом (любой wav/mp3/ogg)
curl -s localhost:8080/ask -F device_id=test -F audio=@question.wav | jq
```

Если задан `GATEWAY_TOKEN` — добавь `-H 'X-Token: ...'`.

Логи разговоров: `./data/logs/ГГГГ-ММ-ДД.jsonl` (что услышал, что ответил, какие инструменты, стоимость).
Системный промпт: `agent/system_prompt.md`, подхватывается после `docker compose restart agent`.

## MCP сказок в Claude Code / Desktop

Тот же MCP открыт на порту 8090 — можно наполнять библиотеку из Claude Code на своём ПК
(там доступен ещё инструмент `add_tale`):

```bash
claude mcp add --transport http tales http://<IP-сервера>:8090/mcp
```

## Сервер на Windows

- **Docker в WSL2**: Pi по умолчанию не видит порты WSL. На Windows 11 добавь в `%UserProfile%\.wslconfig`:
  ```
  [wsl2]
  networkingMode=mirrored
  ```
  и выполни `wsl --shutdown`. Плюс разреши входящие 8080 в брандмауэре Windows.
- **VirtualBox**: сетевой адаптер в режиме «Сетевой мост».

## Настройка скорости

- Whisper: `WHISPER_MODEL=base` быстрее, `small` точнее. `WHISPER_THREADS` = число физических ядер.
- Claude: `claude-haiku-5-5` быстрее и дешевле, `claude-sonnet-5-5` умнее.
- Ответ в `timing_ms` показывает, где тратится время.

## Режимы и родительский контроль

Всё в `config/policies.yaml` (перечитывается на лету). Правила проверяет **шлюз**, а не Claude,
поэтому уговорить колонку нарушить режим нельзя.

- **Режимы**: `normal`, `quiet` (тихий час), `homework` (уроки), `night` (ночь). Свои — добавляй в `modes`.
- **Расписание**: первое подходящее правило побеждает; окна через полночь работают.
- **Ночь**: громкость не выше 30, только сказки с тегом «на ночь», всего 60 минут сказок за ночь —
  Pi получает `limits.stop_after_sec` и сам выключает сказку по таймеру.
- **Блокирующие режимы** (`allow_children: false`): ребёнку один раз говорится `message`, дальше колонка молчит.

Родительские команды голосом — через кодовую фразу из конфига:
«Капитан Немо, тихий час на сорок минут», «…режим уроков для старшего до пяти»,
«…ночной режим», «…отбой тишины». Команда родителя важнее расписания.

Через HTTP (для будущего Telegram-бота):

```bash
curl -s localhost:8080/admin/status | jq
curl -s -X POST localhost:8080/admin/override -H 'Content-Type: application/json' \
  -d '{"mode":"homework","minutes":90,"who":["older"]}'
curl -s -X DELETE localhost:8080/admin/override
```

Проверка от лица конкретного ребёнка (пока нет узнавания голоса):

```bash
curl -s localhost:8080/text -H 'Content-Type: application/json' \
  -d '{"text":"включи колобка","speaker":"older"}' | jq
```

Пока голоса не распознаются, говорящий «неизвестен», и при `unknown_speaker_matches: true`
правило «уроки для старшего» действует на всех.

## Что дальше

- Клиент для Pi — в папке `pi-client/` (см. README-pi.md).
- Озвучка текстовых сказок заранее (Silero TTS) и импорт как аудио.
- RFID-карточки «приложил — играет» в духе Phoniebox.
