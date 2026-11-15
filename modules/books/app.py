"""Модуль books: каталог сказок и аудиокниг поверх Audiobookshelf.

Контракт модуля колонки (см. README, «Как добавить модуль»):
  /mcp       — MCP-инструменты для агента; instructions сервера = prompt.md (как ими пользоваться)
  /svc/mcp   — служебные MCP-инструменты для ядра (агенту не подключаются)
  /stream/id — аудиофайл (Range); снаружи — $PUBLIC_BASE_URL/m/books/stream/<id>
  /health
Инструмент, который включает книгу, возвращает {"player": {"type": "play", "item_id", "title",
"tags", "age_min", "path", "start"?}, ...}: url собирает ядро, оно же проверяет родительские правила.
"""
import contextlib
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import anyio
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from mcp.server.fastmcp import Context, FastMCP

from catalog import REFRESH_SEC, catalog, tag_list

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("books")

PROMPT = Path(os.getenv("MODULE_PROMPT_FILE", Path(__file__).with_name("prompt.md"))).read_text("utf-8")

# host="0.0.0.0" отключает защиту от DNS-rebinding (по умолчанию пускает только Host: localhost) —
# иначе агент не достучится по имени сервиса. Поэтому MCP наружу не публикуется: только compose-сеть.
mcp = FastMCP("books", instructions=PROMPT, host="0.0.0.0", stateless_http=True, json_response=True)
svc = FastMCP("books-svc", host="0.0.0.0", stateless_http=True, json_response=True)


def _run(fn, *args, **kw):
    """Каталог ходит в ABS синхронно — не блокируем event loop MCP."""
    return anyio.to_thread.run_sync(lambda: fn(*args, **kw))


# ---------- права слушателя ----------
# Модуль сам права не выдаёт: ядро вычисляет их по ролям человека и передаёт агенту, агент — сюда
# заголовками MCP-запроса. X-KS-Rights: {"books.tags": {"allow": [...] | null, "deny": [...]}},
# X-KS-Age: возраст (нет — не фильтровать). Нет заголовков — без фильтра (ядро всё равно проверит).

TAGS_RIGHT = "books.tags"


def listener(ctx: Context | None) -> dict:
    """{"allow", "deny", "age"} из заголовков запроса."""
    req = getattr(getattr(ctx, "request_context", None), "request", None) if ctx else None
    headers = getattr(req, "headers", None) or {}
    out = {"allow": None, "deny": None, "age": None}
    try:
        tags = (json.loads(headers.get("x-ks-rights") or "{}") or {}).get(TAGS_RIGHT) or {}
        out["allow"], out["deny"] = tags.get("allow"), tags.get("deny")
        if headers.get("x-ks-age"):
            out["age"] = int(headers["x-ks-age"])
    except (ValueError, AttributeError, TypeError):
        log.warning("непонятные права в заголовках: %r", dict(headers))
    return out


def _narrow_age(asked: int | None, allowed: int | None) -> int | None:
    """Возраст от Claude может только сузить то, что передало ядро."""
    if allowed is None:
        return asked
    return allowed if asked is None else min(asked, allowed)


