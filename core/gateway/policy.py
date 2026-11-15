"""Режимы, расписание, права говорящего, временные команды родителя и лимиты для Pi.

Приоритет режима: временная команда > расписание > default_mode.
Режимы (config/speaker.yaml) — «когда и для кого»; права (rights.py, роли в /admin) — «что можно».
Тот, у кого есть право core.modes_exempt, режимам не подчиняется.
Временные команды, отметки «уже предупредили» и бюджеты ночных сказок — в БД (db.py).
"""
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import db
import lang
import modules
import profiles
import rights

log = logging.getLogger("policy")

CONFIG_PATH = Path(os.getenv("POLICY_FILE", "/config/speaker.yaml"))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CONFIG = {"modes": {"normal": {}}, "schedule": [{"mode": "normal"}]}
MARK_TTL_SEC = 2 * 86400


# ---------- конфиг с горячей перезагрузкой ----------

_cfg: dict | None = None
_cfg_mtime = 0.0


def config() -> dict:
    global _cfg, _cfg_mtime
    try:
        mtime = CONFIG_PATH.stat().st_mtime
    except FileNotFoundError:
        return _cfg or DEFAULT_CONFIG
    if _cfg is None or mtime != _cfg_mtime:
        try:
            _cfg = yaml.safe_load(CONFIG_PATH.read_text("utf-8")) or {}
            _cfg_mtime = mtime
            log.info("%s загружен", CONFIG_PATH.name)
        except yaml.YAMLError:
            log.exception("ошибка в %s — оставляю прежний конфиг", CONFIG_PATH.name)
            _cfg = _cfg or DEFAULT_CONFIG
    return _cfg


def tz() -> ZoneInfo:
    """Часовой пояс — TZ контейнера; в yaml только если нужно переопределить."""
    return ZoneInfo(config().get("timezone") or os.getenv("TZ") or "UTC")


def now() -> datetime:
    return datetime.now(tz())


def default_mode() -> str:
    return config().get("default_mode", "normal")


def mode_conf(name: str) -> dict:
    return (config().get("modes") or {}).get(name, {})


# ---------- кто говорит и что ему можно ----------

def user(speaker: str | None) -> profiles.User | None:
    return profiles.store.get(speaker)


def profile(speaker: str | None) -> dict:
    """Пользователь как dict ({} — не узнан или нет такого): name, age, aliases, roles…"""
    u = user(speaker)
    return u.view() if u else {}


def role_ids(speaker: str | None) -> list[str]:
    """Роли говорящего; нет ролей или голос не узнан — [guest]."""
    u = user(speaker)
    ids = [r for r in (u.roles if u else []) if r != profiles.GUEST]
    return ids or [profiles.GUEST]


def unverified(speaker: str | None) -> bool:
    """Личность не подтверждена родителем: голос не узнан или ролей нет (мог назваться чужим именем)."""
    return role_ids(speaker) == [profiles.GUEST]


def grants(speaker: str | None) -> rights.Grants:
    roles = [r for r in map(profiles.roles.get, role_ids(speaker)) if r]
    u = user(speaker)
    g = rights.effective([{"allow": r.allow, "deny": r.deny} for r in roles], u.age if u else None)
    if unverified(speaker):
        g.flags -= rights.GUEST_FORBIDDEN
    return g


def can_control(speaker: str | None) -> bool:
    """Может ставить и снимать ограничения голосом (право core.control)."""
    return bool(speaker) and grants(speaker).has(rights.CONTROL)


def modes_exempt(speaker: str | None) -> bool:
    return bool(speaker) and not unverified(speaker) and grants(speaker).has(rights.MODES_EXEMPT)


def targets() -> list[str]:
    """id пользователей, на которых действуют режимы (все, кроме имеющих core.modes_exempt)."""
    return [u.id for u in profiles.store.all() if not modes_exempt(u.id)]


def guest_enabled() -> bool:
    """Гостевой доступ: неузнанный голос и люди без ролей пользуются правами роли «Гость».
    Выключен — колонка им отвечает «не узнала» (кроме команд плееру и знакомства)."""
    return bool(db.setting("guest_enabled", True))


