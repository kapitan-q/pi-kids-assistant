# Домашняя голосовая колонка: ядро + модули

```
Pi (слово активации, микрофон, mpv → Bluetooth-колонка)
   │  HTTPS + basic-auth колонки
   ▼
Traefik ─► gateway ─┬─► stt      (faster-whisper)           ┐
                    ├─► voiceid  (кто говорит, sherpa-onnx) ┘ параллельно
                    ├─► agent    (Claude Agent SDK) ──MCP──► модули (books, …) + встроенный player
                    ├─► tts      (Piper)
                    └─► /admin   (люди, роли, права; пароль родителя)
Traefik ─► /m/books/stream/… ─► модуль books (файлы для плеера)
Traefik ─► /audiobookshelf, /books ─► Audiobookshelf (каталог книг, свой вход, приложения для телефона)
```

**Ядро** (`core/`) — голос, агент, люди, права и родительский контроль. О конкретных источниках
(сказки, музыка, YouTube) оно не знает: всё это **модули** (`modules/`), подключённые через MCP.

```
core/gateway   точка входа для Pi: STT/TTS, узнавание, знакомство, права, режимы, /admin
core/agent     Claude + MCP-серверы модулей из config/modules.yaml + встроенный player
core/stt core/tts core/voiceid
modules/books  каталог сказок и аудиокниг поверх Audiobookshelf
config/        speaker.yaml (режимы, расписание, голос), modules.yaml, lang/ru.yaml (фразы ядра)
data/          состояние: core/ (speaker.db — люди, роли, режимы, журнал; голоса), logs/, books/, backups/ — не в git
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
  `data/core` (снимок `speaker.db` и голоса), `data/books`; хранится 14 дней в `data/backups`.
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
- Шлюз проверяет каждую команду по схеме (`core/gateway/actions.py`) и правилам режима и правам говорящего
  (теги, возраст, разрешённые модули, бюджет), в том числе при «продолжай» и перемотке.
- `play_*` не должны иметь необратимых эффектов: шлюз может отказать (кроме счётчиков — можно).

**Как добавить модуль** (например, музыку):
1. `modules/music/` — FastMCP-сервер по образцу `modules/books/app.py` (`/mcp` с `instructions`,
   `/svc/mcp`, `/stream/…`, `/health`), `Dockerfile`, `compose.yml`, при публикации — `compose.traefik.yml`
   с роутером `PathPrefix(/m/music/stream/)` и своей копией basic-auth (см. books).
2. Запись в `config/modules.yaml`, файлы — в `COMPOSE_FILE`.
3. Права модуля: доступ к нему ядро заводит само; свои (`<модуль>.<имя>`) модуль объявляет служебным
   инструментом `rights()` и читает из заголовков `X-KS-Rights`/`X-KS-Age` (см. `listener` в books).
4. В `/admin` → «Роли»: разрешить новый модуль тем ролям, кому можно.

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

## Люди, роли, права

Словарь:

| Понятие | Что это | Где |
|---|---|---|
| **Право** | Константа «что можно». Ядро: `core.control` (ставить и снимать ограничения), `core.modes_exempt` (режимы на него не действуют), `core.any_age` (без возрастных ограничений). Модуль: доступ к нему (`books`) и свои права (`books.tags` — теги книг) | объявляют ядро и модули |
| **Роль** | Набор прав: какие разрешены, какие запрещены. Право со значениями (`books.tags`) — в «разрешено» только эти значения, в «запрещено» — эти нельзя | `/admin` → «Роли» |
| **Человек** | Имя, дата рождения (возраст считается сам), обращения, роли | `/admin` → «Люди» |
| **Обращение** | Как человека называют в голосовой команде: «старший», «машенька» (начало слова) | у человека |
| **Гость** | Встроенная роль: её права у неузнанного голоса и у человека без ролей. Управлять ограничениями гостю нельзя | `/admin` |
| **Режим** | Когда и что ограничено: тихий час, уроки, ночь | `config/speaker.yaml` |
| **`who`** | Для кого правило расписания или команда: id людей и/или ролей | `speaker.yaml`, команды |

- **Итоговые права** человека: разрешено то, что разрешила хоть одна его роль, минус всё, что запретила любая.
  Ядро считает их само и передаёт модулю его часть (агент подключает модуль с заголовками
  `X-KS-Rights`, `X-KS-Age`); модуль по ним фильтрует поиск. Модуль права людям не выдаёт, а ядро
  ещё раз проверяет всё при включении. Новое право = константа в модуле (`rights()`) и галка в роли —
  код ядра не меняется.
- **Знакомство**: человек говорит колонке «давай познакомимся» — она спрашивает имя, возраст и просит
  повторить четыре фразы (одним разговором, без слова активации). Появляется человек **без ролей**
  (права гостя) и голосовой отпечаток (`data/core/voices/`). Дальше колонка узнаёт его по голосу
  (сервис `voiceid`, порог `voice.threshold` в `speaker.yaml`; ниже порога — гость).
  Узнанный может сказать «давай познакомимся» ещё раз — голос допишется к нему же, но второго
  человека под другим именем не завести («ай-ай-ай»). Роли назначает только родитель в `/admin`:
  иначе ребёнок мог бы назваться «Папой».
- **Гостевой доступ** (переключатель в `/admin`): включён — неузнанный голос пользуется правами
  роли «Гость» (удобно, пока колонку пробуют); выключен — ему отвечают «я тебя не узнала» (команды
  плееру и знакомство работают). Короткие фразы узнаются хуже длинных — порог подбирается по логам.
- **`/admin`** (`https://ks.dameni.org/admin`, пользователь `ADMIN_USER`, пароль родителя — bcrypt в
  `ADMIN_PASSWORD_HASH`; пароль колонки не подходит): «Сейчас» (включить и снять режим, для кого),
  «Роли», «Люди» (роли, дата рождения, обращения, **объединить** — перенести голос из нового знакомства
  в существующего человека, удалить вместе с голосом), знакомства и гостевой доступ, журнал.