def _playable(tale_id: str, lim: dict | None = None) -> dict:
    tale = catalog.get(tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    if lim and not catalog.permitted(tale, lim["allow"], lim["deny"], lim["age"]):
        raise ValueError(f"Сказку «{tale['title']}» этому слушателю включать нельзя — предложи другую")
    if not catalog.audio_file(tale):
        raise ValueError(f"Сказка «{tale['title']}» ещё готовится, попробуй чуть позже")
    return tale


def _play_result(tale: dict) -> dict:
    catalog.plays.inc(tale["id"])
    player = catalog.player(tale)
    result = {"player": player, "playing": tale["title"]}
    if player.get("start"):
        result["resume_from_min"] = round(player["start"] / 60)
    return result


# ---------- инструменты для агента ----------

@mcp.tool()
async def search_tales(query: str = "", age: int | None = None,
                       max_minutes: int | None = None, limit: int = 5, ctx: Context = None) -> list[dict]:
    """Найти сказки в домашней библиотеке.
    query — ключевые слова в начальной форме: герои, название, автор, тема
    (например «колобок», «лиса заяц», «Пушкин», «про космос»). Пустой query — популярные.
    age — возраст слушателя, max_minutes — ограничение по длительности."""
    lim = listener(ctx)
    return await _run(catalog.search, query, _narrow_age(age, lim["age"]), max_minutes, limit,
                      allow=lim["allow"], deny=lim["deny"])


@mcp.tool()
async def play_random_tale(tag: str = "", age: int | None = None,
                           max_minutes: int | None = None, ctx: Context = None) -> dict:
    """Выбрать случайную сказку и СРАЗУ включить её на колонке (одним вызовом, play_tale не нужен).
    Для «любую», «какую-нибудь», «на ночь», «другую». Можно ограничить тегом (см. list_tags),
    возрастом и длительностью. Реже слушанные выпадают чаще."""
    lim = listener(ctx)
    tale = await _run(catalog.random, tag or None, _narrow_age(age, lim["age"]), max_minutes,
                      allow=lim["allow"], deny=lim["deny"])
    if not tale:
        raise ValueError("Подходящих сказок не нашлось")
    return await _run(_play_result, tale)


@mcp.tool()
async def list_tags() -> list[str]:
    """Все теги библиотеки (темы сказок)."""
    return await _run(catalog.list_tags)


@mcp.tool()
async def get_tale(tale_id: str) -> dict:
    """Подробности о сказке по id (описание, автор, длительность)."""
    tale = await _run(catalog.get, tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    return catalog.public(tale)


@mcp.tool()
async def play_tale(tale_id: str, ctx: Context = None) -> dict:
    """Включить сказку на колонке. Вызывай, когда понятно, какую сказку хотят.
    Если сказку уже начинали слушать, она продолжится с того места (resume_from_min)."""
    tale = await _run(_playable, tale_id, listener(ctx))
    return await _run(_play_result, tale)


# ---------- служебные инструменты для ядра ----------

@svc.tool()
async def rights() -> dict:
    """Права, которые объявляет модуль (ядро показывает их в ролях /admin и передаёт обратно)."""
    return {"rights": [{
        "id": TAGS_RIGHT, "kind": "values", "applies_to": "tags", "title": "Теги книг",
        "description": "Разрешены — только книги с этими тегами; запрещены — книги с ними не включать.",
        "hint": await _run(catalog.list_tags),
    }]}


@svc.tool()
async def report_progress(item_id: str, position: float, finished: bool = False) -> dict:
    """Позиция прослушивания (для «продолжить с того места»)."""
    if not await _run(catalog.get, item_id):
        raise ValueError(f"нет книги {item_id}")
    await _run(catalog.save_progress, item_id, position, finished)
    return {"ok": True}


@svc.tool()
async def item_info(item_id: str) -> dict:
    """Что за книга: для проверки родительских правил при «продолжай» и перемотке."""
    tale = await _run(catalog.get, item_id)
    if not tale:
        raise ValueError(f"нет книги {item_id}")
    return {"item_id": tale["id"], "title": tale["title"], "tags": tag_list(tale["tags"]),
            "age_min": tale["age_min"]}


# ---------- FastAPI-обёртка ----------

def _refresh_loop() -> None:
    """Каталог обновляется фоном: новые книги из Audiobookshelf появляются за REFRESH_SEC."""
    while True:
        try:
            catalog.refresh()
        except Exception:  # noqa: BLE001
            log.exception("обновление каталога не удалось")
        time.sleep(max(60, REFRESH_SEC))


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    threading.Thread(target=_refresh_loop, daemon=True).start()
    async with mcp.session_manager.run(), svc.session_manager.run():
        yield


mcp_app = mcp.streamable_http_app()   # создаёт session_manager, endpoint /mcp
svc_app = svc.streamable_http_app()
app = FastAPI(title="books", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/health")
def health():
    # ok — сервис жив; пустой каталог (ABS ещё не настроен) не должен валить колонку
    return {"ok": True, "catalog_loaded": catalog.loaded_at > 0,
            "items": len(catalog.tales), "skipped": len(catalog.skipped)}


@app.api_route("/stream/{item_id}", methods=["GET", "HEAD"])
def stream(item_id: str):
    if not re.fullmatch(r"[\w-]{1,64}", item_id):
        raise HTTPException(404)
    tale = catalog.get(item_id)
    path = catalog.audio_file(tale) if tale else None
    if not path or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/refresh")
def refresh():
    catalog.refresh()
    return {"items": len(catalog.tales), "skipped": catalog.skipped}


# монтируем последними, чтобы маршруты выше имели приоритет
app.mount("/svc", svc_app)
app.mount("/", mcp_app)