def onboarding_enabled() -> bool:
    """Можно ли знакомиться с колонкой (новые пользователи). Переключается в /admin."""
    return bool(db.setting("onboarding_enabled", True))


def set_setting(key: str, value: bool) -> None:
    db.set_setting(key, bool(value))


def speaker_matches(who: list | None, speaker: str | None, mode: str | None = None) -> bool:
    """who в правилах и командах — id пользователей и/или ролей.
    Неподтверждённого говорящего (unknown_speaker_matches) касаются все ограничения с who —
    но не послабления: «обычный режим для старшего» на неузнанного не распространяется."""
    if not who:
        return True
    if (speaker is not None and speaker in who) or set(role_ids(speaker)) & set(who):
        return True
    if not unverified(speaker) or mode == default_mode():
        return False
    return bool(config().get("unknown_speaker_matches", True))


# ---------- временные команды (БД) ----------

def overrides() -> list[dict]:
    ts = time.time()
    db.ex("DELETE FROM override WHERE until_ts <= ?", (ts,))
    db.mark_prune(MARK_TTL_SEC)
    rows = db.q("SELECT * FROM override ORDER BY created_at")
    return [{"id": r["id"], "mode": r["mode"], "until_ts": r["until_ts"],
             "until": datetime.fromtimestamp(r["until_ts"], tz()).isoformat(timespec="minutes"),
             "who": json.loads(r["who"]) if r["who"] else None} for r in rows]


# ---------- расписание ----------

def expand_days(spec) -> set[int]:
    if not spec:
        return set(range(7))
    result: set[int] = set()
    for item in spec:
        item = str(item).lower().strip()
        if item in ("daily", "all", "*"):
            return set(range(7))
        if "-" in item:
            a, b = (DAYS.index(x.strip()) for x in item.split("-"))
            i = a
            while True:
                result.add(i)
                if i == b:
                    break
                i = (i + 1) % 7
        else:
            result.add(DAYS.index(item))
    return result


def _at(day: datetime, hhmm: str) -> datetime:
    h, m = (int(x) for x in hhmm.split(":"))
    return day.replace(hour=h, minute=m, second=0, microsecond=0)


def rule_window(rule: dict, t: datetime) -> tuple[datetime, datetime] | None:
    """Если правило активно в момент t — (начало, конец) текущего окна, иначе None."""
    days = expand_days(rule.get("days"))
    if not rule.get("from") or not rule.get("to"):
        if t.weekday() not in days:
            return None
        start = t.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    start, end = _at(t, rule["from"]), _at(t, rule["to"])
    if start < end:  # обычное окно внутри дня
        if t.weekday() in days and start <= t < end:
            return start, end
        return None
    # окно через полночь, напр. 21:30 → 07:00
    if t >= start and t.weekday() in days:
        return start, end + timedelta(days=1)
    yesterday = t - timedelta(days=1)
    if t < end and yesterday.weekday() in days:
        return start - timedelta(days=1), end
    return None


def schedule_refs() -> set[str]:
    """id людей и ролей, на которые ссылается расписание (нельзя удалить/объединить молча)."""
    return {w for rule in config().get("schedule") or [] for w in rule.get("who") or []}


# ---------- итоговый режим ----------

@dataclass
class Resolved:
    mode: str
    conf: dict
    key: str                # уникален для «экземпляра» режима: одна ночь, одна команда
    until: datetime | None
    source: str             # override | schedule | default | exempt


def resolve(speaker: str | None, t: datetime | None = None) -> Resolved:
    name = default_mode()
    if modes_exempt(speaker):
        return Resolved(name, mode_conf(name), f"def:{name}", None, "exempt")
    t = t or now()
    for ov in reversed(overrides()):  # последняя команда главнее
        if speaker_matches(ov.get("who"), speaker, ov["mode"]):
            until = datetime.fromtimestamp(ov["until_ts"], tz())
            return Resolved(ov["mode"], mode_conf(ov["mode"]), f"ov:{ov['id']}", until, "override")
    for i, rule in enumerate(config().get("schedule") or []):
        win = rule_window(rule, t)
        if win and speaker_matches(rule.get("who"), speaker, rule.get("mode", "normal")):
            mode = rule.get("mode", "normal")
            return Resolved(mode, mode_conf(mode), f"sch:{i}:{win[0]:%Y%m%d%H%M}", win[1], "schedule")
    return Resolved(name, mode_conf(name), f"def:{name}", None, "default")


