# Колонка-сказочница: серверная часть

```
Pi 3B (слово активации, микрофон, mpv → Bluetooth → Станция Лайт)
   │  HTTPS + basic-auth
   ▼
Traefik ──► gateway :8080 ──► stt    (faster-whisper, CPU)
                         ├──► agent  (Claude Agent SDK) ──MCP──► tales (MCP + REST) ──► Audiobookshelf
                         ├──► tts    (Piper)
                         └──► /media (mp3 из ./library), /speech (озвученные ответы)
Traefik ──► Audiobookshelf (/audiobookshelf, /admin, /books) — каталог: загрузка и описания с телефона
```

## Запуск

Наружу не публикуется ни один порт. Как открыть gateway — задаётся в `.env` через `COMPOSE_FILE`:

- `docker-compose.yml:docker-compose.traefik.yml` — сервер titan, `https://ks.dameni.org` за Traefik
  с basic-auth на весь хост (закрыты и API, и `/media`, `/speech`);
- `docker-compose.yml:docker-compose.local.yml` — дома, порт 8080 в локальной сети (тогда задай `GATEWAY_TOKEN`).

```bash
cp .env.example .env        # ключ, пароли, BASIC_AUTH_USERS, ADMIN_TOKEN — см. комментарии в файле
docker compose up -d --build
docker compose logs -f stt tts   # первый старт качает модели Whisper и голос Piper
curl -u speaker:ПАРОЛЬ https://ks.dameni.org/health
```

### Деплой на titan

```bash
# с ПК (из папки проекта)
tar czf - --exclude=.git --exclude=.env --exclude=pi-client/.venv --exclude=pi-client/config.toml . \
  | ssh titan 'tar xzf - -C ~/workspace/kids-speaker'
# на titan
cd ~/workspace/kids-speaker && docker compose up -d --build
```

- Первый запуск Audiobookshelf: `docker compose run --rm --no-deps -v ./modules/books/scripts:/s -e ABS_ROOT_PASSWORD gateway python /s/abs_setup.py`
  — создаст администратора, библиотеку «Сказки», пользователя speaker и напечатает `ABS_API_KEY`/`ABS_LIBRARY_ID` для `.env`.
- Бэкап: `scripts/backup.sh` — cron ежедневно в 04:00: база Audiobookshelf, описания сказок и режимы в `data/backups`, 14 дней.
- Тесты шлюза: `docker compose run --rm --no-deps gateway sh -c "pip install -q pytest && pytest -q"`.

## Библиотека — Audiobookshelf

