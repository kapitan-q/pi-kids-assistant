"""Шлюз для Pi — единственная точка входа в ядро.

POST /ask    multipart: audio=<wav>, device_id, now_playing=<item_id>, source=<модуль>, position=<сек>
POST /text   {"device_id", "text", "speaker"?, "now_playing"?, "source"?} — текстом (отладка;
             speaker учитывается только с X-Admin-Token)
POST /reset  {"device_id"}                                 — забыть контекст разговора
POST /progress {"source", "item_id", "position", "finished"} — где остановились (Pi раз в 30 с)
GET  /policy?device_id=…  — текущие лимиты для того, кто включил то, что играет (Pi раз в минуту)
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
import admin
import lang
import modules
import onboarding
import parent_commands
import policy
import profiles
import quick_commands
import rights
import voices

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("gateway")

STT_URL = os.getenv("STT_URL", "http://stt:8000")
TTS_URL = os.getenv("TTS_URL", "http://tts:8000")
AGENT_URL = os.getenv("AGENT_URL", "http://agent:8000")
VOICEID_URL = os.getenv("VOICEID_URL", "http://voiceid:8000")
VOICEID_TIMEOUT = float(os.getenv("VOICEID_TIMEOUT", "5"))
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
CORE_SERVICES = {"stt": STT_URL, "tts": TTS_URL, "agent": AGENT_URL, "voiceid": VOICEID_URL}

http: httpx.AsyncClient
# device_id -> {"source", "item_id", "tags", "age_min", "speaker"} последнего play (speaker — кто включил)
now_playing: dict[str, dict] = {}
_background: set[asyncio.Task] = set()


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    SPEECH_DIR.mkdir(parents=True, exist_ok=True)
    http = httpx.AsyncClient(timeout=httpx.Timeout(AGENT_HTTP_TIMEOUT, connect=5.0))
    log.info("роли: %s", [r.id for r in profiles.roles.all()])
    log.info("модули: %s, пользователи: %s", list(modules.enabled()), [u.id for u in profiles.store.all()])
    if not admin.ADMIN_PASSWORD_HASH:
        log.warning("ADMIN_PASSWORD_HASH не задан — /admin не пустит никого")
    if not ADMIN_TOKEN:
        log.warning("ADMIN_TOKEN не задан — /control выключен")
    rights_task = asyncio.create_task(rights.refresh_loop())   # права, которые объявляют модули
    yield
    rights_task.cancel()
    await http.aclose()


app = FastAPI(title="kids-speaker gateway", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(admin.router)


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


async def voice_print(data: bytes, filename: str) -> dict | None:
    """Голосовой отпечаток фразы (voiceid). Ошибка/таймаут — None: тогда говорящий неизвестен (строже)."""
    try:
        r = await http.post(f"{VOICEID_URL}/embed", timeout=VOICEID_TIMEOUT,
                            files={"file": (filename, data, "audio/wav")})
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError) as e:
        log.warning("voiceid: %r", e)
        return None


def identify_speaker(vp: dict | None) -> tuple[str | None, float]:
    """(id профиля | None, близость). Ниже порога voice.threshold — не узнан."""
    if not vp:
        return None, 0.0
    pid, score = voices.store.identify(vp["vector"])
    threshold = float((policy.config().get("voice") or {}).get("threshold", 0.6))
    if pid and score >= threshold and profiles.store.get(pid):
        profiles.store.touch(pid)
        return pid, score
    return None, score


# ---------- основной сценарий ----------

def _say_time(dt) -> str:
    return lang.t("time.hours_minutes", h=dt.hour, m=dt.minute) if dt.minute else lang.t("time.hours", h=dt.hour)


def command_targets() -> dict[str, dict]:
    """Кого можно назвать в голосовой команде режима: люди, на которых действуют режимы, и роли."""
    out = {uid: policy.profile(uid) for uid in policy.targets()}
    out.update({r.id: {"name": r.title} for r in profiles.roles.all() if r.id != profiles.GUEST})
    return out


def _target_name(tid: str) -> str:
    p = policy.profile(tid)
    if p:
        return p["name"]
    r = profiles.roles.get(tid)
    return r.title if r else tid


async def handle_parent_command(text: str, can_relax: bool, actor: str | None = None,
                                via: str = "voice") -> dict | None:
    cmd = parent_commands.parse(text, command_targets(), policy.now())
    if not cmd:
        return None
    names = ""
    if cmd["who"]:
        names = lang.t("parent.for_names", names=", ".join(_target_name(w).lower() for w in cmd["who"]))
    if cmd["mode"] == "clear":
        if not can_relax:
            return {"reply": lang.t("parent.relax_denied")}
        policy.relax(cmd["who"], via, actor)
        return {"reply": lang.t("parent.ok_clear", names=names)}
    conf = policy.mode_conf(cmd["mode"])
    until = cmd["until"] or policy.now() + timedelta(minutes=conf.get("default_minutes", 60))
    policy.add_override(cmd["mode"], until, cmd["who"], via, actor)
    return {"reply": lang.t("parent.ok_mode", title=conf.get("title", cmd["mode"]), names=names,
                            time=_say_time(until))}


async def _answer(text: str, reply: str, action: dict | None, lim: dict, **extra) -> dict:
    return {"heard": text, "reply": reply, "speech_url": await synthesize(reply),
            "action": to_pi(action), "limits": lim, **extra}


def agent_rights(res, speaker: str | None) -> dict:
    """Что агент передаёт модулям: какие модули подключить и права каждого (+ возраст)."""
    g = policy.grants(speaker)
    mods = policy.allowed_modules(res, g) or []
    return {"modules": {m: g.for_module(m) for m in mods}, "age": g.age}


async def handle_text(device_id: str, text: str, speaker: str | None,
                      playing: tuple[str | None, str | None] = (None, None),
                      utt: onboarding.Utterance | None = None, admin_verified: bool = False) -> dict:
    """playing = (source, item_id) того, что загружено в плеере Pi; utt — голос фразы (для знакомства).
    Порядок важен: знакомство → команды плееру → команды режимов → блокировка режимом →
    «давай познакомимся» → гостевой доступ → агент."""
    utt = utt or onboarding.Utterance(text=text)
    res = policy.resolve(speaker)          # у кого core.modes_exempt — всегда default
    lim = policy.limits(res)

    # 0. Идёт знакомство — все реплики ему
    if onboarding.active(device_id):
        r = onboarding.step(device_id, utt)
        return await _answer(text, r["reply"], None, lim, onboarding=True, expect_reply=r["expect_reply"])

    # 1. Простые команды плееру («стоп», «потише», «сними с паузы») — сразу, без Claude.
    #    Раньше команд режимов: «замолчи» — это «стоп», а не тихий час. Работают и для неузнанного
    #    голоса (короткие фразы узнаются хуже всего); «продолжай» всё равно проверяется правилами.
    if QUICK_COMMANDS and (quick := quick_commands.parse(text)) is not None:
        action, reply, lim = await enforce(quick["action"], "", res, lim, device_id, playing, speaker)
        log.info("quick %s: %r -> %s", device_id, text, action)
        return await _answer(text, reply, action, lim, quick=True)

    # 2. Команды режимов («тихий час», «режим уроков для старшего», «отбой тишины») — фразой целиком.
    #    С правом core.control (узнан по голосу) — без кодовой фразы, и ставить, и снимать.
    #    Кодовая фраза без права — только ужесточить (фразу могут подслушать). Админ-токен — всё.
    #    К агенту такие фразы не уходят.
    controller = policy.can_control(speaker)
    rest = parent_commands.strip_passphrase(text, (policy.config().get("parent") or {}).get("passphrase", ""))
    if rest is not None or controller:
        cmd_text = rest if rest is not None else text
        result = await handle_parent_command(cmd_text, can_relax=admin_verified or controller,
                                             actor=speaker, via="api" if admin_verified else "voice")
        if result:
            return await _answer(cmd_text, result["reply"], None, lim, parent_command=True)
        if rest is not None:
            return await _answer(cmd_text, lang.t("parent.not_understood"), None, lim, parent_command=True)

    # 3. Режим блокирует этого говорящего
    if not policy.children_allowed(res):
        reply = res.conf.get("message", "") if policy.announce_once(res) else ""
        return await _answer(text, reply, {"type": "stop"}, lim, blocked=True)

    # 4. «Давай познакомимся» — новый пользователь голосом
    if onboarding.is_trigger(text):
        r = onboarding.start(device_id, utt, speaker)
        return await _answer(text, r["reply"], None, lim, onboarding=True, expect_reply=r["expect_reply"])

    # 5. Гостевой доступ выключен: неузнанному и человеку без ролей — только плеер и знакомство
    if policy.unverified(speaker) and not admin_verified and not policy.guest_enabled():
        key = "guest_off.no_roles" if speaker else "guest_off.unknown"
        return await _answer(text, lang.t(key), None, lim, guest_off=True)

    # 6. Агент: подключает только доступные модули и передаёт им права говорящего
    try:
        r = await http.post(f"{AGENT_URL}/chat", json={
            "device_id": device_id, "text": text,
            "context": policy.agent_context(res, speaker, policy.available_titles(res, speaker)),
            "rights": agent_rights(res, speaker),
        })
        r.raise_for_status()
        data = r.json()
    except (httpx.HTTPError, ValueError):
        log.exception("агент не ответил")
        return await _answer(text, lang.t("fallback"), None, lim, error="agent")
    reply = data.get("reply", "")
    action = actions.validate(data.get("action"), modules.enabled())
    action, reply, lim = await enforce(action, reply, res, lim, device_id, playing, speaker)
    return await _answer(text, reply, action, lim)


async def enforce(action: dict | None, reply: str, res, lim: dict, device_id: str,
                  playing: tuple[str | None, str | None],
                  speaker: str | None) -> tuple[dict | None, str, dict]:
    """Страховка: режим и права говорящего важнее того, что решил агент (или быстрая команда) —
    и для нового, и для «продолжай»/перемотки уже загруженного."""
    if action and action["type"] in ("play", "resume", "seek"):
        if action["type"] == "play":
            item = {k: action.get(k) for k in ("source", "item_id", "tags", "age_min")}
        else:
            item = await playing_item(device_id, *playing)
        deny = policy.check_playback(res, item, policy.grants(speaker))
        if deny:
            action, reply = ({"type": "stop"} if action["type"] != "play" else None), deny
        else:
            policy.start_budget(res)
            lim = policy.limits(res)  # пересчитать stop_after_sec
            if item:   # дальше лимиты для Pi (/policy) считаются для того, кто включил
                now_playing[device_id] = {**now_playing.get(device_id, {}), **item, "speaker": speaker}
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
    name = audio.filename or "audio.wav"
    try:
        text, vp = await asyncio.gather(transcribe(data, name), voice_print(data, name))   # параллельно
    except (httpx.HTTPError, KeyError, ValueError):
        log.exception("STT не ответил")
        return await _answer("", lang.t("fallback"), None, policy.limits(policy.resolve(None)), error="stt")
    t_stt = time.monotonic()
    if not text:
        return {"heard": "", "reply": "", "speech_url": None, "action": None,
                "limits": policy.limits(policy.resolve(None))}
    speaker, score = identify_speaker(vp)
    utt = onboarding.Utterance(text=text, audio=data, vector=(vp or {}).get("vector"),
                               speech_sec=(vp or {}).get("speech_sec", 0.0), model=(vp or {}).get("model"))
    result = await handle_text(device_id, text, speaker, (src, now_playing or None), utt)
    result["speaker"] = speaker
    result["timing_ms"] = {"stt": int((t_stt - t0) * 1000),
                           "total": int((time.monotonic() - t0) * 1000)}
    log.info("ask %s: %r [%s %.2f] -> %r %s", device_id, text, speaker or "гость", score,
             result["reply"], result.get("action"))
    return result


@app.post("/text", dependencies=[Depends(check_token)])
async def ask_text(body: TextIn, x_admin_token: str | None = Header(default=None)):
    t0 = time.monotonic()
    speaker = body.speaker if is_admin(x_admin_token) else None
    src = body.source or (LEGACY_SOURCE if body.now_playing else None)
    result = await handle_text(body.device_id, body.text, speaker, (src, body.now_playing),
                               admin_verified=speaker is not None)
    result["timing_ms"] = {"total": int((time.monotonic() - t0) * 1000)}
    return result


@app.post("/reset", dependencies=[Depends(check_token)])
async def reset(body: ResetIn):
    try:
        r = await http.post(f"{AGENT_URL}/reset", json={"device_id": body.device_id})
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise HTTPException(502, "agent unavailable") from e
    return {"ok": True}


@app.post("/progress", dependencies=[Depends(check_token)])
async def progress(body: ProgressIn):
    """Pi раз в полминуты и при остановке сообщает позицию — чтобы потом продолжить с того места."""
    item_id = body.item_id or body.tale_id
    await save_progress(body.source or LEGACY_SOURCE, item_id, body.position, body.finished)
    return {"ok": True}


@app.get("/policy", dependencies=[Depends(check_token)])
async def get_policy(device_id: str | None = None):
    """Лимиты для того, кто включил то, что сейчас загружено на устройстве: сказку папы ночью
    не выключит, режим «для старшего» не глушит младшего. Не знаем, кто включил, — как для неузнанного."""
    speaker = (now_playing.get(device_id) or {}).get("speaker") if device_id else None
    return policy.limits(policy.resolve(speaker))


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
    # только файлы плеера: MCP и прочее у модуля наружу не отдаём
    if not base or ".." in path or not re.fullmatch(r"stream/[\w./-]+", path):
        raise HTTPException(404)
    headers = {h: v for h, v in request.headers.items() if h.lower() in ("range", "if-range")}
    upstream = http.build_request(request.method, f"{base}/{path}", headers=headers,
                                  timeout=httpx.Timeout(30, read=None))
    try:
        r = await http.send(upstream, stream=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"module {module} unavailable") from e
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
    return policy.add_override(body.mode, policy.now() + timedelta(minutes=minutes), body.who, "api")


@app.delete("/control/override", dependencies=[Depends(check_admin)])
async def admin_clear(who: str | None = None):
    removed = policy.relax([who] if who else None, "api")
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
