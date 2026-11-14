"""Шлюз для Pi — единственная точка входа.

POST /ask    multipart: audio=<wav>, device_id=kids-room, now_playing=<tale_id|пусто>
POST /text   {"device_id": "...", "text": "...", "speaker": "older"} — текстом (отладка;
             speaker учитывается только с X-Admin-Token)
POST /reset  {"device_id": "..."}                          — забыть контекст разговора
GET  /policy                                               — текущие лимиты (Pi опрашивает раз в минуту)
GET  /speech/<id>.wav                                      — озвученный ответ
GET  /media/<путь>                                         — файлы сказок
GET  /health, /livez

Админка (заголовок X-Admin-Token = ADMIN_TOKEN; пустой ADMIN_TOKEN — админка выключена):
GET    /admin/status
POST   /admin/override  {"mode": "quiet", "minutes": 60, "who": ["older"]}
DELETE /admin/override  (?who=older)  — вернуть обычный режим (в т.ч. поверх расписания)

Снаружи всё закрыто basic-auth в Traefik (docker-compose.traefik.yml).
GATEWAY_TOKEN (X-Token) — необязательный второй слой для устройства.

Ответ /ask и /text:
{
  "heard": "включи колобка",
  "reply": "Включаю сказку Колобок",
  "speech_url": "https://server/speech/ab12.wav" | null,   # сначала проиграть это
  "action": {"type": "play", "url": ".../media/kolobok.mp3", "title": "Колобок", "tale_id": 3}
            | {"type": "stop"} | {"type": "resume"}
            | {"type": "seek", "seconds": 30, "mode": "relative" | "absolute"}
            | {"type": "volume", "level": 50} | {"type": "volume", "change": -10}
            | null,
  "limits": {"mode": "night", "allow_playback": true, "max_volume": 30,
             "stop_after_sec": 2400 | null, "until": "2026-11-15T07:00+01:00"}
}
Pi:
- на слово активации ставит сказку на паузу; после ответа play/stop/resume/seek/volume.
  seek и volume = применить и снять с паузы. При action=null сказка остаётся на паузе.
- громкость хранит Pi (10..100), change прибавляет с обрезкой до 10..limits.max_volume.
- limits.stop_after_sec при play — таймер: через столько секунд плавно выключить сказку.
- раз в минуту GET /policy: если allow_playback=false — остановить; громкость > max_volume — убавить.
"""
import contextlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import parent_commands
import policy

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

STT_URL = os.getenv("STT_URL", "http://stt:8000")
TTS_URL = os.getenv("TTS_URL", "http://tts:8000")
AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")
TALES_URL = os.getenv("TALES_URL", "http://tales:8000")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
AGENT_HTTP_TIMEOUT = float(os.getenv("AGENT_HTTP_TIMEOUT", "40"))
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", "5000000"))
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library"))
SPEECH_DIR = Path("/tmp/speech")
SPEECH_TTL_SEC = 600
FALLBACK_REPLY = "Ой, у меня что-то не получилось. Попробуй ещё раз."

http: httpx.AsyncClient


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(timeout=httpx.Timeout(AGENT_HTTP_TIMEOUT, connect=5.0))
    policy.config()  # сразу проверить, что конфиг читается
    if not ADMIN_TOKEN:
        log.warning("ADMIN_TOKEN не задан — админка выключена")
    yield
    await http.aclose()


