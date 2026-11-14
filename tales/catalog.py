"""Каталог сказок поверх Audiobookshelf: кэш в памяти, поиск, прогресс прослушивания.

Источник правды — библиотека Audiobookshelf (ABS). Раз в REFRESH_SEC секунд забираем список
книг, для новых/изменённых — подробности (файлы). В детский каталог попадают только книги
с описанием и без тега «скрыть»: по описанию Claude находит сказку по героям и теме.

Теги ABS:  обычные (автор, тема, «на ночь»…) + «возраст:6-12» — возрастной диапазон.
Многофайловые книги склеиваются ffmpeg в MERGED_DIR/<id>.mp3 — Pi играет один файл,
так проще перемотка и продолжение с места остановки.
"""
import json
import logging
import os
import random
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import httpx

log = logging.getLogger("catalog")

ABS_URL = os.getenv("ABS_URL", "http://audiobookshelf").rstrip("/")
ABS_API_KEY = os.getenv("ABS_API_KEY", "")
ABS_LIBRARY_ID = os.getenv("ABS_LIBRARY_ID", "")
ABS_LIBRARY_ROOT = os.getenv("ABS_LIBRARY_ROOT", "/audiobooks")   # путь библиотеки внутри ABS
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "/library"))          # та же папка у нас
MERGED_DIR = Path(os.getenv("MERGED_DIR", "/data/merged"))
STATE_FILE = Path(os.getenv("TALES_STATE", "/data/plays.json"))
REFRESH_SEC = int(os.getenv("CATALOG_REFRESH_SEC", "300"))
HIDE_TAG = "скрыть"
AGE_RE = re.compile(r"^возраст\s*:\s*(\d+)\s*-\s*(\d+)$")

# ---------------------------------------------------------------- поиск (русский, грубо)

STOPWORDS = {
    "сказка", "сказку", "сказки", "сказочку", "про", "для", "как", "что", "это",
    "включи", "поставь", "расскажи", "почитай", "давай", "хочу", "можно", "мне",
    "какую", "нибудь", "новую", "другую",
}
SHORT_STOPWORDS = {"на", "по", "не", "ну", "он", "мы", "вы", "ты", "же", "ли", "бы", "из", "за",
                   "от", "до", "со", "во", "ко", "то", "да", "их", "ее", "ей", "им", "но", "а", "и",
                   "в", "с", "к", "у", "о", "я"}
VOWELS = set("аеёиоуыэюяйь")


def _stem(word: str) -> str:
    """«колобка» → «колоб», «лису» → «лис», «зиму» → «зим». Согласную на конце короткого слова
    не трогаем: «волк» → «вол» нашёл бы «волшебство»."""
    w = word if len(word) <= 4 else word[: max(4, len(word) - 2)]
    while len(w) > 3 and w[-1] in VOWELS:
        w = w[:-1]
    return w


def stems_of(query: str) -> list[str]:
    words = re.findall(r"[\w-]+", query.lower().replace("ё", "е"))
    out = []
    for w in words:
        if w in STOPWORDS or w in SHORT_STOPWORDS:
            continue
        s = _stem(w).replace("-", "")
        out.append(s)
        if len(s) >= 4 and s[-2:] in ("ик", "ок", "ек"):   # «ежик» → «еж»
            out.append(s[:-2])
    return list(dict.fromkeys(s for s in out if len(s) >= 2))


