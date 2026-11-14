"""Встроенный MCP-сервер ядра «player»: управление тем, что сейчас играет на колонке.

Работает в процессе агента (SDK MCP). Каждый инструмент возвращает {"player": {...}} —
агент забирает это как команду для Pi. Включать что-то новое умеют только модули (их play_*).
"""
import json

from claude_agent_sdk import create_sdk_mcp_server, tool

VOLUME_MIN, VOLUME_MAX, VOLUME_STEP = 10, 100, 10


def _result(action: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps({"player": action})}]}


def _error(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def volume_action(args: dict) -> dict:
    """Шкала 10..100 с шагом 10. {"level": 50} — выставить; {"change": -10} — сдвинуть
    (Pi знает текущую громкость и сам обрежет до 10..100)."""
    if args.get("level") is not None:
        level = round(int(args["level"]) / VOLUME_STEP) * VOLUME_STEP
        return {"type": "volume", "level": max(VOLUME_MIN, min(VOLUME_MAX, level))}
    raw = int(args.get("change") or 0)
    change = max(-90, min(90, round(raw / VOLUME_STEP) * VOLUME_STEP)) or (VOLUME_STEP if raw > 0 else -VOLUME_STEP)
    return {"type": "volume", "change": change}


@tool("stop", "Остановить то, что играет («стоп», «хватит», «выключи»).", {})
async def stop(_args):
    return _result({"type": "stop"})


@tool("resume", "Продолжить то, что стоит на паузе («продолжай»).", {})
async def resume(_args):
    return _result({"type": "resume"})


@tool("seek", "Перемотать то, что играет (после перемотки оно продолжит играть).\n"
      "mode=\"relative\": seconds > 0 — вперёд, < 0 — назад.\n"
      "  «перемотай дальше» ≈ +30, «сильно вперёд» ≈ +120, «чуть назад», «я не расслышал» ≈ -15.\n"
      "mode=\"absolute\": позиция от начала в секундах; «с начала» = 0.",
      {"type": "object",
       "properties": {"seconds": {"type": "integer"},
                      "mode": {"type": "string", "enum": ["relative", "absolute"]}},
       "required": ["seconds"]})
async def seek(args):
    mode = args.get("mode") or "relative"
    seconds = int(args.get("seconds", 30))
    if mode not in ("relative", "absolute"):
        return _error("mode должен быть relative или absolute")
    if mode == "absolute" and seconds < 0:
        return _error("абсолютная позиция не может быть отрицательной")
    return _result({"type": "seek", "seconds": seconds, "mode": mode})


@tool("set_volume", "Громкость колонки, шкала 10–100 с шагом 10. Передай ровно один параметр.\n"
      "change — относительно: «потише» = -10, «погромче» = +10, «намного тише» = -30.\n"
      "level — точное значение: «громкость 50», «на половину» = 50, «на максимум» = 100, "
      "«на минимум»/«совсем тихо» = 10.",
      {"type": "object",
       "properties": {"level": {"type": "integer"}, "change": {"type": "integer"}}})
async def set_volume(args):
    if (args.get("level") is None) == (args.get("change") is None):
        return _error("Нужен ровно один параметр: level или change")
    if args.get("level") is not None and not 0 <= int(args["level"]) <= 100:
        return _error("level должен быть от 10 до 100")
    return _result(volume_action(args))


SERVER_NAME = "player"
server = create_sdk_mcp_server(SERVER_NAME, tools=[stop, resume, seek, set_volume])
