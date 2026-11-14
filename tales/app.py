"""Сервис сказок: MCP-сервер (/mcp) + небольшой REST для шлюза (/api/...)."""
import contextlib

from fastapi import FastAPI, HTTPException
from mcp.server.fastmcp import FastMCP

import db

# host="0.0.0.0" отключает защиту от DNS-rebinding, которая по умолчанию
# пускает только Host: localhost — иначе agent не достучится по имени "tales".
mcp = FastMCP("tales", host="0.0.0.0", stateless_http=True, json_response=True)


# ---------- инструменты для детского агента ----------

@mcp.tool()
def search_tales(query: str = "", age: int | None = None,
                 max_minutes: int | None = None, limit: int = 5) -> list[dict]:
    """Найти сказки в домашней библиотеке.
    query — ключевые слова в начальной форме: герои, название, автор, тема
    (например «колобок», «лиса заяц», «Пушкин», «про космос»). Пустой query — популярные.
    age — возраст слушателя, max_minutes — ограничение по длительности."""
    return db.search(query, age, max_minutes, limit)


@mcp.tool()
def random_tale(tag: str = "", age: int | None = None,
                max_minutes: int | None = None) -> dict:
    """Случайная сказка с аудио; можно ограничить тегом (см. list_tags), возрастом и длительностью.
    Реже слушанные выпадают чаще."""
    tale = db.random_tale(tag or None, age, max_minutes)
    if not tale:
        raise ValueError("Подходящих сказок не нашлось")
    return tale


@mcp.tool()
def list_tags() -> list[str]:
    """Все теги библиотеки (темы сказок)."""
    return db.list_tags()


@mcp.tool()
def get_tale(tale_id: int) -> dict:
    """Подробности о сказке по id (описание, автор, длительность)."""
    tale = db.get(tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    return tale


@mcp.tool()
def play_tale(tale_id: int) -> dict:
    """Включить сказку на колонке. Вызывай, когда понятно, какую сказку хотят."""
    tale = db.get(tale_id)
    if not tale:
        raise ValueError(f"Сказки с id={tale_id} нет")
    if not tale["has_audio"]:
        raise ValueError(f"У сказки «{tale['title']}» пока нет аудио")
    db.mark_played(tale_id)
    return {"ok": True, "playing": tale["title"]}


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


# ---------- инструмент для взрослых (Claude Code / Desktop), детскому агенту не выдаётся ----------

@mcp.tool()
def add_tale(title: str, author: str = "", description: str = "", tags: str = "",
             age_min: int | None = None, age_max: int | None = None,
             file: str | None = None, text: str | None = None) -> dict:
    """Добавить или обновить сказку. file — путь к аудио относительно папки library
    (например «народные/kolobok.mp3»). tags — через запятую."""
    tale_id = db.upsert(title, author, description, tags, age_min, age_max, None, file, text)
    return {"ok": True, "id": tale_id}


# ---------- FastAPI-обёртка ----------

@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    async with mcp.session_manager.run():
        yield


mcp_app = mcp.streamable_http_app()  # создаёт session_manager, endpoint /mcp
app = FastAPI(title="tales", lifespan=lifespan)


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/api/tales/{tale_id}")
def api_get(tale_id: int):
    tale = db.get(tale_id)
    if not tale:
        raise HTTPException(404, "not found")
    return tale


@app.get("/api/tales")
def api_search(q: str = "", limit: int = 20):
    return db.search(q, limit=limit)


# монтируем последним, чтобы маршруты выше имели приоритет
app.mount("/", mcp_app)
