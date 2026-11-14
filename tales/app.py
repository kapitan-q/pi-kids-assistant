"""Сервис сказок: MCP для агента (/mcp) + REST для шлюза (/api/...), каталог — Audiobookshelf.

Инструменты для детского агента те же, что и раньше; id сказки — строковый id книги в ABS.
Пополнять библиотеку — через веб-интерфейс Audiobookshelf (ks.dameni.org/admin).
"""
import contextlib
import logging
import threading
import time

from fastapi import FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from catalog import REFRESH_SEC, catalog

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tales")

# host="0.0.0.0" отключает защиту от DNS-rebinding, которая по умолчанию
# пускает только Host: localhost — иначе agent не достучится по имени "tales".
# Поэтому порт tales НЕ публикуется наружу: MCP доступен только внутри compose-сети.
mcp = FastMCP("tales", host="0.0.0.0", stateless_http=True, json_response=True)


# ---------- инструменты для детского агента ----------

@mcp.tool()
def search_tales(query: str = "", age: int | None = None,
                 max_minutes: int | None = None, limit: int = 5) -> list[dict]:
    """Найти сказки в домашней библиотеке.
    query — ключевые слова в начальной форме: герои, название, автор, тема
    (например «колобок», «лиса заяц», «Пушкин», «про космос»). Пустой query — популярные.
    age — возраст слушателя, max_minutes — ограничение по длительности."""
    return catalog.search(query, age, max_minutes, limit)


@mcp.tool()
def play_random_tale(tag: str = "", age: int | None = None,
                     max_minutes: int | None = None) -> dict:
    """Выбрать случайную сказку и СРАЗУ включить её на колонке (одним вызовом, play_tale не нужен).
    Для «любую», «какую-нибудь», «на ночь», «другую». Можно ограничить тегом (см. list_tags),
    возрастом и длительностью. Реже слушанные выпадают чаще."""
    tale = catalog.random(tag or None, age, max_minutes)
    if not tale:
        raise ValueError("Подходящих сказок не нашлось")
    catalog.plays.inc(tale["id"])
    return {"ok": True, "tale_id": tale["id"], "playing": tale["title"],
            "description": tale["description"]}


@mcp.tool()
def list_tags() -> list[str]:
    """Все теги библиотеки (темы сказок)."""
    return catalog.list_tags()


@mcp.tool()
def get_tale(tale_id: str) -> dict:
    """Подробности о сказке по id (описание, автор, длительность)."""
    tale = catalog.get(tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    return catalog.public(tale)


@mcp.tool()
def play_tale(tale_id: str) -> dict:
    """Включить сказку на колонке. Вызывай, когда понятно, какую сказку хотят.
    Если сказку уже начинали слушать, она продолжится с того места (resume_from_min)."""
    tale = catalog.get(tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    if not catalog.media_path(tale):
        raise ValueError(f"Сказка «{tale['title']}» ещё готовится, попробуй чуть позже")
    catalog.plays.inc(tale["id"])
    result = {"ok": True, "playing": tale["title"]}
    if (start := catalog.resume_from(tale)) is not None:
        result["resume_from_min"] = round(start / 60)
    return result


@mcp.tool()
def stop_playback() -> str:
    """Остановить текущую сказку («стоп», «хватит», «выключи»)."""
    return "Остановлено"


@mcp.tool()
def resume_playback() -> str:
    """Продолжить сказку, поставленную на паузу («продолжай»)."""
    return "Продолжаю"


@mcp.tool()
def seek_playback(seconds: int = 30, mode: str = "relative") -> str:
    """Перемотать текущую сказку (после перемотки она продолжит играть).
    mode="relative": seconds > 0 — вперёд, < 0 — назад.
      «перемотай дальше» ≈ +30, «сильно вперёд» ≈ +120, «чуть назад», «я не расслышал» ≈ -15.
    mode="absolute": позиция от начала в секундах; «с начала» = 0."""
    if mode not in ("relative", "absolute"):
        raise ValueError("mode должен быть relative или absolute")
    if mode == "absolute" and seconds < 0:
        raise ValueError("абсолютная позиция не может быть отрицательной")
    return "Перематываю"


@mcp.tool()
def set_volume(level: int | None = None, change: int | None = None) -> str:
    """Громкость колонки, шкала 10–100 с шагом 10. Передай ровно один параметр.
    change — относительно: «потише» = -10, «погромче» = +10, «намного тише» = -30.
    level — точное значение: «громкость 50», «громкость на половину» = 50, «на максимум» = 100,
      «на минимум»/«совсем тихо» = 10."""
    if (level is None) == (change is None):
        raise ValueError("Нужен ровно один параметр: level или change")
    if level is not None and not 0 <= level <= 100:
        raise ValueError("level должен быть от 10 до 100")
    return "Готово"


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
    async with mcp.session_manager.run():
        yield


mcp_app = mcp.streamable_http_app()  # создаёт session_manager, endpoint /mcp
app = FastAPI(title="tales", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class ProgressIn(BaseModel):
    tale_id: str
    position: float
    finished: bool = False


@app.get("/health")
def health():
    # ok — сервис жив; пустой каталог (ABS ещё не настроен) не должен валить всю колонку
    return {"ok": True, "catalog_loaded": catalog.loaded_at > 0,
            "tales": len(catalog.tales), "skipped": len(catalog.skipped)}


@app.get("/api/tales/{tale_id}")
def api_get(tale_id: str, with_resume: bool = False):
    tale = catalog.get(tale_id)
    if not tale:
        raise HTTPException(404, "not found")
    d = catalog.public(tale)
    d["media_path"] = catalog.media_path(tale)
    if with_resume:
        d["resume_from"] = catalog.resume_from(tale)
    return d


@app.get("/api/tales")
def api_search(q: str = "", limit: int = 20):
    return catalog.search(q, limit=limit)


@app.post("/api/progress")
def api_progress(body: ProgressIn):
    if not catalog.get(body.tale_id):
        raise HTTPException(404, "not found")
    catalog.save_progress(body.tale_id, body.position, body.finished)
    return {"ok": True}


@app.post("/api/refresh")
def api_refresh():
    catalog.refresh()
    return {"tales": len(catalog.tales), "skipped": catalog.skipped}


# монтируем последним, чтобы маршруты выше имели приоритет
app.mount("/", mcp_app)
