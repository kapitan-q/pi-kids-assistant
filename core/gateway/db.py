"""Данные приложения: data/core/speaker.db (SQLite). Правятся только через /admin и голосом — не руками.

Пользователи, роли и их права, временные режимы, настройки, служебные отметки, журнал.
Версия схемы — PRAGMA user_version; миграции — список MIGRATIONS (только дописывать в конец).
При первом запуске импортируются прежние profiles.json / roles.json / state.json (→ *.migrated).
Голоса (wav, эмбеддинги) остаются файлами в data/core/voices/.
"""
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

log = logging.getLogger("db")
DATA_DIR = Path(os.getenv("CORE_DATA_DIR", "/data/core"))
DB_PATH = Path(os.getenv("CORE_DB", DATA_DIR / "speaker.db"))
STATE_PATH = Path(os.getenv("STATE_FILE", "/data/state/state.json"))   # только для импорта

MIGRATIONS = [
    """
    CREATE TABLE user (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        birth_date TEXT,
        aliases TEXT NOT NULL DEFAULT '[]',
        voice_samples INTEGER NOT NULL DEFAULT 0,
        voice_updated REAL,
        created_at REAL NOT NULL,
        last_seen REAL
    );
    CREATE TABLE role (
        id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        created_at REAL NOT NULL
    );
    CREATE TABLE role_right (
        role_id TEXT NOT NULL REFERENCES role(id) ON DELETE CASCADE,
        right_id TEXT NOT NULL,
        effect TEXT NOT NULL CHECK (effect IN ('allow', 'deny')),
        vals TEXT NOT NULL DEFAULT '[]',
        PRIMARY KEY (role_id, right_id, effect)
    );
    CREATE TABLE user_role (
        user_id TEXT NOT NULL REFERENCES user(id) ON DELETE CASCADE,
        role_id TEXT NOT NULL REFERENCES role(id) ON DELETE CASCADE,
        PRIMARY KEY (user_id, role_id)
    );
    CREATE TABLE override (
        id TEXT PRIMARY KEY,
        mode TEXT NOT NULL,
        until_ts REAL NOT NULL,
        who TEXT,
        created_at REAL NOT NULL
    );
    CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE mark (ns TEXT NOT NULL, key TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (ns, key));
    CREATE TABLE audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts REAL NOT NULL,
        actor TEXT,
        via TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    );
    """,
]

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def conn() -> sqlite3.Connection:
    global _conn
    with _lock:
        if _conn is None:
            DB_PATH.parent.mkdir(parents=True, exist_ok=True)
            c = sqlite3.connect(DB_PATH, check_same_thread=False, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA synchronous=FULL")
            _migrate(c)
            _conn = c
        return _conn


def _migrate(c: sqlite3.Connection) -> None:
    version = c.execute("PRAGMA user_version").fetchone()[0]
    fresh = version == 0
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        c.execute("BEGIN")
        try:   # не executescript: он сам делает COMMIT посреди транзакции
            for stmt in filter(str.strip, sql.split(";")):
                c.execute(stmt)
            c.execute(f"PRAGMA user_version={i}")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        log.info("схема БД → версия %d", i)
    if fresh:
        _import_json(c)
    _defaults(c, fresh)


def _enabled_modules() -> list[str]:
    try:
        import modules
        return list(modules.enabled())
    except Exception:  # noqa: BLE001 — без modules.yaml (тесты)
        return []


def _defaults(c: sqlite3.Connection, fresh: bool) -> None:
    """Гость есть всегда; в новой установке без ролей — ещё «Родитель» со всеми правами ядра."""
    now = time.time()
    if not c.execute("SELECT 1 FROM role WHERE id='guest'").fetchone():
        c.execute("INSERT INTO role(id, title, created_at) VALUES('guest', 'Гость', 0)")
        for m in _enabled_modules():
            c.execute("INSERT INTO role_right(role_id, right_id, effect) VALUES('guest', ?, 'allow')", (m,))
    c.execute("DELETE FROM role_right WHERE role_id='guest' AND right_id='core.control'")
    if fresh and c.execute("SELECT count(*) FROM role").fetchone()[0] == 1:
        c.execute("INSERT INTO role(id, title, created_at) VALUES('parent', 'Родитель', ?)", (now,))
        for r in ["core.control", "core.modes_exempt", "core.any_age", *_enabled_modules()]:
            c.execute("INSERT INTO role_right(role_id, right_id, effect) VALUES('parent', ?, 'allow')", (r,))


@contextlib.contextmanager
def tx():
    """Транзакция: всё или ничего (объединение профилей, удаление роли и т.п.)."""
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise


def q(sql: str, args=()) -> list[sqlite3.Row]:
    with _lock:
        return conn().execute(sql, args).fetchall()


def q1(sql: str, args=()) -> sqlite3.Row | None:
    rows = q(sql, args)
    return rows[0] if rows else None


def ex(sql: str, args=()) -> int:
    """Одна команда вне явной транзакции (SQLite сам делает её атомарной). Возвращает rowcount."""
    with _lock:
        return conn().execute(sql, args).rowcount


# ---------- настройки, отметки, журнал ----------

def setting(key: str, default=None):
    row = q1("SELECT value FROM setting WHERE key=?", (key,))
    return json.loads(row["value"]) if row else default


def set_setting(key: str, value) -> None:
    ex("INSERT INTO setting(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
       (key, json.dumps(value, ensure_ascii=False)))


def mark_get(ns: str, key: str) -> float | None:
    row = q1("SELECT ts FROM mark WHERE ns=? AND key=?", (ns, key))
    return row["ts"] if row else None


def mark_set_once(ns: str, key: str, ts: float | None = None) -> bool:
    """Поставить отметку, если её ещё нет. True — поставили сейчас."""
    return ex("INSERT OR IGNORE INTO mark(ns, key, ts) VALUES(?, ?, ?)", (ns, key, ts or time.time())) > 0


def mark_prune(max_age_sec: float) -> None:
    ex("DELETE FROM mark WHERE ts < ?", (time.time() - max_age_sec,))


def audit(via: str, action: str, actor: str | None = None, **detail) -> None:
    """Журнал: кто, через что (voice/admin/api) и что сделал с режимами, ролями, людьми."""
    try:
        ex("INSERT INTO audit(ts, actor, via, action, detail) VALUES(?, ?, ?, ?, ?)",
           (time.time(), actor, via, action, json.dumps(detail, ensure_ascii=False, default=str)))
        ex("DELETE FROM audit WHERE ts < ?", (time.time() - 365 * 86400,))
    except sqlite3.Error:
        log.exception("журнал не записан")


def audit_tail(limit: int = 50) -> list[dict]:
    rows = q("SELECT ts, actor, via, action, detail FROM audit ORDER BY id DESC LIMIT ?", (limit,))
    return [{**dict(r), "detail": json.loads(r["detail"] or "{}")} for r in rows]


# ---------- импорт прежних JSON ----------

def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text("utf-8"))
    except ValueError:
        log.exception("%s повреждён — не импортирую (файл не трогаю)", path)
        return None


