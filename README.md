# Домашняя голосовая колонка: ядро + модули

```
Pi (слово активации, микрофон, mpv → Bluetooth-колонка)
   │  HTTPS + basic-auth колонки
   ▼
Traefik ─► gateway ─┬─► stt      (faster-whisper)           ┐
                    ├─► voiceid  (кто говорит, sherpa-onnx) ┘ параллельно
                    ├─► agent    (Claude Agent SDK) ──MCP──► модули (books, …) + встроенный player
                    ├─► tts      (Piper)
                    └─► /admin   (профили, пароль родителя)
Traefik ─► /m/books/stream/… ─► модуль books (файлы для плеера)
Traefik ─► /audiobookshelf, /books ─► Audiobookshelf (каталог книг, свой вход, приложения для телефона)
```

**Ядро** (`core/`) — голос, агент, профили и родительский контроль. О конкретных источниках
(сказки, музыка, YouTube) оно не знает: всё это **модули** (`modules/`), подключённые через MCP.

```
core/gateway   точка входа для Pi: STT/TTS, узнавание, знакомство, режимы, доступы, /admin
core/agent     Claude + MCP-серверы модулей из config/modules.yaml + встроенный player
core/stt core/tts core/voiceid
modules/books  каталог сказок и аудиокниг поверх Audiobookshelf
config/        speaker.yaml (роли, режимы, расписание), modules.yaml, lang/ru.yaml (фразы ядра)
data/          состояние: core/ (профили, голоса), state/, logs/, books/, backups/ — не в git
pi-client/     клиент для Raspberry Pi (README-pi.md)
```

## Запуск

Наружу не публикуется ни один порт. Набор compose-файлов (ядро + модули + способ публикации) — в `.env`:

```bash
# titan, за Traefik:
COMPOSE_FILE=docker-compose.yml:modules/books/compose.yml:docker-compose.traefik.yml:modules/books/compose.traefik.yml
# дома, порт 8080 (задай GATEWAY_TOKEN):
COMPOSE_FILE=docker-compose.yml:modules/books/compose.yml:docker-compose.local.yml
```

```bash
cp .env.example .env        # ключ Anthropic, пароли, BASIC_AUTH_USERS, ADMIN_PASSWORD_HASH — см. комментарии
docker compose up -d --build
docker compose logs -f stt tts voiceid   # первый старт качает модели
curl -u speaker:ПАРОЛЬ https://ks.dameni.org/health   # {"ok": ядро, "core": {...}, "modules": {...}}
```

Деплой на titan — через git: репозиторий на сервере `~/workspace/kids-speaker` (ветка `master`):
`git pull` (или push в него), затем `docker compose up -d --build --remove-orphans`.

- Бэкап: `scripts/backup.sh` — cron в 04:00: база Audiobookshelf, `metadata.json` книг, `config/`,
  `data/state`, `data/core` (профили и голоса), `data/books`; хранится 14 дней в `data/backups`.
- Тесты (в контейнерах, `config/` смонтирован):
  ```bash
  docker compose run --rm --no-deps -v ./core/gateway/tests:/app/tests gateway sh -c "pip install -q pytest && python -m pytest -q tests"
  docker compose run --rm --no-deps agent sh -c "pip install -q pytest && python -m pytest -q tests"
  docker compose run --rm --no-deps books sh -c "pip install -q pytest && python -m pytest -q tests"
  ```
- Сквозная проверка через `/text`: `python3 scripts/smoke_test.py`;
  регрессия агента на реальных фразах из логов: `scripts/replay_phrases.py run|diff`.

## Модули

Модуль — отдельный контейнер (свой `modules/<имя>/compose.yml`), ядро знает о нём только из
`config/modules.yaml`:

```yaml
modules:
  books:
    title: Сказки и аудиокниги
    url: http://books:8000
    enabled: true          # false — агент не видит инструментов, /health его не проверяет
```

Контракт:

| Адрес модуля | Кто пользуется | Что |
|---|---|---|
| `{url}/mcp` | агент | MCP-инструменты; `instructions` сервера попадают в системный промпт (как пользоваться модулем) |
| `{url}/svc/mcp` | шлюз | служебные MCP-инструменты: `report_progress(item_id, position, finished)`, `item_info(item_id)` |
| `{url}/stream/…` | Pi | файлы для плеера; снаружи — `$PUBLIC_BASE_URL/m/<имя>/stream/…` |
| `{url}/health` | шлюз | `{"ok": true}`; упавший модуль не роняет `/health` ядра |

**Включить что-то** = инструмент модуля возвращает JSON с полем `player`:

```json
{"player": {"type": "play", "item_id": "…", "title": "…", "tags": ["на ночь"], "age_min": 6,
            "path": "stream/…", "start": 120}, "playing": "…"}
```

- `path` — только относительный `stream/…`; полный url собирает шлюз. `source` модуль не задаёт —
  агент берёт его из имени MCP-сервера. Модулю можно только `play`; стоп, пауза, перемотка и громкость —
  у встроенного сервера `player` ядра.
- Шлюз проверяет каждую команду по схеме (`core/gateway/actions.py`) и правилам режима/профиля
  (теги, возраст, разрешённые модули, бюджет), в том числе при «продолжай» и перемотке.
- `play_*` не должны иметь необратимых эффектов: шлюз может отказать (кроме счётчиков — можно).

**Как добавить модуль** (например, музыку):
1. `modules/music/` — FastMCP-сервер по образцу `modules/books/app.py` (`/mcp` с `instructions`,
   `/svc/mcp`, `/stream/…`, `/health`), `Dockerfile`, `compose.yml`, при публикации — `compose.traefik.yml`
   с роутером `PathPrefix(/m/music/stream/)` и своей копией basic-auth (см. books).