- Данные `/admin` — в `data/core/speaker.db` (SQLite). **Руками не править** — только через `/admin`.
  Прежние `profiles.json`/`roles.json`/`state.json` импортируются при первом старте (→ `*.migrated`).
  Человека или роль, на которых ссылается расписание, удалить или объединить нельзя — сначала правится
  `speaker.yaml`.

| Что меняется | Где |
|---|---|
| Люди, роли, права, включить/снять режим, знакомства, гостевой доступ | `/admin` |
| Режимы, расписание, кодовая фраза, порог узнавания голоса | `config/speaker.yaml` (на лету) |
| Модули | `config/modules.yaml` + `COMPOSE_FILE` |
| Фразы колонки, промпты | `config/lang/ru.yaml`, `core/agent/prompt.md`, `modules/*/prompt.md` |
| Ключи, пароли, токены | `.env` |

## Режимы и родительский контроль

`config/speaker.yaml`, перечитывается на лету. Правила проверяет **шлюз**, а не Claude, поэтому
уговорить колонку нарушить режим нельзя.

- **Режимы**: `normal`, `quiet` (тихий час), `homework` (уроки), `night` (ночь); свои — в `modes`
  (включаются из `/admin` и по HTTP; голосом — только эти четыре).
  Поля: `max_volume`, `tags` (только с этими тегами), `play_budget_min`, `allow_children` (`false` —
  тем, на кого действуют режимы, не отвечать), `modules`, сообщения.
- **Расписание**: первое подходящее правило побеждает; окна через полночь работают;
  `who` — id людей и/или ролей из `/admin` (`who: [child]`). На неподтверждённого говорящего
  (`unknown_speaker_matches`) действуют все ограничения с `who`, но не послабления.
- **На кого действуют**: на всех, кроме тех, у кого есть право `core.modes_exempt`. Лимиты Pi
  (`GET /policy?device_id=…`) считаются для того, кто включил то, что играет: сказку папы ночью Pi
  не выключит, а «уроки для старшего» не глушат младшего.
- **Ночь**: громкость ≤ 30, только «на ночь», 60 минут — Pi получает `limits.stop_after_sec` и гасит сам.
- **Голосом** — только фраза целиком: «тихий час на сорок пять минут», «уроки для старшего до пяти»,
  «отбой режимов». С правом `core.control` (узнан по голосу) — без кодовой фразы, ставить и снимать.
  Кодовая фраза (`parent.passphrase`) — если голос не узнан: только ужесточить. «Сними с паузы»,
  «замолчи» — команды плееру, а не режимы. Всё пишется в журнал `/admin`.

Управление по HTTP (для скриптов; basic-auth колонки + `X-Admin-Token`):

```bash
A=(-u speaker:ПАРОЛЬ -H "X-Admin-Token: $ADMIN_TOKEN"); URL=https://ks.dameni.org
curl -s "${A[@]}" $URL/control/status | jq
curl -s "${A[@]}" -X POST $URL/control/override -H 'Content-Type: application/json' \
  -d '{"mode":"homework","minutes":90,"who":["older"]}'
curl -s "${A[@]}" -X DELETE $URL/control/override        # обычный режим (и поверх расписания)
# от лица человека (speaker учитывается только с admin-токеном)
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
