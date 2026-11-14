#!/usr/bin/env python3
"""Регрессия поведения агента: прогнать реальные фразы через /text и сравнить с эталоном.

    python3 scripts/replay_phrases.py run  data/replay-before.json   # снять результаты
    python3 scripts/replay_phrases.py diff data/replay-before.json data/replay-after.json

Фразы — уникальные «heard» из data/logs/*.jsonl (без служебных проверок) + несколько своих.
Каждая фраза — с чистого разговора (/reset), режим — обычный (/control/override снимается).
Ответы Claude недетерминированы: сравниваем тип команды и название, а не текст.
"""
import base64
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXTRA = ["включи сказку про колобка", "включи любую сказку", "давай другую сказку",
         "что ты умеешь?", "включи сказку на ночь", "расскажи про динозавров"]
SKIP = {"стоп", "продолжай", "сделай потише", "громкость пятьдесят"}   # без играющей сказки не показательны


def env() -> dict:
    return dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines()
                if "=" in l and not l.startswith("#"))


def call(method: str, path: str, body=None, admin=False):
    e = env()
    req = urllib.request.Request("https://" + e["KS_HOST"] + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None)
    req.add_header("Content-Type", "application/json")
    pw = (ROOT / ".speaker-password").read_text().strip()
    req.add_header("Authorization", "Basic " + base64.b64encode(f"speaker:{pw}".encode()).decode())
    if admin:
        req.add_header("X-Admin-Token", e.get("ADMIN_TOKEN", "").strip("'\""))
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read() or b"null")
    except urllib.error.HTTPError as err:
        return {"http_error": err.code}


def phrases() -> list[str]:
    seen: dict[str, None] = {}
    for f in sorted((ROOT / "data/logs").glob("*.jsonl")):
        for line in f.read_text("utf-8").splitlines():
            h = json.loads(line).get("heard", "").strip()
            if h and h.lower() not in SKIP:
                seen.setdefault(h)
    for p in EXTRA:
        seen.setdefault(p)
    return list(seen)


def run(out: Path) -> None:
    call("DELETE", "/control/override", admin=True)
    results = {}
    for p in phrases():
        call("POST", "/reset", {"device_id": "test"})
        d = call("POST", "/text", {"device_id": "test", "text": p})
        act = d.get("action") or {}
        results[p] = {"type": act.get("type"), "title": act.get("title"), "reply": d.get("reply")}
        print(f"{p!r:50} -> {act.get('type')} {act.get('title') or ''}")
    call("POST", "/reset", {"device_id": "test"})
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), "utf-8")
    print(f"\n{len(results)} фраз → {out}")


def diff(a: Path, b: Path) -> None:
    before, after = json.loads(a.read_text("utf-8")), json.loads(b.read_text("utf-8"))
    changed = 0
    for p in before:
        x, y = before[p], after.get(p, {})
        if (x["type"], x["title"]) != (y.get("type"), y.get("title")):
            changed += 1
            print(f"≠ {p!r}\n   было:  {x['type']} {x['title'] or ''} | {x['reply']!r}\n"
                  f"   стало: {y.get('type')} {y.get('title') or ''} | {y.get('reply')!r}")
    print(f"\nРасхождений: {changed} из {len(before)} (случайные сказки и уточняющие вопросы — норма)")


if __name__ == "__main__":
    if sys.argv[1:2] == ["run"]:
        run(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["diff"]:
        diff(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        sys.exit(__doc__)