app = FastAPI(title="kids-speaker gateway", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/media", StaticFiles(directory=LIBRARY_DIR, check_dir=False), name="media")


def _same(a: str | None, b: str) -> bool:
    return bool(a) and hmac.compare_digest(a.encode(), b.encode())


def check_token(x_token: str | None = Header(default=None)) -> None:
    if GATEWAY_TOKEN and not _same(x_token, GATEWAY_TOKEN):
        raise HTTPException(401, "bad token")


def check_admin(x_admin_token: str | None = Header(default=None)) -> None:
    if not ADMIN_TOKEN:
        raise HTTPException(403, "admin disabled")
    if not _same(x_admin_token, ADMIN_TOKEN):
        raise HTTPException(401, "bad admin token")


def is_admin(x_admin_token: str | None) -> bool:
    return bool(ADMIN_TOKEN) and _same(x_admin_token, ADMIN_TOKEN)


# ---------- обращения к сервисам ----------

async def transcribe(data: bytes, filename: str) -> str:
    r = await http.post(f"{STT_URL}/transcribe",
                        files={"file": (filename, data, "application/octet-stream")})
    r.raise_for_status()
    return r.json()["text"].strip()


def _cleanup_speech() -> None:
    cutoff = time.time() - SPEECH_TTL_SEC
    for f in SPEECH_DIR.glob("*.wav"):
        if f.stat().st_mtime < cutoff:
            f.unlink(missing_ok=True)


async def synthesize(text: str) -> str | None:
    """Озвучить ответ. Ошибка TTS не должна ронять весь ответ — тогда просто без речи."""
    if not text:
        return None
    try:
        r = await http.post(f"{TTS_URL}/synthesize", json={"text": text}, timeout=30)
        r.raise_for_status()
    except httpx.HTTPError:
        log.exception("TTS не ответил — отдаю ответ без речи")
        return None
    sid = uuid.uuid4().hex
    (SPEECH_DIR / f"{sid}.wav").write_bytes(r.content)
    _cleanup_speech()
    return f"{PUBLIC_BASE_URL}/speech/{sid}.wav"


async def get_tale(tale_id) -> dict | None:
    try:
        tale_id = int(tale_id)
    except (TypeError, ValueError):
        return None
    try:
        r = await http.get(f"{TALES_URL}/api/tales/{tale_id}", timeout=5)
    except httpx.HTTPError:
        log.exception("tales недоступен")
        return None
    return r.json() if r.status_code == 200 else None


async def resolve_action(action: dict | None) -> dict | None:
    if not action:
        return None
    if action["type"] != "play":
        return action  # stop / resume / seek / volume — как есть
    tale = await get_tale(action.get("tale_id"))
    if not tale or not tale.get("file"):
        return None
    return {
        "type": "play",
        "tale_id": tale["id"],
        "title": tale["title"],
        "tags": tale.get("tags", ""),
        "url": f"{PUBLIC_BASE_URL}/media/{quote(tale['file'])}",
    }


def identify_speaker(_audio: bytes) -> str | None:
    """Заглушка под узнавание по голосу (шаг 2). Вернёт id профиля или None."""
    return None


# ---------- основной сценарий ----------

def _say_time(dt) -> str:
    return f"{dt.hour} {dt.minute:02d}" if dt.minute else f"{dt.hour} часов"


async def handle_parent_command(text: str, can_relax: bool) -> dict | None:
    cfg = policy.config()
    cmd = parent_commands.parse(text, cfg.get("profiles") or {}, policy.now())
    if not cmd:
        return None
    names = ""
    if cmd["who"]:
        names = " для " + ", ".join(policy.profile(w).get("name", w).lower() for w in cmd["who"])
    if cmd["mode"] == "clear":
        if not can_relax:
            return {"reply": "Снять режим можно только с телефона."}
        policy.relax(cmd["who"])
        return {"reply": f"Хорошо, обычный режим{names}."}
    conf = policy.mode_conf(cmd["mode"])
    until = cmd["until"] or policy.now() + timedelta(minutes=conf.get("default_minutes", 60))
    policy.add_override(cmd["mode"], until, cmd["who"])
    title = conf.get("title", cmd["mode"])
    return {"reply": f"Хорошо. {title}{names} до {_say_time(until)}."}


async def _answer(text: str, reply: str, action: dict | None, lim: dict, **extra) -> dict:
    return {"heard": text, "reply": reply, "speech_url": await synthesize(reply),
            "action": action, "limits": lim, **extra}


async def handle_text(device_id: str, text: str, speaker: str | None,
                      now_playing: str | None = None) -> dict:
    # 1. Команды родителя.
    #    Проверенный родитель (speaker=parent — пока только через админ-токен) может всё.
    #    Кодовая фраза голосом — только ужесточать режим (её могут подслушать дети),
    #    снимать — если parent.voice_can_relax: true. К агенту такие фразы не уходят.
    cfg = policy.config()
    parent_cfg = cfg.get("parent") or {}
    verified_parent = policy.is_parent(speaker)
    rest = parent_commands.strip_passphrase(text, parent_cfg.get("passphrase", ""))
    if rest is not None or verified_parent:
        cmd_text = rest if rest is not None else text
        can_relax = verified_parent or bool(parent_cfg.get("voice_can_relax", False))
        result = await handle_parent_command(cmd_text, can_relax)
        if result:
            return await _answer(cmd_text, result["reply"], None,
                                 policy.limits(policy.resolve(None)), parent_command=True)
        if rest is not None:
            return await _answer(cmd_text, "Не поняла команду.", None,
                                 policy.limits(policy.resolve(None)), parent_command=True)

    # 2. Режим для этого говорящего
    res = policy.resolve(speaker)
    lim = policy.limits(res)
    if not policy.children_allowed(res) and not verified_parent:
        reply = res.conf.get("message", "") if policy.announce_once(res) else ""
        return await _answer(text, reply, {"type": "stop"}, lim, blocked=True)

    # 3. Агент
    try:
        r = await http.post(f"{AGENT_URL}/chat", json={
            "device_id": device_id, "text": text,
            "context": policy.agent_context(res, speaker),
        })
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        log.exception("агент не ответил")
        return await _answer(text, FALLBACK_REPLY, None, lim, error="agent")
    reply = data.get("reply", "")
    action = await resolve_action(data.get("action"))

    # 4. Страховка: правила режима важнее того, что решил агент —
    #    и для новой сказки, и для «продолжай»/перемотки уже играющей.
    if action and action["type"] in ("play", "resume", "seek") and not verified_parent:
        if action["type"] == "play":
            tags = action["tags"]
        else:
            tale = await get_tale(now_playing) if now_playing else None
            tags = tale.get("tags", "") if tale else None
        deny = policy.check_playback(res, tags)
        if deny:
            action, reply = ({"type": "stop"} if action["type"] != "play" else None), deny
        else:
            policy.start_budget(res)
            lim = policy.limits(res)  # пересчитать stop_after_sec
    if action and action["type"] == "volume" and action.get("level"):
        action["level"] = min(action["level"], lim["max_volume"])

    return await _answer(text, reply, action, lim)


# ---------- API для Pi ----------

class TextIn(BaseModel):
    device_id: str = "default"
    text: str
    speaker: str | None = None
    now_playing: str | None = None


class ResetIn(BaseModel):
    device_id: str = "default"


class OverrideIn(BaseModel):
    mode: str
    minutes: int | None = None
    who: list[str] | None = None


@app.post("/ask", dependencies=[Depends(check_token)])
async def ask(audio: UploadFile = File(...), device_id: str = Form("default"),
              now_playing: str = Form("")):
    t0 = time.monotonic()
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")
    text = await transcribe(data, audio.filename or "audio.wav")
    t_stt = time.monotonic()
    if not text:
        return {"heard": "", "reply": "", "speech_url": None, "action": None,
                "limits": policy.limits(policy.resolve(None))}
    result = await handle_text(device_id, text, identify_speaker(data), now_playing or None)
    result["timing_ms"] = {"stt": int((t_stt - t0) * 1000),
                           "total": int((time.monotonic() - t0) * 1000)}
    log.info("ask %s: %r -> %r %s", device_id, text, result["reply"], result.get("action"))
    return result


@app.post("/text", dependencies=[Depends(check_token)])
async def ask_text(body: TextIn, x_admin_token: str | None = Header(default=None)):
    t0 = time.monotonic()
    speaker = body.speaker if is_admin(x_admin_token) else None
    result = await handle_text(body.device_id, body.text, speaker, body.now_playing)
    result["timing_ms"] = {"total": int((time.monotonic() - t0) * 1000)}
    return result


@app.post("/reset", dependencies=[Depends(check_token)])
async def reset(body: ResetIn):
    r = await http.post(f"{AGENT_URL}/reset", json={"device_id": body.device_id})
    r.raise_for_status()
    return {"ok": True}


@app.get("/policy", dependencies=[Depends(check_token)])
async def get_policy():
    return policy.limits(policy.resolve(None))


@app.get("/speech/{name}")
async def speech(name: str):
    if not re.fullmatch(r"[0-9a-f]{32}\.wav", name):
        raise HTTPException(404)
    path = SPEECH_DIR / name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


# ---------- админка ----------

@app.get("/admin/status", dependencies=[Depends(check_admin)])
async def admin_status():
    return policy.status()


@app.post("/admin/override", dependencies=[Depends(check_admin)])
async def admin_override(body: OverrideIn):
    if body.mode not in (policy.config().get("modes") or {}):
        raise HTTPException(400, f"нет режима {body.mode}")
    minutes = body.minutes or policy.mode_conf(body.mode).get("default_minutes", 60)
    return policy.add_override(body.mode, policy.now() + timedelta(minutes=minutes), body.who)


@app.delete("/admin/override", dependencies=[Depends(check_admin)])
async def admin_clear(who: str | None = None):
    removed = policy.relax([who] if who else None)
    return {"removed": removed, "now": policy.status()}


# ---------- здоровье ----------

@app.get("/livez")
async def livez():
    return {"ok": True}


@app.get("/health")
async def health():
    status = {}
    for name, url in {"stt": STT_URL, "tts": TTS_URL, "agent": AGENT_URL, "tales": TALES_URL}.items():
        try:
            r = await http.get(f"{url}/health", timeout=3)
            status[name] = r.status_code == 200 and r.json().get("ok", False)
        except Exception:  # noqa: BLE001
            status[name] = False
    return {"ok": all(status.values()), "services": status, "mode": policy.resolve(None).mode}