2. Запись в `config/modules.yaml`, файлы — в `COMPOSE_FILE`.
3. Роли в `config/speaker.yaml`: добавить модуль в `modules` тем, кому можно.

## Модуль books — Audiobookshelf

Каталог живёт в [Audiobookshelf](https://www.audiobookshelf.org): `https://ks.dameni.org/books`
(→ `/audiobookshelf`), вход — логин Audiobookshelf. Книги можно слушать и без колонки — с телефона, в машине.

- **Добавить**: «Загрузить» → файлы (одна книга = одна или несколько глав).
- **Описание обязательно**: без него книга не попадёт в колонку — по описанию Claude ищет «про лису».
- **Теги**: автор, тема, «на ночь» (только такие играют ночью), возраст — `возраст:6-12`
  (младше нижней границы колонка не включит), `скрыть` — убрать из колонки, не удаляя.
- Изменения видны через `CATALOG_REFRESH_SEC` (5 минут). Многофайловые книги склеиваются в `data/merged/`;
  место остановки хранится в Audiobookshelf у пользователя `speaker` — «продолжи» начинает с того места.
- Первый запуск: `docker compose run --rm --no-deps -v ./modules/books/scripts:/s -e ABS_ROOT_PASSWORD gateway python /s/abs_setup.py`
  — администратор, библиотека, пользователь speaker; печатает `ABS_API_KEY`/`ABS_LIBRARY_ID` для `.env`.
- Сборники по манифесту: `modules/books/scripts/build_library.py`.

## Профили, голос, админка

- **Знакомство**: человек говорит колонке «давай познакомимся» — она спрашивает имя, возраст и просит
  повторить четыре фразы (одним разговором, без слова активации). Получается профиль с ролью
  `newcomer` и голосовым отпечатком (`data/core/voices/`). Дальше колонка узнаёт его по голосу
  (сервис `voiceid`, порог `voice.threshold` в `speaker.yaml`; ниже порога — гость `guest`).
  Узнанный человек может сказать «давай познакомимся» ещё раз — голос допишется к его профилю.
- **`/admin`** (`https://ks.dameni.org/admin`, пользователь `ADMIN_USER`, пароль родителя —
  bcrypt в `ADMIN_PASSWORD_HASH`; пароль колонки не подходит): список профилей, роль, возраст,
  обращения для команд («старш»), личные ограничения (модули, запрещённые теги), **объединить**
  (перенести голос в существующий профиль — например, в «Старший», на которого ссылается расписание),
  удалить (вместе с голосом).
- **Роли** — `config/speaker.yaml`, `roles`: что можно по умолчанию (`modules`, `tags_deny`, `parent`).
  Профиль в админке может только сузить роль. Повысить роль новичка можно только в админке:
  голосом к чужому профилю голос не привязывается, иначе ребёнок мог бы назваться «Папой».
- Родитель, узнанный по голосу, обходит детские ограничения контента и даёт команды без кодовой
  фразы, но **снимать режимы голосом** не может (голос можно перепутать) — только `/control`
  с `ADMIN_TOKEN` или `parent.voice_can_relax: true`.

## Режимы и родительский контроль

`config/speaker.yaml`, перечитывается на лету. Правила проверяет **шлюз**, а не Claude, поэтому
уговорить колонку нарушить режим нельзя.

- **Режимы**: `normal`, `quiet` (тихий час), `homework` (уроки), `night` (ночь); свои — в `modes`.
  Поля: `max_volume`, `tags` (только с этими тегами), `play_budget_min`, `allow_children`, `modules`, сообщения.
- **Расписание**: первое подходящее правило побеждает; окна через полночь работают;
  `who` — id профилей и/или роли (`who: [child]`).
- **Ночь**: громкость ≤ 30, только «на ночь», 60 минут — Pi получает `limits.stop_after_sec` и гасит сам.
- **Кодовая фраза** (голосом, только ужесточать): «Капитан Немо, тихий час на сорок пять минут»,
  «…режим уроков для старшего до пяти». Не снимает блокировки и не уходит к Claude.

Управление по HTTP (для скриптов; basic-auth колонки + `X-Admin-Token`):

```bash
A=(-u speaker:ПАРОЛЬ -H "X-Admin-Token: $ADMIN_TOKEN"); URL=https://ks.dameni.org
curl -s "${A[@]}" $URL/control/status | jq
curl -s "${A[@]}" -X POST $URL/control/override -H 'Content-Type: application/json' \
  -d '{"mode":"homework","minutes":90,"who":["older"]}'
curl -s "${A[@]}" -X DELETE $URL/control/override        # обычный режим (и поверх расписания)
# от лица профиля (speaker учитывается только с admin-токеном)
curl -s "${A[@]}" $URL/text -H 'Content-Type: application/json' \
  -d '{"device_id":"test","text":"включи колобка","speaker":"older"}' | jq
```

## Настройка

- Фразы ядра — `config/lang/ru.yaml`; системный промпт ядра — `core/agent/prompt.md`,
  модуля books — `modules/books/prompt.md` (оба применяются после пересборки сервиса).
- Скорость: `WHISPER_MODEL` (`base` быстрее, `small` точнее), `WHISPER_THREADS`, `STT_CPUS`;
  `CLAUDE_MODEL`; в ответе `/ask` есть `timing_ms`.
- Логи разговоров: `data/logs/ГГГГ-ММ-ДД.jsonl`, хранятся `LOG_RETENTION_DAYS` дней.
- Вариант «дома» на Windows/WSL2: `networkingMode=mirrored` в `.wslconfig` и входящий 8080 в брандмауэре.
