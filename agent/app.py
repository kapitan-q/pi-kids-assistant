"""Агент: Claude Agent SDK + MCP-сервер сказок.

POST /chat  {"device_id": "kids-room", "text": "включи колобка"}
  -> {"reply": "Включаю сказку Колобок", "action": {"type": "play", "tale_id": 3}}
POST /reset {"device_id": "..."}  — начать разговор заново
"""
import asyncio
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
from fastapi import FastAPI
from pydantic import BaseModel

log = logging.getLogger("agent")
logging.basicConfig(level=logging.INFO)

MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")
TALES_MCP_URL = os.getenv("TALES_MCP_URL", "http://tales:8000/mcp")
IDLE_SEC = int(os.getenv("SESSION_IDLE_SEC", "600"))
LOG_DIR = Path(os.getenv("LOG_DIR", "/data/logs"))
SYSTEM_PROMPT = Path(os.getenv("SYSTEM_PROMPT_FILE", "/app/system_prompt.md")).read_text("utf-8")

# Детскому агенту — только эти инструменты (add_tale не выдаём).
KID_TOOLS = ["search_tales", "random_tale", "list_tags", "get_tale",
             "play_tale", "stop_playback", "resume_playback", "seek_playback", "set_volume"]
ALLOWED_TOOLS = [f"mcp__tales__{t}" for t in KID_TOOLS]
# Встроенные инструменты Claude Code агенту не нужны.
BUILTIN_TOOLS = ["Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "MultiEdit",
                 "Glob", "Grep", "WebFetch", "WebSearch", "Task", "TodoWrite",
                 "NotebookEdit", "ExitPlanMode", "SlashCommand", "Skill"]
# Вызов этих инструментов = команда плееру на Pi
ACTION_TOOLS = {
    "mcp__tales__play_tale": "play",
    "mcp__tales__stop_playback": "stop",
    "mcp__tales__resume_playback": "resume",
    "mcp__tales__seek_playback": "seek",
    "mcp__tales__set_volume": "volume",
}


def volume_action(tool_input: dict) -> dict:
    """Нормализуем к шкале 10..100 с шагом 10.
    {"level": 50} — выставить; {"change": -10} — сдвинуть (Pi знает текущую и сам обрежет до 10..100)."""
    if tool_input.get("level") is not None:
        level = round(int(tool_input["level"]) / 10) * 10
        return {"type": "volume", "level": max(10, min(100, level))}
    change = round(int(tool_input.get("change") or 0) / 10) * 10
    change = max(-90, min(90, change)) or (10 if (tool_input.get("change") or 0) > 0 else -10)
    return {"type": "volume", "change": change}

FALLBACK_REPLY = "Ой, у меня что-то не получилось. Попробуй ещё раз."


def make_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        model=MODEL,
        system_prompt=SYSTEM_PROMPT,
        mcp_servers={"tales": {"type": "http", "url": TALES_MCP_URL}},
        allowed_tools=ALLOWED_TOOLS,
        disallowed_tools=BUILTIN_TOOLS,
        max_turns=6,
        cwd="/app/workdir",
    )


class Session:
    def __init__(self) -> None:
        self.client: ClaudeSDKClient | None = None
        self.last_used = 0.0
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:  # noqa: BLE001
                log.exception("disconnect failed")
        self.client = None

    async def ensure(self) -> ClaudeSDKClient:
        if self.client and time.monotonic() - self.last_used > IDLE_SEC:
            log.info("session idle, starting fresh")
            await self.close()
        if self.client is None:
            self.client = ClaudeSDKClient(options=make_options())
            await self.client.connect()
        self.last_used = time.monotonic()
        return self.client


sessions: dict[str, Session] = {}
app = FastAPI(title="agent")


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

    reply = " ".join(after_tools) or " ".join(before_tools[-1:])
    return {"reply": reply, "action": action, "cost_usd": cost, "tools": tools_called}


@app.post("/chat")
async def chat(body: ChatIn):
    session = sessions.setdefault(body.device_id, Session())
    t0 = time.monotonic()
    async with session.lock:
        try:
            client = await session.ensure()
            prompt = f"[Служебно: {body.context}]\n{body.text}" if body.context else body.text
            result = await run_turn(client, prompt)
        except Exception:  # noqa: BLE001
            log.exception("agent turn failed")
            await session.close()
            result = {"reply": FALLBACK_REPLY, "action": None, "cost_usd": None, "tools": []}
    result["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    write_log({"ts": datetime.now().isoformat(timespec="seconds"),
               "device": body.device_id, "heard": body.text, **result})
    return result


@app.post("/reset")
async def reset(body: ResetIn):
    session = sessions.pop(body.device_id, None)
    if session:
        await session.close()
    return {"ok": True}


@app.get("/health")
def health():
    return {"ok": True, "model": MODEL, "sessions": len(sessions)}
