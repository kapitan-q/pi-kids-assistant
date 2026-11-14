"""Шлюз для Pi — единственная точка входа в ядро.

POST /ask    multipart: audio=<wav>, device_id, now_playing=<item_id>, source=<модуль>, position=<сек>
POST /text   {"device_id", "text", "speaker"?, "now_playing"?, "source"?} — текстом (отладка;
             speaker учитывается только с X-Admin-Token)
POST /reset  {"device_id"}                                 — забыть контекст разговора
POST /progress {"source", "item_id", "position", "finished"} — где остановились (Pi раз в 30 с)
GET  /policy                                               — текущие лимиты (Pi опрашивает раз в минуту)
GET  /speech/<id>.wav                                      — озвученный ответ
GET  /m/<модуль>/<путь>                                    — файлы модулей (прокси; за Traefik — напрямую)
GET  /health, /livez

Управление (заголовок X-Admin-Token = ADMIN_TOKEN; пустой ADMIN_TOKEN — выключено):
GET    /control/status
POST   /control/override  {"mode": "quiet", "minutes": 60, "who": ["older"]}
DELETE /control/override  (?who=older)  — вернуть обычный режим (в т.ч. поверх расписания)

Ответ /ask и /text:
{
  "heard": "включи колобка",
  "reply": "Включаю сказку Колобок",
  "speech_url": "https://server/speech/ab12.wav" | null,   # сначала проиграть это
  "action": {"type": "play", "source": "books", "item_id": "…", "title": "Колобок",
             "url": ".../m/books/stream/…", "start": 120?}
            | {"type": "stop"} | {"type": "resume"}
            | {"type": "seek", "seconds": 30, "mode": "relative" | "absolute"}
            | {"type": "volume", "level": 50} | {"type": "volume", "change": -10}
            | null,
  "limits": {"mode": "night", "allow_playback": true, "max_volume": 30,
             "stop_after_sec": 2400 | null, "until": "2026-11-15T07:00+01:00"}
}
Pi:
- на слово активации ставит воспроизведение на паузу; после ответа play/stop/resume/seek/volume.
  seek и volume = применить и снять с паузы. При action=null остаётся на паузе.
- громкость хранит Pi (10..100), change прибавляет с обрезкой до 10..limits.max_volume.
- limits.stop_after_sec при play — таймер: через столько секунд плавно выключить.
- раз в минуту GET /policy: если allow_playback=false — остановить; громкость > max_volume — убавить.
"""
import asyncio
import contextlib
import hmac
import logging
import os
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from starlette.background import BackgroundTask

import actions
import lang
import modules
import parent_commands
import policy
import quick_commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

STT_URL = os.getenv("STT_URL", "http://stt:8000")
TTS_URL = os.getenv("TTS_URL", "http://tts:8000")
AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")
GATEWAY_TOKEN = os.getenv("GATEWAY_TOKEN", "")
ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")
AGENT_HTTP_TIMEOUT = float(os.getenv("AGENT_HTTP_TIMEOUT", "40"))
MAX_AUDIO_BYTES = int(os.getenv("MAX_AUDIO_BYTES", "5000000"))
QUICK_COMMANDS = os.getenv("QUICK_COMMANDS", "1") not in ("0", "false", "no")
SPEECH_DIR = Path(os.getenv("SPEECH_DIR", "/tmp/speech"))
SPEECH_TTL_SEC = int(os.getenv("SPEECH_TTL_SEC", "600"))
# Старый Pi-клиент знает только tale_id и не шлёт source: считаем, что это этот модуль.
LEGACY_SOURCE = os.getenv("LEGACY_SOURCE", "books")
CORE_SERVICES = {"stt": STT_URL, "tts": TTS_URL, "agent": AGENT_URL}

http: httpx.AsyncClient
now_playing: dict[str, dict] = {}     # device_id -> {"source", "item_id", "tags", "age_min"} последнего play
_background: set[asyncio.Task] = set()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(timeout=httpx.Timeout(AGENT_HTTP_TIMEOUT, connect=5.0))
    policy.config()  # сразу проверить, что конфиг читается
    log.info("модули: %s", list(modules.enabled()))
    if not ADMIN_TOKEN:
        log.warning("ADMIN_TOKEN не задан — /control выключен")
    yield
    await http.aclose()