def birth_from_age(age: int | None) -> str | None:
    """Возраст → дата рождения (1 января): возраст дальше считается сам и не устаревает."""
    return date(date.today().year - int(age), 1, 1).isoformat() if age else None


def _import_json(c: sqlite3.Connection) -> None:
    profiles_p, roles_p = DATA_DIR / "profiles.json", DATA_DIR / "roles.json"
    profiles, roles, state = _read_json(profiles_p), _read_json(roles_p), _read_json(STATE_PATH)
    if not (profiles or roles or state):
        return
    c.execute("BEGIN")
    try:
        for r in (roles or {}).get("roles", []):
            c.execute("INSERT INTO role(id, title, created_at) VALUES(?, ?, ?)",
                      (r["id"], r.get("title") or r["id"], r.get("created_at") or time.time()))
            allow = {m: [] for m in r.get("allow_modules") or []}
            deny = {m: [] for m in r.get("deny_modules") or []}
            if r.get("control") or r.get("parent"):
                allow.update({"core.control": [], "core.modes_exempt": []})
            if r.get("deny_tags"):
                for m in r.get("allow_modules") or []:
                    deny[f"{m}.tags"] = list(r["deny_tags"])
            for effect, rights in (("allow", allow), ("deny", deny)):
                for right, vals in rights.items():
                    c.execute("INSERT INTO role_right(role_id, right_id, effect, vals) VALUES(?, ?, ?, ?)",
                              (r["id"], right, effect, json.dumps(vals, ensure_ascii=False)))
        known_roles = {row[0] for row in c.execute("SELECT id FROM role")}
        for p in (profiles or {}).get("profiles", []):
            voice = p.get("voice") or {}
            c.execute("INSERT INTO user(id, name, birth_date, aliases, voice_samples, voice_updated, created_at,"
                      " last_seen) VALUES(?, ?, ?, ?, ?, ?, ?, ?)",
                      (p["id"], p["name"], birth_from_age(p.get("age")),
                       json.dumps(p.get("aliases") or [], ensure_ascii=False), voice.get("samples") or 0,
                       voice.get("updated_at"), p.get("created_at") or time.time(), p.get("last_seen")))
            roles_of = p.get("roles") or ([p["role"]] if p.get("role") else [])
            for rid in roles_of:
                if rid in known_roles and rid != "guest":
                    c.execute("INSERT INTO user_role(user_id, role_id) VALUES(?, ?)", (p["id"], rid))
        state = state or {}
        for o in state.get("overrides") or []:
            if o.get("until_ts", 0) > time.time():
                c.execute("INSERT INTO override(id, mode, until_ts, who, created_at) VALUES(?, ?, ?, ?, ?)",
                          (o["id"], o["mode"], o["until_ts"],
                           json.dumps(o["who"], ensure_ascii=False) if o.get("who") else None, time.time()))
        for ns in ("announced", "budgets"):
            for k, ts in (state.get(ns) or {}).items():
                c.execute("INSERT OR IGNORE INTO mark(ns, key, ts) VALUES(?, ?, ?)", (ns, k, ts))
        if "onboarding_enabled" in state:
            c.execute("INSERT INTO setting(key, value) VALUES('onboarding_enabled', ?)",
                      (json.dumps(bool(state["onboarding_enabled"])),))
        c.execute("COMMIT")
    except Exception:
        c.execute("ROLLBACK")
        log.exception("импорт JSON не удался — БД пустая, исходные файлы не тронуты")
        raise
    for path in (profiles_p, roles_p, STATE_PATH):
        if path.exists():
            path.rename(path.with_name(path.name + ".migrated"))
    log.info("импортировано из JSON: людей %d, ролей %d",
             len((profiles or {}).get("profiles", [])), len((roles or {}).get("roles", [])))


def reset_for_tests() -> None:
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
