"""Агент: Claude Agent SDK + MCP-серверы модулей из config/modules.yaml + встроенный player.

POST /chat  {"device_id": "kids-room", "text": "включи колобка", "context": "..."}
  -> {"reply": "Включаю сказку Колобок",
      "action": {"type": "play", "source": "books", "item_id": "...", "path": "...", ...} | null}
POST /reset {"device_id": "..."}  — начать разговор заново

Команды плееру не зашиты в агенте: любой инструмент может вернуть {"player": {...}}.
Модулю можно только включать (play) — source берётся из имени его MCP-сервера, а не из ответа;
stop/resume/seek/volume — только у встроенного сервера player. Окончательно проверяет шлюз.
"""
import asyncio
import contextlib
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import yaml
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

import player

log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO)

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
IDLE_SEC = int(os.getenv("SESSION_IDLE_SEC", "600"))
TURN_TIMEOUT = float(os.getenv("AGENT_TURN_TIMEOUT", "30"))
MAX_TURNS = int(os.getenv("AGENT_MAX_TURNS", "6"))
# Глубина «обдумывания» Claude (low|medium|high). Пусто — по умолчанию модели. low на Haiku 5.5
# заметно не ускоряет короткие ответы, но иногда пропускает шаги — поэтому не включаем.
EFFORT = os.getenv("AGENT_EFFORT", "").strip() or None
ALLOWED_DEVICES = {d.strip() for d in os.getenv("ALLOWED_DEVICES", "").split(",") if d.strip()}
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
WORKDIR = os.getenv("AGENT_WORKDIR", "/app/workdir")
CONFIG_DIR = Path(os.getenv("CONFIG_DIR", "/config"))
SYSTEM_PROMPT = Path(os.getenv("SYSTEM_PROMPT_FILE", Path(__file__).with_name("prompt.md"))).read_text("utf-8")
LANG = yaml.safe_load((CONFIG_DIR / "lang" / f"{os.getenv('LANG_CODE', 'ru')}.yaml").read_text("utf-8"))
FALLBACK_REPLY = LANG["fallback"]

PLAYER_TYPES = {"stop", "resume", "seek", "volume"}   # только от встроенного player
MODULE_TYPES = {"play"}                               # модулям — только включать


def load_modules() -> dict[str, dict]:
    """Включённые модули: имя -> настройки. Читается при создании каждой сессии."""
    data = yaml.safe_load((CONFIG_DIR / "modules.yaml").read_text("utf-8")) or {}
    return {name: m for name, m in (data.get("modules") or {}).items() if m.get("enabled", True)}


def module_headers(name: str, rights: dict | None) -> dict[str, str]:
    """Права говорящего для модуля — заголовками MCP-запросов (модуль по ним фильтрует; ASCII-JSON)."""
    if rights is None:
        return {}
    h = {"X-KS-Rights": json.dumps((rights.get("modules") or {}).get(name) or {})}
    if rights.get("age") is not None:
        h["X-KS-Age"] = str(int(rights["age"]))
    return h


def make_options(modules: dict[str, dict], rights: dict | None = None) -> ClaudeAgentOptions:
    """rights — от шлюза: {"modules": {имя: права модуля}, "age"}. Модули не из списка не подключаются:
    у говорящего нет к ним доступа (или режим их не разрешает). None — подключить все (без шлюза, тесты)."""
    if rights is not None:
        modules = {n: m for n, m in modules.items() if n in (rights.get("modules") or {})}
    servers: dict = {}
    for name, m in modules.items():
        servers[name] = {"type": "http", "url": m["url"].rstrip("/") + "/mcp"}
        if h := module_headers(name, rights):
            servers[name]["headers"] = h
    servers[player.SERVER_NAME] = player.server
    opts = dict(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers=servers,
        allowed_tools=[f"mcp__{name}" for name in servers],   # все инструменты этих серверов
        tools=[],                    # никаких встроенных инструментов Claude Code
        permission_mode="default",   # всё, что не в allowed_tools, — отказ
        setting_sources=[],          # никаких settings.json/CLAUDE.md с диска
        max_turns=MAX_TURNS,
        cwd=WORKDIR,
    )
    if EFFORT:
        opts["effort"] = EFFORT
    return ClaudeAgentOptions(**opts)


def tool_server(tool_name: str) -> str | None:
    """mcp__books__play_tale -> books"""
    parts = tool_name.split("__")
    return parts[1] if len(parts) >= 3 and parts[0] == "mcp" else None


def player_from_result(server: str | None, content) -> dict | None:
    """Команда плееру из результата инструмента: JSON-объект с полем player в одном из текстовых блоков."""
    blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
    for b in blocks:
        if not isinstance(b, dict) or b.get("type") != "text":
            continue
        try:
            data = json.loads(b.get("text") or "")
        except (ValueError, TypeError):
            continue
        act = data.get("player") if isinstance(data, dict) else None
        if not isinstance(act, dict):
            continue
        kind = act.get("type")
        if server == player.SERVER_NAME and kind in PLAYER_TYPES:
            return {k: v for k, v in act.items() if k != "source"}
        if server and server != player.SERVER_NAME and kind in MODULE_TYPES:
            return {**act, "source": server}          # source — только из имени сервера
        log.warning("отброшена команда %r от %s", kind, server)
    return None