app = FastAPI(title="kids-speaker gateway", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)


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


def background(coro) -> None:
    """Фоновая задача, которая не должна задерживать ответ."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


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


async def save_progress(source: str | None, item_id: str | None, position: float | None,
                        finished: bool = False) -> None:
    """Позиция прослушивания → модуль (svc report_progress). Ошибки не мешают ответу."""
    if not source or not item_id or position is None:
        return
    await modules.call_svc(source, "report_progress",
                           {"item_id": str(item_id), "position": float(position), "finished": finished})


async def playing_item(device_id: str, source: str | None, item_id: str | None) -> dict | None:
    """Что сейчас загружено в плеере Pi — для правил при «продолжай»/перемотке.
    None — ничего; tags=None — загружено, но теги узнать не удалось (правила решат строго)."""
    if not item_id or not re.fullmatch(actions.ID_RE[1:-1], item_id):
        return None
    source = source or LEGACY_SOURCE
    cached = now_playing.get(device_id)
    if cached and cached["source"] == source and cached["item_id"] == item_id:
        return cached
    info = await modules.call_svc(source, "item_info", {"item_id": item_id}) or {}
    item = {"source": source, "item_id": item_id, "tags": info.get("tags"), "age_min": info.get("age_min")}
    if item["tags"] is not None:
        now_playing[device_id] = item
    return item


def to_pi(action: dict | None) -> dict | None:
    """Готовая команда для Pi: url для play + поле tale_id для старого клиента."""
    if not action:
        return None
    if action["type"] != "play":
        action.pop("tags", None)
        return action
    action["url"] = f"{PUBLIC_BASE_URL}/m/{action['source']}/{action.pop('path')}"
    if action["source"] == LEGACY_SOURCE:
        action["tale_id"] = action["item_id"]   # совместимость со старым client.py — убрать после обновления Pi
    return action


def identify_speaker(_audio: bytes) -> str | None:
    """Заглушка под узнавание по голосу. Вернёт id профиля или None."""
    return None


# ---------- основной сценарий ----------

def _say_time(dt) -> str:
    return lang.t("time.hours_minutes", h=dt.hour, m=dt.minute) if dt.minute else lang.t("time.hours", h=dt.hour)


async def handle_parent_command(text: str, can_relax: bool) -> dict | None:
    cfg = policy.config()
    cmd = parent_commands.parse(text, cfg.get("profiles") or {}, policy.now())
    if not cmd:
        return None
    names = ""
    if cmd["who"]:
        names = lang.t("parent.for_names", names=", ".join(
            policy.profile(w).get("name", w).lower() for w in cmd["who"]))
    if cmd["mode"] == "clear":
        if not can_relax:
            return {"reply": lang.t("parent.relax_denied")}
        policy.relax(cmd["who"])
        return {"reply": lang.t("parent.ok_clear", names=names)}
    conf = policy.mode_conf(cmd["mode"])
    until = cmd["until"] or policy.now() + timedelta(minutes=conf.get("default_minutes", 60))
    policy.add_override(cmd["mode"], until, cmd["who"])
    return {"reply": lang.t("parent.ok_mode", title=conf.get("title", cmd["mode"]), names=names,
                            time=_say_time(until))}


async def _answer(text: str, reply: str, action: dict | None, lim: dict, **extra) -> dict:
    return {"heard": text, "reply": reply, "speech_url": await synthesize(reply),
            "action": to_pi(action), "limits": lim, **extra}


def available_modules(res) -> dict[str, str]:
    """Модули, которые сейчас можно включать: включённые ∩ разрешённые режимом."""
    return modules.titles(policy.mode_modules(res))


async def handle_text(device_id: str, text: str, speaker: str | None,
                      playing: tuple[str | None, str | None] = (None, None)) -> dict:
    """playing = (source, item_id) того, что загружено в плеере Pi."""
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
            return await _answer(cmd_text, lang.t("parent.not_understood"), None,
                                 policy.limits(policy.resolve(None)), parent_command=True)

    # 2. Режим для этого говорящего
    res = policy.resolve(speaker)
    lim = policy.limits(res)
    if not policy.children_allowed(res) and not verified_parent:
        reply = res.conf.get("message", "") if policy.announce_once(res) else ""
        return await _answer(text, reply, {"type": "stop"}, lim, blocked=True)

    # 3. Простые команды плееру («стоп», «потише», «продолжай») — сразу, без Claude
    if QUICK_COMMANDS and (quick := quick_commands.parse(text)) is not None:
        action, reply, lim = await enforce(quick["action"], "", res, lim, device_id, playing,
                                           speaker, verified_parent)
        log.info("quick %s: %r -> %s", device_id, text, action)
        return await _answer(text, reply, action, lim, quick=True)

    # 4. Агент
    try:
        r = await http.post(f"{AGENT_URL}/chat", json={
            "device_id": device_id, "text": text,
            "context": policy.agent_context(res, speaker, available_modules(res)),
        })
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        log.exception("агент не ответил")
        return await _answer(text, lang.t("fallback"), None, lim, error="agent")
    reply = data.get("reply", "")
    action = actions.validate(data.get("action"), modules.enabled())
    action, reply, lim = await enforce(action, reply, res, lim, device_id, playing, speaker, verified_parent)
    return await _answer(text, reply, action, lim)


async def enforce(action: dict | None, reply: str, res, lim: dict, device_id: str,
                  playing: tuple[str | None, str | None], speaker: str | None,
                  verified_parent: bool) -> tuple[dict | None, str, dict]:
    """Страховка: правила режима важнее того, что решил агент (или быстрая команда) —
    и для нового, и для «продолжай»/перемотки уже загруженного."""
    if action and action["type"] in ("play", "resume", "seek") and not verified_parent:
        if action["type"] == "play":
            item = {k: action.get(k) for k in ("source", "item_id", "tags", "age_min")}
        else:
            item = await playing_item(device_id, *playing)
        deny = policy.check_playback(res, item, policy.profile(speaker).get("age"))
        if deny:
            action, reply = ({"type": "stop"} if action["type"] != "play" else None), deny
        else:
            policy.start_budget(res)
            lim = policy.limits(res)  # пересчитать stop_after_sec
    if action and action["type"] == "play":
        now_playing[device_id] = {k: action.get(k) for k in ("source", "item_id", "tags", "age_min")}
    if action and action["type"] == "volume" and action.get("level"):
        action["level"] = min(action["level"], lim["max_volume"])
    return action, reply, lim


# ---------- API для Pi ----------

class TextIn(BaseModel):
    device_id: str = "default"
    text: str
    speaker: str | None = None
    now_playing: str | None = None
    source: str | None = None


class ResetIn(BaseModel):
    device_id: str = "default"


class OverrideIn(BaseModel):
    mode: str
    minutes: int | None = None
    who: list[str] | None = None


class ProgressIn(BaseModel):
    device_id: str = "default"
    source: str | None = None
    item_id: str | None = None
    tale_id: str | None = None      # старый client.py
    position: float
    finished: bool = False


@app.post("/ask", dependencies=[Depends(check_token)])
async def ask(audio: UploadFile = File(...), device_id: str = Form("default"),
              now_playing: str = Form(""), source: str = Form(""), position: str = Form("")):
    t0 = time.monotonic()
    src = source or (LEGACY_SOURCE if now_playing else None)
    if now_playing and position:   # заодно запомнить, где остановились — фоном, не задерживая ответ
        with contextlib.suppress(ValueError):
            background(save_progress(src, now_playing, float(position)))
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(413, "audio too large")
    text = await transcribe(data, audio.filename or "audio.wav")
    t_stt = time.monotonic()
    if not text:
        return {"heard": "", "reply": "", "speech_url": None, "action": None,
                "limits": policy.limits(policy.resolve(None))}
    result = await handle_text(device_id, text, identify_speaker(data), (src, now_playing or None))
    result["timing_ms"] = {"stt": int((t_stt - t0) * 1000),
                           "total": int((time.monotonic() - t0) * 1000)}
    log.info("ask %s: %r -> %r %s", device_id, text, result["reply"], result.get("action"))
    return result


@app.post("/text", dependencies=[Depends(check_token)])
async def ask_text(body: TextIn, x_admin_token: str | None = Header(default=None)):
    t0 = time.monotonic()
    speaker = body.speaker if is_admin(x_admin_token) else None
    src = body.source or (LEGACY_SOURCE if body.now_playing else None)
    result = await handle_text(body.device_id, body.text, speaker, (src, body.now_playing))
    result["timing_ms"] = {"total": int((time.monotonic() - t0) * 1000)}
    return result


@app.post("/reset", dependencies=[Depends(check_token)])
async def reset(body: ResetIn):
    r = await http.post(f"{AGENT_URL}/reset", json={"device_id": body.device_id})
    r.raise_for_status()
    return {"ok": True}


@app.post("/progress", dependencies=[Depends(check_token)])
async def progress(body: ProgressIn):
    """Pi раз в полминуты и при остановке сообщает позицию — чтобы потом продолжить с того места."""
    item_id = body.item_id or body.tale_id
    await save_progress(body.source or LEGACY_SOURCE, item_id, body.position, body.finished)
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


PROXY_HEADERS = ("content-type", "content-length", "content-range", "accept-ranges", "last-modified", "etag")


@app.api_route("/m/{module}/{path:path}", methods=["GET", "HEAD"], dependencies=[Depends(check_token)])
async def module_files(module: str, path: str, request: Request):
    """Файлы модулей для плеера. За Traefik /m/<модуль> уходит в модуль напрямую — это для режима «дома»."""
    base = modules.base_url(module)
    if not base or ".." in path or not re.fullmatch(r"[\w./-]+", path):
        raise HTTPException(404)
    headers = {h: v for h, v in request.headers.items() if h.lower() in ("range", "if-range")}
    upstream = http.build_request(request.method, f"{base}/{path}", headers=headers,
                                  timeout=httpx.Timeout(30, read=None))
    r = await http.send(upstream, stream=True)
    out_headers = {h: v for h, v in r.headers.items() if h.lower() in PROXY_HEADERS}
    if request.method == "HEAD":
        await r.aclose()
        return StreamingResponse(iter(()), status_code=r.status_code, headers=out_headers)
    return StreamingResponse(r.aiter_raw(), status_code=r.status_code, headers=out_headers,
                             background=BackgroundTask(r.aclose))


# ---------- управление ----------

@app.get("/control/status", dependencies=[Depends(check_admin)])
async def admin_status():
    return policy.status()


@app.post("/control/override", dependencies=[Depends(check_admin)])
async def admin_override(body: OverrideIn):
    if body.mode not in (policy.config().get("modes") or {}):
        raise HTTPException(400, f"нет режима {body.mode}")
    minutes = body.minutes or policy.mode_conf(body.mode).get("default_minutes", 60)
    return policy.add_override(body.mode, policy.now() + timedelta(minutes=minutes), body.who)


@app.delete("/control/override", dependencies=[Depends(check_admin)])
async def admin_clear(who: str | None = None):
    removed = policy.relax([who] if who else None)
    return {"removed": removed, "now": policy.status()}


# ---------- здоровье ----------

@app.get("/livez")
async def livez():
    return {"ok": True}


async def _alive(url: str) -> bool:
    try:
        r = await http.get(f"{url}/health", timeout=3)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception:  # noqa: BLE001
        return False


@app.get("/health")
async def health():
    """ok — ядро; модули — отдельно: упавший модуль не должен снимать колонку с роутинга."""
    core_names, mod_names = list(CORE_SERVICES), list(modules.enabled())
    checks = await asyncio.gather(*(_alive(u) for u in CORE_SERVICES.values()),
                                  *(_alive(modules.base_url(m)) for m in mod_names))
    core = dict(zip(core_names, checks[:len(core_names)]))
    mods = dict(zip(mod_names, checks[len(core_names):]))
    return {"ok": all(core.values()), "core": core, "modules": mods, "mode": policy.resolve(None).mode}
