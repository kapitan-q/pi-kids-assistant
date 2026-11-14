#!/usr/bin/env python3
"""Сквозная проверка сценариев через /text: что услышал → что ответил → какую команду получит Pi.

Запуск на сервере:  python3 scripts/smoke_test.py
(логин/пароль и ADMIN_TOKEN берутся из .speaker-password и .env; URL — из KS_HOST)
Работает на настоящей библиотеке: нужны «Мойдодыр» (тег «на ночь») и «Сказка о рыбаке и рыбке» (без него).
Ответы Claude недетерминированы — проверяем тип и параметры команды, а не текст.
"""
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
URL = "https://" + ENV["KS_HOST"]
AUTH = "Basic " + base64.b64encode(f"speaker:{(ROOT / '.speaker-password').read_text().strip()}".encode()).decode()
ADMIN = ENV.get("ADMIN_TOKEN", "")
DEVICE = "test"
results: list[tuple[bool, str]] = []


def call(method: str, path: str, body=None, admin=False, auth=True):
    req = urllib.request.Request(URL + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    if auth:
        req.add_header("Authorization", AUTH)
    if admin:
        req.add_header("X-Admin-Token", ADMIN)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as e:
        return e.code, None


def say(text: str, now_playing=None) -> dict:
    t0 = time.monotonic()
    _, d = call("POST", "/text", {"device_id": DEVICE, "text": text, "source": "books" if now_playing else None,
                                  "now_playing": str(now_playing) if now_playing else None})
    d["_ms"] = int((time.monotonic() - t0) * 1000)
    return d


def check(name: str, ok: bool, d: dict | None = None) -> None:
    results.append((ok, name))
    act = json.dumps(d.get("action"), ensure_ascii=False) if d else ""
    reply = (d or {}).get("reply", "")
    ms = (d or {}).get("_ms", "")
    print(f"{'✅' if ok else '❌'} {name}\n     ответ: {reply!r}\n     action: {act}  ({ms} мс)")


def kind(d):
    return (d.get("action") or {}).get("type")


def section(title):
    print(f"\n=== {title}")


# ---------------------------------------------------------------- доступ
section("Доступ")
check("без пароля — 401", call("GET", "/health", auth=False)[0] == 401)
check("админка без токена — 401", call("GET", "/control/status")[0] == 401)
st, h = call("GET", "/health")
check(f"ядро живо {h and h.get('core')}", st == 200 and h["ok"])
check(f"модули живы {h and h.get('modules')}", st == 200 and all((h.get("modules") or {"-": False}).values()))
call("DELETE", "/control/override", admin=True)          # обычный режим, даже если по расписанию ночь
call("POST", "/reset", {"device_id": DEVICE})

# ---------------------------------------------------------------- обычный режим
section("Обычный режим")
d = say("включи мойдодыра")
a = d.get("action") or {}
check("«включи мойдодыра» → play Мойдодыр", kind(d) == "play" and a.get("title") == "Мойдодыр", d)
kolobok_id = a.get("item_id")
check("play: source=books, url модуля, компат-поле tale_id", a.get("source") == "books"
      and "/m/books/stream/" in a.get("url", "") and a.get("tale_id") == kolobok_id, d)
if a.get("url"):
    req = urllib.request.Request(a["url"], headers={"Authorization": AUTH, "Range": "bytes=0-99"})
    with urllib.request.urlopen(req, timeout=10) as r:
        check("mp3 по url: с basic-auth Range → 206", r.status == 206 and len(r.read()) == 100)
    try:
        urllib.request.urlopen(urllib.request.Request(a["url"], method="HEAD"), timeout=10)
        check("mp3 без basic-auth → 401", False)
    except urllib.error.HTTPError as e:
        check("mp3 без basic-auth → 401", e.code == 401)
if d.get("speech_url"):
    req = urllib.request.Request(d["speech_url"], headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=10) as r:
        check("озвученный ответ — wav", r.status == 200 and r.headers.get_content_type() == "audio/wav")

d = say("стоп", kolobok_id)
check("«стоп» → stop", kind(d) == "stop", d)
d = say("продолжай", kolobok_id)
check("«продолжай» → resume", kind(d) == "resume", d)
d = say("перемотай немного вперёд", kolobok_id)
a = d.get("action") or {}
check("«перемотай вперёд» → seek +N relative", kind(d) == "seek" and a.get("mode") == "relative" and a.get("seconds", 0) > 0, d)
d = say("давай с самого начала", kolobok_id)
a = d.get("action") or {}
check("«с самого начала» → seek 0 absolute", kind(d) == "seek" and a.get("mode") == "absolute" and a.get("seconds") == 0, d)
d = say("сделай потише", kolobok_id)
a = d.get("action") or {}
check("«потише» → volume change < 0", kind(d) == "volume" and (a.get("change") or 0) < 0, d)
d = say("громкость пятьдесят", kolobok_id)
check("«громкость пятьдесят» → volume level 50", kind(d) == "volume" and (d.get("action") or {}).get("level") == 50, d)
d = say("включи сказку о рыбаке и рыбке")
rybka_id = (d.get("action") or {}).get("item_id")
check("«о рыбаке и рыбке» → play", kind(d) == "play" and "рыб" in (d.get("action") or {}).get("title", "").lower(), d)
call("POST", "/progress", {"source": "books", "item_id": rybka_id, "position": 300})   # «остановились» на 5-й минуте
d = say("включи сказку о рыбаке и рыбке")
a = d.get("action") or {}
check("повторно — продолжает с того места (start ≈ 300)", kind(d) == "play" and 290 <= (a.get("start") or 0) <= 310, d)
call("POST", "/progress", {"tale_id": rybka_id, "position": 0})   # вернуть в начало (старый формат Pi)
d = say("почему небо голубое?")
check("вопрос «почему небо голубое» → ответ без команды", kind(d) is None and len(d.get("reply", "")) > 10, d)
d = say("включи любую сказку")
check("«любую сказку» → play", kind(d) == "play", d)

# ---------------------------------------------------------------- быстрые команды
section("Быстрые команды (без Claude)")
for phrase, want in [("стоп", "stop"), ("продолжай", "resume"), ("сделай потише", "volume"),
                     ("громкость семьдесят", "volume"), ("перемотай назад", "seek")]:
    d = say(phrase, kolobok_id)
    check(f"«{phrase}» → {want} мгновенно", d.get("quick") and kind(d) == want and d["_ms"] < 1500, d)
d = say("стоп а почему мойдодыр сердится")
check("сложная фраза уходит к Claude", not d.get("quick") and d.get("reply"), d)

# ---------------------------------------------------------------- ночь
section("Ночной режим (команда админки)")
call("POST", "/control/override", {"mode": "night", "minutes": 10}, admin=True)
call("POST", "/reset", {"device_id": DEVICE})
d = say("включи сказку про рыбака и рыбку")
a = d.get("action") or {}
check("ночью «рыбка» (не на ночь) → не включается", kind(d) is None or "на ночь" in a.get("tags", []), d)
d = say("включи мойдодыра")
lim = d.get("limits") or {}
check("ночью «мойдодыр» (на ночь) → play + таймер", kind(d) == "play" and lim.get("stop_after_sec") and lim.get("max_volume") == 30, d)
d = say("сделай громкость на максимум", kolobok_id)
a = d.get("action") or {}
check("ночью «на максимум» → не выше 30", kind(d) == "volume" and (a.get("level") is None or a.get("level") <= 30), d)

d = say("продолжай", rybka_id)   # сейчас загружена «Рыбка» (без тега «на ночь»)
check("ночью «продолжай» не-ночную сказку → stop", kind(d) in ("stop", None) and kind(d) != "resume", d)

# ---------------------------------------------------------------- тихий час
section("Тихий час")
call("POST", "/control/override", {"mode": "quiet", "minutes": 10}, admin=True)
d = say("включи мойдодыра")
check("в тихий час → blocked + stop + сообщение", d.get("blocked") and kind(d) == "stop" and d.get("reply"), d)
d = say("ну пожалуйста включи")
check("повторно → молчит (reply пустой)", d.get("blocked") and d.get("reply") == "", d)
d = say("Капитан Немо, включи мойдодыра")
check("кодовая фраза не обходит блок", kind(d) is None and d.get("parent_command"), d)

# ---------------------------------------------------------------- родитель
section("Родительские команды голосом")
call("DELETE", "/control/override", admin=True)
d = say("Капитан Немо, тихий час на сорок пять минут")
check("«…тихий час на сорок пять минут» → режим включён", d.get("parent_command") and d["limits"]["mode"] == "quiet", d)
d = say("Капитан Немо, отбой тишины")
check("кодовой фразой без права снимать — нельзя", "телефон" in d.get("reply", "") and d["limits"]["mode"] == "quiet", d)

call("DELETE", "/control/override", admin=True)
call("POST", "/reset", {"device_id": DEVICE})
failed = [n for ok, n in results if not ok]
print(f"\nИтого: {len(results) - len(failed)}/{len(results)} OK" + ("" if not failed else "\nНе прошло:\n  - " + "\n  - ".join(failed)))
sys.exit(1 if failed else 0)