Каталог сказок живёт в [Audiobookshelf](https://www.audiobookshelf.org): `https://ks.dameni.org/admin`
(или `/books`, `/audiobookshelf`), вход — логин Audiobookshelf. Там же приложения для телефона.

- **Добавить сказку**: «Загрузить» → файлы (одна сказка = одна книга; можно несколькими файлами-главами).
- **Описание обязательно**: без него сказка не попадёт в каталог колонки — по описанию Claude находит
  сказку по героям и теме («про лису», «про принцессу»).
- **Теги**: автор, тема, «на ночь» (только такие играют в ночном режиме), «короткая»/«длинная»,
  возраст — тегом `возраст:6-12`. Тег `скрыть` — убрать сказку из колонки, не удаляя.
- Колонка видит изменения в течение `CATALOG_REFRESH_SEC` (5 минут).
- Книги из нескольких файлов колонка склеивает в один (`data/merged/`), так работают перемотка и
  **продолжение с того места**: позицию прослушивания Pi сообщает раз в полминуту и при остановке,
  она хранится в Audiobookshelf у пользователя `speaker`.

На диске: `library/<сказка>/<файлы>.mp3` + `metadata.json` (так же их видит Audiobookshelf).
Большие сборники с заготовленными описаниями удобнее разложить `modules/books/scripts/build_library.py`
по манифесту — части склеиваются в один mp3, рядом кладётся `metadata.json`.

## Проверка без Pi

```bash
AUTH=(-u speaker:ПАРОЛЬ); URL=https://ks.dameni.org
# текстом
curl -s "${AUTH[@]}" $URL/text -H 'Content-Type: application/json' \
  -d '{"device_id":"test","text":"включи сказку про колобка"}' | jq

# голосом (любой wav/mp3/ogg)
curl -s "${AUTH[@]}" $URL/ask -F device_id=test -F audio=@question.wav | jq
```

`device_id` должен быть в `ALLOWED_DEVICES`. Если задан `GATEWAY_TOKEN` — добавь `-H 'X-Token: ...'`.

Логи разговоров: `./data/logs/ГГГГ-ММ-ДД.jsonl` (что услышал, что ответил, какие инструменты, стоимость),
хранятся `LOG_RETENTION_DAYS` дней.
Системный промпт: `agent/system_prompt.md`, подхватывается после `docker compose restart agent`.

## MCP сказок

MCP сервиса `tales` (`/mcp`) доступен только внутри compose-сети. Агенту выдаётся 9 инструментов:
поиск, случайная сказка, теги, подробности и команды плееру — без правки каталога.
Ключ Audiobookshelf у `tales` — пользователя `speaker`: только чтение и прогресс прослушивания.

## Сервер на Windows (вариант «дома»)

- `COMPOSE_FILE=docker-compose.yml:docker-compose.local.yml`, задай `GATEWAY_TOKEN`.
- **Docker в WSL2**: Pi по умолчанию не видит порты WSL. На Windows 11 добавь в `%UserProfile%\.wslconfig`:
  ```
  [wsl2]
  networkingMode=mirrored
  ```
  и выполни `wsl --shutdown`. Плюс разреши входящие 8080 в брандмауэре Windows.
- **VirtualBox**: сетевой адаптер в режиме «Сетевой мост».

## Настройка скорости

- Whisper: `WHISPER_MODEL=base` быстрее, `small` точнее. `WHISPER_THREADS` = число физических ядер,
  `STT_CPUS` — сколько ядер сервера отдать контейнеру.
- Claude: `claude-haiku-5-5` быстрее и дешевле, `claude-sonnet-5-5` умнее. `AGENT_TURN_TIMEOUT` — сколько ждать ответа.
- Ответ в `timing_ms` показывает, где тратится время.

## Режимы и родительский контроль

Всё в `config/policies.yaml` (перечитывается на лету). Правила проверяет **шлюз**, а не Claude,
поэтому уговорить колонку нарушить режим нельзя — в том числе «продолжай» и перемотку
сказки, которая не подходит под текущий режим.

- **Режимы**: `normal`, `quiet` (тихий час), `homework` (уроки), `night` (ночь). Свои — добавляй в `modes`.
- **Расписание**: первое подходящее правило побеждает; окна через полночь работают.
- **Ночь**: громкость не выше 30, только сказки с тегом «на ночь», 60 минут от первой сказки за ночь —
  Pi получает `limits.stop_after_sec` и сам выключает сказку по таймеру.
- **Блокирующие режимы** (`allow_children: false`): ребёнку один раз говорится `message`, дальше колонка молчит.

Родительские команды голосом — через кодовую фразу из конфига:
«Капитан Немо, тихий час на сорок пять минут», «…режим уроков для старшего до пяти», «…ночной режим».
Голосом можно только **ужесточать**: фразу легко подслушать. Она не снимает блокировки и не уходит к Claude.
Снять режим голосом («…отбой тишины») можно, только если `parent.voice_can_relax: true`, иначе — через админку.
Снятие возвращает обычный режим и поверх расписания (ночь, уроки) — до конца текущего окна.

Админка по HTTP (basic-auth + `X-Admin-Token`; пустой `ADMIN_TOKEN` — админка выключена):

```bash
A=(-u speaker:ПАРОЛЬ -H "X-Admin-Token: $ADMIN_TOKEN"); URL=https://ks.dameni.org
curl -s "${A[@]}" $URL/control/status | jq
curl -s "${A[@]}" -X POST $URL/control/override -H 'Content-Type: application/json' \
  -d '{"mode":"homework","minutes":90,"who":["older"]}'
curl -s "${A[@]}" -X DELETE $URL/control/override        # обычный режим (и поверх расписания)
```

Проверка от лица конкретного ребёнка (пока нет узнавания голоса; `speaker` учитывается только с admin-токеном):

```bash
curl -s "${A[@]}" $URL/text -H 'Content-Type: application/json' \
  -d '{"device_id":"test","text":"включи колобка","speaker":"older"}' | jq
```

Пока голоса не распознаются, говорящий «неизвестен», и при `unknown_speaker_matches: true`
правило «уроки для старшего» действует на всех.

## Что дальше

- Клиент для Pi — в папке `pi-client/` (см. README-pi.md).
- Telegram-бот поверх `/control/*` вместо голосового снятия режимов.
- Озвучка текстовых сказок заранее (Silero TTS) и импорт как аудио.
- RFID-карточки «приложил — играет» в духе Phoniebox.