def _norm(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


def score(tale: dict, stems: list[str]) -> int:
    """Совпадение с начала слова важнее совпадения внутри слова («лис» в «Василисе»)."""
    head = _norm(tale["title"] + " " + tale["tags"])
    body = _norm(tale["author"] + " " + tale["description"])
    total = 0
    for s in stems:
        start = re.compile(r"(?:^|[^\w])" + re.escape(s))
        if start.search(head):
            total += 8
        elif start.search(body):
            total += 4
        elif s in head:
            total += 2
        elif s in body:
            total += 1
    return total


# ---------------------------------------------------------------- состояние (сколько раз слушали)

class Plays:
    def __init__(self, path: Path) -> None:
        self.path, self.lock = path, threading.Lock()
        try:
            self.data = json.loads(path.read_text("utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            self.data = {}

    def get(self, tale_id: str) -> int:
        return int(self.data.get(tale_id, 0))

    def inc(self, tale_id: str) -> None:
        with self.lock:
            self.data[tale_id] = self.get(tale_id) + 1
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), "utf-8")
            tmp.replace(self.path)


# ---------------------------------------------------------------- каталог

class Catalog:
    def __init__(self) -> None:
        self.http = httpx.Client(base_url=ABS_URL, timeout=20,
                                 headers={"Authorization": f"Bearer {ABS_API_KEY}"})
        self.tales: dict[str, dict] = {}       # id -> сказка (только доступные ребёнку)
        self.details: dict[str, tuple] = {}    # id -> (updatedAt, files)
        self.skipped: list[str] = []
        self.loaded_at = 0.0
        self.lock = threading.Lock()
        self.plays = Plays(STATE_FILE)
        self.merging: set[str] = set()

    # --- загрузка из ABS

    def _files(self, item_id: str, updated) -> list[tuple[str, float]]:
        cached = self.details.get(item_id)
        if cached and cached[0] == updated:
            return cached[1]
        r = self.http.get(f"/api/items/{item_id}", params={"expanded": 1})
        r.raise_for_status()
        d = r.json()
        files = []
        for f in sorted(d["media"].get("audioFiles", []), key=lambda f: f.get("index") or 0):
            if f.get("exclude"):
                continue
            path = f["metadata"]["path"]
            if not path.startswith(ABS_LIBRARY_ROOT + "/"):
                continue
            files.append((path[len(ABS_LIBRARY_ROOT) + 1:], float(f.get("duration") or 0)))
        self.details[item_id] = (updated, files)
        return files

    def refresh(self) -> None:
        r = self.http.get(f"/api/libraries/{ABS_LIBRARY_ID}/items", params={"minified": 0, "limit": 0})
        r.raise_for_status()
        tales, skipped = {}, []
        for it in r.json()["results"]:
            m = it["media"]
            md = m["metadata"]
            title = (md.get("title") or "").strip()
            raw_tags = [t.strip() for t in m.get("tags") or [] if t.strip()]
            if HIDE_TAG in [t.lower() for t in raw_tags] or it.get("isMissing") or it.get("isInvalid"):
                continue
            desc = re.sub(r"<[^>]+>", " ", md.get("description") or "").strip()   # ABS хранит HTML
            if not title or not desc:
                skipped.append(title or it["id"])
                continue
            age_min = age_max = None
            tags = []
            for t in raw_tags:
                if m_age := AGE_RE.match(t.lower()):
                    age_min, age_max = int(m_age.group(1)), int(m_age.group(2))
                else:
                    tags.append(t)
            author = md.get("authorName") or ", ".join(a["name"] for a in md.get("authors") or [])
            files = self._files(it["id"], it.get("updatedAt"))
            tale = {
                "id": it["id"], "title": title, "author": author, "description": desc,
                "tags": ", ".join(tags), "age_min": age_min, "age_max": age_max,
                "duration_sec": int(m.get("duration") or sum(d for _, d in files)) or None,
                "files": files,
            }
            tales[it["id"]] = tale
        with self.lock:
            self.tales, self.skipped, self.loaded_at = tales, skipped, time.time()
        log.info("каталог: %d сказок, без описания пропущено %d", len(tales), len(skipped))
        self._merge_pending()

    def ensure_fresh(self) -> None:
        if time.time() - self.loaded_at > REFRESH_SEC:
            try:
                self.refresh()
            except Exception:  # noqa: BLE001
                log.exception("не удалось обновить каталог — работаю со старым")

    # --- склейка многофайловых книг

    def _merged_path(self, tale: dict) -> Path:
        return MERGED_DIR / f"{tale['id']}.mp3"

    def _merge_pending(self) -> None:
        todo = [t for t in self.tales.values()
                if len(t["files"]) > 1 and not self._merged_path(t).exists() and t["id"] not in self.merging]
        if todo:
            threading.Thread(target=self._merge_all, args=(todo,), daemon=True).start()

    def _merge_all(self, tales: list[dict]) -> None:
        MERGED_DIR.mkdir(parents=True, exist_ok=True)
        for t in tales:
            self.merging.add(t["id"])
            out = self._merged_path(t)
            try:
                with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as lst:
                    for rel, _ in t["files"]:
                        lst.write("file '" + str(LIBRARY_DIR / rel).replace("'", "'\\''") + "'\n")
                tmp = out.with_suffix(".part.mp3")
                subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
                                "-i", lst.name, "-map", "0:a", "-c", "copy", "-map_metadata", "-1", str(tmp)],
                               check=True, timeout=1800)
                tmp.replace(out)
                log.info("склеено: %s (%d файлов)", t["title"], len(t["files"]))
            except Exception:  # noqa: BLE001
                log.exception("не удалось склеить %s", t["title"])
            finally:
                Path(lst.name).unlink(missing_ok=True)
                self.merging.discard(t["id"])

    # --- для инструментов и шлюза

    def media_path(self, tale: dict) -> str | None:
        """Путь для шлюза: library/<rel> или merged/<id>.mp3. None — пока нечего играть."""
        if not tale["files"]:
            return None
        if len(tale["files"]) == 1:
            return "media/" + tale["files"][0][0]
        return "merged/" + f"{tale['id']}.mp3" if self._merged_path(tale).exists() else None

    def public(self, tale: dict) -> dict:
        d = {k: tale[k] for k in ("id", "title", "author", "description", "tags",
                                  "age_min", "age_max", "duration_sec")}
        if tale["duration_sec"]:
            d["minutes"] = round(tale["duration_sec"] / 60)
        d["has_audio"] = self.media_path(tale) is not None
        return d

    def _filtered(self, age, max_minutes, tag) -> list[dict]:
        self.ensure_fresh()
        out = []
        for t in self.tales.values():
            # возраст фильтрует только снизу («рано»: «Премудрый пискарь» пятилетке).
            # Верхняя граница — «может быть скучно», а не запрет: восьмилетний просит «Мойдодыра» — включаем.
            if age is not None and (t["age_min"] or 0) > age:
                continue
            if max_minutes is not None and t["duration_sec"] and t["duration_sec"] > max_minutes * 60:
                continue
            if tag and _norm(tag) not in _norm(t["tags"]):
                continue
            out.append(t)
        return out

    def search(self, query: str = "", age=None, max_minutes=None, limit: int = 5) -> list[dict]:
        limit = max(1, min(int(limit), 20))
        pool = self._filtered(age, max_minutes, None)
        stems = stems_of(query or "")
        if not stems:
            pool.sort(key=lambda t: (-self.plays.get(t["id"]), t["title"]))
            return [self.public(t) for t in pool[:limit]]
        scored = [(score(t, stems), t) for t in pool]
        scored = [(s, t) for s, t in scored if s > 0]
        scored.sort(key=lambda st: (-st[0], st[1]["title"]))
        return [self.public(t) for _, t in scored[:limit]]

    def random(self, tag=None, age=None, max_minutes=None) -> dict | None:
        pool = [t for t in self._filtered(age, max_minutes, tag) if self.media_path(t)]
        if not pool:
            return None
        # реже слушанные выпадают чаще
        weights = [1 / (1 + self.plays.get(t["id"])) for t in pool]
        return random.choices(pool, weights=weights)[0]

    def get(self, tale_id: str) -> dict | None:
        self.ensure_fresh()
        return self.tales.get(str(tale_id))

    def list_tags(self) -> list[str]:
        self.ensure_fresh()
        return sorted({tg.strip().lower() for t in self.tales.values() for tg in t["tags"].split(",") if tg.strip()})

    # --- прогресс прослушивания (пользователь колонки в ABS)

    def progress(self, tale_id: str) -> dict | None:
        r = self.http.get(f"/api/me/progress/{tale_id}")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()

    def resume_from(self, tale: dict) -> int | None:
        """С какой секунды продолжать: есть прогресс, не дослушано, не в самом начале/конце."""
        try:
            p = self.progress(tale["id"])
        except httpx.HTTPError:
            log.exception("прогресс недоступен")
            return None
        if not p or p.get("isFinished"):
            return None
        t, dur = float(p.get("currentTime") or 0), float(tale["duration_sec"] or p.get("duration") or 0)
        if t < 30 or (dur and t > dur - 30):
            return None
        return int(t)

    def save_progress(self, tale_id: str, position: float, finished: bool = False) -> None:
        tale = self.get(tale_id)
        dur = float((tale or {}).get("duration_sec") or 0)
        body = {"currentTime": max(0.0, float(position)), "isFinished": bool(finished)}
        if dur:
            body["duration"] = dur
            body["progress"] = 1.0 if finished else min(1.0, body["currentTime"] / dur)
        r = self.http.patch(f"/api/me/progress/{tale_id}", json=body)
        r.raise_for_status()


catalog = Catalog()
