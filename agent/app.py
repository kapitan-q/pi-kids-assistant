"""Агент: Claude Agent SDK + MCP-сервер сказок.

POST /chat  {"device_id": "kids-room", "text": "включи колобка"}
  -> {"reply": "Включаю сказку Колобок", "action": {"type": "play", "tale_id": 3}}
POST /reset {"device_id": "..."}  — начать разговор заново
"""
import asyncio
import contextlib
import dataclasses
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO)

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
TALES_MCP_URL = os.getenv("TALES_MCP_URL", "http://tales:8000/mcp")
IDLE_SEC = int(os.getenv("SESSION_IDLE_SEC", "600"))
TURN_TIMEOUT = float(os.getenv("AGENT_TURN_TIMEOUT", "30"))
# Глубина «обдумывания» Claude: low — быстрее (детской болтовне хватает), medium — по умолчанию у Haiku 5.5
EFFORT = os.getenv("AGENT_EFFORT", "low").strip() or None
ALLOWED_DEVICES = {d.strip() for d in os.getenv("ALLOWED_DEVICES", "").split(",") if d.strip()}
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "30"))
SYSTEM_PROMPT = Path(os.getenv("SYSTEM_PROMPT_FILE", "/app/system_prompt.md")).read_text("utf-8")

# Детскому агенту — только эти инструменты (add_tale не выдаём).
KID_TOOLS = ["search_tales", "random_tale", "list_tags", "get_tale",
             "play_tale", "stop_playback", "resume_playback", "seek_playback", "set_volume"]
ALLOWED_TOOLS = [f"mcp__tales__{t}" for t in KID_TOOLS]
# Встроенные инструменты Claude Code агенту не нужны. Если SDK умеет задавать базовый
# набор (`tools`), отдаём пустой список — тогда никакой встроенный инструмент, даже
# появившийся в новой версии CLI, агенту не достанется. Чёрный список — запасной вариант.
BUILTIN_TOOLS = ["Bash", "BashOutput", "KillShell", "KillBash", "Read", "Write", "Edit",
                 "MultiEdit", "Glob", "Grep", "LS", "WebFetch", "WebSearch", "Task", "Agent",
                 "TodoWrite", "NotebookEdit", "NotebookRead", "ExitPlanMode", "SlashCommand",
                 "Skill", "ListMcpResources", "ReadMcpResource", "ListMcpResourcesTool",
                 "ReadMcpResourceTool"]
_OPTION_FIELDS = {f.name for f in dataclasses.fields(ClaudeAgentOptions)}
# Вызов этих инструментов = команда плееру на Pi
ACTION_TOOLS = {
    "mcp__tales__play_tale": "play",
    "mcp__tales__stop_playback": "stop",
    "mcp__tales__resume_playback": "resume",
    "mcp__tales__seek_playback": "seek",
    "mcp__tales__set_volume": "volume",
}
FALLBACK_REPLY = "Ой, у меня что-то не получилось. Попробуй ещё раз."


def volume_action(tool_input: dict) -> dict:
    """Нормализуем к шкале 10..100 с шагом 10.
    {"level": 50} — выставить; {"change": -10} — сдвинуть (Pi знает текущую и сам обрежет до 10..100)."""
    if tool_input.get("level") is not None:
        level = round(int(tool_input["level"]) / 10) * 10
        return {"type": "volume", "level": max(10, min(100, level))}
    change = round(int(tool_input.get("change") or 0) / 10) * 10
    change = max(-90, min(90, change)) or (10 if (tool_input.get("change") or 0) > 0 else -10)
    return {"type": "volume", "change": change}


def make_options() -> ClaudeAgentOptions:
    opts = dict(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"tales": {"type": "http", "url": TALES_MCP_URL}},
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=BUILTIN_TOOLS,
        max_turns=6,
        cwd="/app/workdir",
    )
    if EFFORT and "effort" in _OPTION_FIELDS:
        opts["effort"] = EFFORT
    if "tools" in _OPTION_FIELDS:
        opts["tools"] = []           # пустой базовый набор встроенных инструментов
    if "permission_mode" in _OPTION_FIELDS:
        opts["permission_mode"] = "default"   # всё, что не в allowed_tools, — отказ
    if "setting_sources" in _OPTION_FIELDS:
        opts["setting_sources"] = []  # никаких settings.json/CLAUDE.md с диска
    return ClaudeAgentOptions(**opts)


class Session:
    def __init__(self) -> None:
        self.client: ClaudeSDKClient | None = None
        self.last_used = time.monotonic()
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        if self.client:
            try:
                await asyncio.wait_for(self.client.disconnect(), 10)
            except Exception:  # noqa: BLE001
                log.exception("disconnect failed")
        self.client = None

    async def ensure(self) -> ClaudeSDKClient:
        if self.client and time.monotonic() - self.last_used > IDLE_SEC:
            log.info("session idle, starting fresh")
            await self.close()
        if self.client is None:
            self.client = ClaudeSDKClient(options=make_options())
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
    log.info("tools: base=%s, allowed=%s", "tools" in _OPTION_FIELDS, ALLOWED_TOOLS)
    task = asyncio.create_task(reaper())
    yield
    task.cancel()
    for s in list(sessions.values()):
        await s.close()


app = FastAPI(title="agent", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class ChatIn(BaseModel):
    device_id: str = "default"
    text: str
    context: str | None = None  # служебная пометка от шлюза: кто говорит, режим


class ResetIn(BaseModel):
    device_id: str = "default"


def write_log(entry: dict) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"{datetime.now():%Y-%m-%d}.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def run_turn(client: ClaudeSDKClient, text: str) -> dict:
    await client.query(text)

    before_tools: list[str] = []   # текст до последнего вызова инструмента
    after_tools: list[str] = []    # текст после — это и есть финальный ответ
    pending: dict[str, dict] = {}  # tool_use_id -> action
    action: dict | None = None
    cost = None
    tools_called: list[str] = []
    result_subtype = None
    is_error = False

    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock) and block.text.strip():
                    after_tools.append(block.text.strip())
                elif isinstance(block, ToolUseBlock):
                    tools_called.append(block.name)
                    before_tools, after_tools = before_tools + after_tools, []
                    kind = ACTION_TOOLS.get(block.name)
                    if kind:
                        act = {"type": kind}
                        if kind == "volume":
                            act = volume_action(block.input)
                        elif kind == "play":
                            act["tale_id"] = block.input.get("tale_id")
                        elif kind == "seek":
                            act["seconds"] = int(block.input.get("seconds", 30))
                            act["mode"] = block.input.get("mode", "relative")
                        pending[block.id] = act
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                    act = pending.pop(block.tool_use_id)
                    if not block.is_error:  # play_tale с несуществующим id не включаем
                        action = act
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
            client = await session.ensure()
            prompt = f"[Служебно: {body.context}]\n{body.text}" if body.context else body.text
            result = await asyncio.wait_for(run_turn(client, prompt), TURN_TIMEOUT)
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