def children_allowed(res: Resolved) -> bool:
    """Режим разрешает отвечать и включать (поле режима allow_children: «тем, на кого действуют режимы»)."""
    return bool(res.conf.get("allow_children", True))


# ---------- бюджет сказок (ночной режим) ----------

def budget_remaining_sec(res: Resolved) -> int | None:
    """None — бюджета нет; иначе сколько секунд сказок ещё можно."""
    budget_min = res.conf.get("play_budget_min")
    if not budget_min:
        return None
    started = db.mark_get("budgets", res.key)
    if started is None:
        return int(budget_min * 60)
    return max(0, int(budget_min * 60 - (time.time() - started)))


def start_budget(res: Resolved) -> None:
    if res.conf.get("play_budget_min"):
        db.mark_set_once("budgets", res.key)


def announce_once(res: Resolved) -> bool:
    """True — если про этот режим ещё не предупреждали (и отмечаем)."""
    return db.mark_set_once("announced", res.key)


def limits(res: Resolved) -> dict:
    """Что Pi должен соблюдать прямо сейчас."""
    return {
        "mode": res.mode,
        "allow_playback": children_allowed(res),
        "max_volume": int(res.conf.get("max_volume", 100)),
        "stop_after_sec": budget_remaining_sec(res),   # None — без таймера
        "until": res.until.isoformat(timespec="minutes") if res.until else None,
    }


def agent_context(res: Resolved, speaker: str | None, available: dict[str, str] | None = None) -> str | None:
    """Служебная пометка для агента: кто говорит, режим, что доступно.
    available — {имя: название} модулей, доступных говорящему сейчас."""
    parts = []
    p = profile(speaker)
    if p:
        parts.append(lang.t("context_speaker_age", name=p["name"], age=p["age"]) if p.get("age")
                     else lang.t("context_speaker", name=p["name"]))
    else:
        parts.append(lang.t("context_unknown"))
    if res.conf.get("prompt"):
        parts.append(res.conf["prompt"])
    if available is not None:
        parts.append(lang.t("context_modules", modules=", ".join(available.values()) or "—"))
    if res.conf.get("tags"):
        parts.append(lang.t("context_tags", tags=", ".join(res.conf["tags"])))
    remaining = budget_remaining_sec(res)
    if remaining is not None and remaining <= 0:
        parts.append(lang.t("budget_over_context"))
    return "; ".join(parts) or None


# ---------- команды родителя ----------

def add_override(mode: str, until: datetime, who: list[str] | None = None,
                 via: str = "api", actor: str | None = None) -> dict:
    oid = uuid.uuid4().hex[:8]
    db.ex("INSERT INTO override(id, mode, until_ts, who, created_at) VALUES(?, ?, ?, ?, ?)",
          (oid, mode, until.timestamp(), json.dumps(who, ensure_ascii=False) if who else None, time.time()))
    db.audit(via, "mode_on", actor, mode=mode, until=until.isoformat(timespec="minutes"), who=who)
    return next((o for o in overrides() if o["id"] == oid), {"id": oid, "mode": mode})


def clear_overrides(who: list[str] | None = None) -> int:
    rows = overrides()
    gone = [o["id"] for o in rows if not who or not o.get("who") or set(o["who"]) & set(who)]
    for oid in gone:
        db.ex("DELETE FROM override WHERE id=?", (oid,))
    return len(gone)