class Session:
    def __init__(self) -> None:
        self.client: ClaudeSDKClient | None = None
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()
        self.stale = False   # модуль был недоступен при старте сессии — пересоздать после хода
        self.rights_key: str | None = None   # права, с которыми подключены модули

    async def close(self) -> None:
        if self.client:
            try:
                await asyncio.wait_for(self.client.disconnect(), 10)
            except Exception:  # noqa: BLE001
                log.exception("disconnect failed")
        self.client = None
        self.stale = False

    async def ensure(self, rights: dict | None = None) -> ClaudeSDKClient:
        """Права другие (сменился говорящий, режим закрыл модуль) — новый разговор с другими модулями:
        заодно ребёнок не продолжает чужой диалог."""
        key = json.dumps(rights, sort_keys=True, ensure_ascii=False)
        if self.client and (self.stale or time.monotonic() - self.last_used > IDLE_SEC or key != self.rights_key):
            log.info("session stale/idle/rights changed, starting fresh")
            await self.close()
        if self.client is None:
            self.client = ClaudeSDKClient(options=make_options(load_modules(), rights))
            self.rights_key = key
            await asyncio.wait_for(self.client.connect(), TURN_TIMEOUT)
        self.last_used = time.monotonic()
        return self.client


sessions: dict[str, Session] = {}


async def reaper() -> None:
    """Закрываем простаивающие сессии (каждая держит процесс Claude Code CLI)
    и чистим старые логи разговоров."""
    while True:
        await asyncio.sleep(60)
        try:
            now = time.monotonic()
            for device, s in list(sessions.items()):
                if now - s.last_used > IDLE_SEC and not s.lock.locked():
                    log.info("closing idle session %s", device)
                    sessions.pop(device, None)
                    await s.close()
            prune_logs()
        except Exception:  # noqa: BLE001
            log.exception("reaper failed")


def prune_logs() -> None:
    cutoff = time.time() - LOG_RETENTION_DAYS * 86400
    for f in LOG_DIR.glob("*.jsonl"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("modules: %s", list(load_modules()))
    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    for s in list(sessions.values()):
        await s.close()


app = FastAPI(title="agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class ChatIn(BaseModel):
    device_id: str = "default"
    text: str
    context: str | None = None  # служебная пометка от шлюза: кто говорит, режим, доступы
    rights: dict | None = None  # {"modules": {имя: права модуля}, "age"} — какие модули подключить и с чем


class ResetIn(BaseModel):
    device_id: str = "default"


def write_log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_turn(session: Session, text: str) -> dict:
    client = session.client
    await client.query(text)

    before_tools: list[str] = []   # текст до последнего вызова инструмента
    after_tools: list[str] = []    # текст после — это и есть финальный ответ
    calls: dict[str, str | None] = {}   # tool_use_id -> сервер
    action: dict | None = None
    cost = None
    tools_called: list[str] = []
    result_subtype = None
    is_error = False

    async for msg in client.receive_response():
        if isinstance(msg, SystemMessage) and msg.subtype == "init":
            down = [s.get("name") for s in msg.data.get("mcp_servers") or [] if s.get("status") != "connected"]
            if down:
                log.warning("MCP-серверы недоступны: %s — сессия будет пересоздана", down)
                session.stale = True
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    after_tools.append(block.text.strip())
                elif isinstance(block, ToolUseBlock):
                    tools_called.append(block.name)
                    before_tools, after_tools = before_tools + after_tools, []
                    calls[block.id] = tool_server(block.name)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in calls and not block.is_error:
                    act = player_from_result(calls[block.tool_use_id], block.content)
                    if act:
                        action = act   # последняя успешная команда
        elif isinstance(msg, ResultMessage):
            cost = msg.total_cost_usd
            result_subtype = getattr(msg, "subtype", None)
            is_error = bool(getattr(msg, "is_error", False))

    reply = " ".join(after_tools) or " ".join(before_tools[-1:])
    if is_error:
        # ошибки API/CLI («Invalid API key», лимиты) приходят обычным текстом —
        # ребёнку их зачитывать нельзя
        log.error("turn error (result=%s): %s", result_subtype, reply[:300])
        return {"reply": FALLBACK_REPLY, "action": action, "cost_usd": cost,
                "tools": tools_called, "error": result_subtype or "is_error"}
    if not reply and not action:
        # error_max_turns и т.п.: лучше сказать хоть что-то, чем молчать
        log.warning("empty reply (result=%s)", result_subtype)
        reply = FALLBACK_REPLY
    return {"reply": reply, "action": action, "cost_usd": cost, "tools": tools_called}


@app.post("/chat")
async def chat(body: ChatIn):
    if ALLOWED_DEVICES and body.device_id not in ALLOWED_DEVICES:
        raise HTTPException(403, "unknown device")
    session = sessions.setdefault(body.device_id, Session())
    t0 = time.monotonic()
    async with session.lock:
        try:
            await session.ensure(body.rights)
            prompt = f"[Служебно: {body.context}]\n{body.text}" if body.context else body.text
            result = await asyncio.wait_for(run_turn(session, prompt), TURN_TIMEOUT)
        except Exception as e:  # noqa: BLE001  (в т.ч. TimeoutError)
            log.exception("agent turn failed: %r", e)
            await session.close()
            result = {"reply": FALLBACK_REPLY, "action": None, "cost_usd": None, "tools": [],
                      "error": type(e).__name__}
        session.last_used = time.monotonic()
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write_log({"ts": datetime.now().isoformat(timespec="seconds"),
               "device": body.device_id, "heard": body.text, **result})
    return result


@app.post("/reset")
async def reset(body: ResetIn):
    session = sessions.pop(body.device_id, None)
    if session:
        async with session.lock:
            await session.close()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "sessions": len(sessions)}