def relax(who: list[str] | None = None, via: str = "api", actor: str | None = None) -> int:
    """Вернуть обычный режим: снять временные команды, а если режим всё ещё задаёт
    расписание (ночь, уроки) — перекрыть его командой default_mode до конца окна.
    who — id людей (и ролей: тогда для всех её людей). Возвращает число снятых команд."""
    removed = clear_overrides(who)
    default = default_mode()
    db.audit(via, "mode_off", actor, who=who, removed=removed)
    if who:
        people = [w for w in who if profiles.store.get(w)]
        people += [u for w in who if profiles.roles.get(w) for u in profiles.roles.users_of(w)]
        for sp in dict.fromkeys(people):
            res = resolve(sp)
            if res.source == "schedule" and res.mode != default and res.until:
                add_override(default, res.until, [sp], via, actor)
        return removed
    # для всех: проверяем неизвестного говорящего и каждого, на кого действуют режимы
    untils = [r.until for r in map(resolve, [None, *targets()])
              if r.source == "schedule" and r.mode != default and r.until]
    if untils:
        add_override(default, max(untils), None, via, actor)
    return removed


def mode_modules(res: Resolved) -> list[str] | None:
    """Какие модули разрешает режим (None — любые)."""
    mods = res.conf.get("modules")
    return list(mods) if mods is not None else None


def allowed_modules(res: Resolved, g: rights.Grants | None) -> list[str] | None:
    """Режим ∩ права говорящего (None — любые)."""
    out = mode_modules(res)
    if g is not None:
        out = [m for m in g.modules() if out is None or m in out]
    return out


def check_playback(res: Resolved, item: dict | None, g: rights.Grants | None = None) -> str | None:
    """Можно ли сейчас включить/продолжить item = {"source", "tags": [..] | None, "age_min"}.
    res — режим для говорящего (у кого core.modes_exempt — default); g — его права.
    item=None — ничего не играет (продолжать нечего, проверяем только режим и бюджет).
    tags=None — что играет, известно, а теги узнать не удалось: если режим/права требуют теги — нельзя.
    None — можно; иначе фраза, которую сказать ребёнку."""
    if not children_allowed(res):
        return res.conf.get("message") or lang.t("blocked_default")
    if item is not None:
        mods = allowed_modules(res, g)
        if mods is not None and item.get("source") not in mods:
            return res.conf.get("modules_message") or lang.t("module_denied")
    remaining = budget_remaining_sec(res)
    if remaining is not None and remaining <= 0:
        return res.conf.get("budget_message") or lang.t("budget_default")
    if item is None:
        return None
    allowed_tags = {t.lower() for t in res.conf.get("tags") or []}
    tags = item.get("tags")
    if allowed_tags and (tags is None or not allowed_tags & {t.lower() for t in tags}):
        return res.conf.get("tags_message") or lang.t("tags_default")
    if g is not None and g.item_denied(item.get("source") or "", item):
        return lang.t("tags_default")
    if g is not None and g.age is not None and (item.get("age_min") or 0) > g.age:
        return lang.t("age_denied")
    return None


def modes_list() -> list[dict]:
    """Режимы, которые можно включить (для админки): все, кроме default_mode."""
    return [{"id": k, "title": v.get("title", k), "default_minutes": v.get("default_minutes", 60)}
            for k, v in (config().get("modes") or {}).items() if k != default_mode()]


def overview() -> dict:
    """Что действует сейчас: на неузнанный голос и на каждого, на кого действуют режимы (для /admin)."""
    default = default_mode()
    people = [(None, None)] + [(uid, profile(uid).get("name", uid)) for uid in targets()]
    rows = []
    for uid, name in people:
        r = resolve(uid)
        rows.append({"id": uid, "name": name, "mode": r.mode, "title": r.conf.get("title", r.mode),
                     "until": r.until.isoformat(timespec="minutes") if r.until else None,
                     "source": r.source, "restricted": r.mode != default})
    return {"now": now().isoformat(timespec="minutes"), "default_mode": default, "people": rows,
            "modes": modes_list(), "onboarding_enabled": onboarding_enabled(),
            "guest_enabled": guest_enabled()}


def status() -> dict:
    return {"now": now().isoformat(timespec="minutes"), "overrides": overrides(),
            "unknown_speaker": limits(resolve(None))}


def available_titles(res: Resolved, speaker: str | None) -> dict[str, str]:
    """{модуль: название}, которые говорящему можно сейчас: его права ∩ режим."""
    return modules.titles(allowed_modules(res, grants(speaker)))
