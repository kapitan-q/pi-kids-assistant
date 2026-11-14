"""Режимы, расписание, временные команды родителя и лимиты для Pi.

Приоритет: временная команда родителя > расписание > default.
Состояние (команды, «уже предупредили», бюджеты ночных сказок) — в JSON-файле.
"""
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

import lang
import profiles

log = logging.getLogger("policy")

CONFIG_PATH = Path(os.getenv("POLICY_FILE", "/config/speaker.yaml"))
STATE_PATH = Path(os.getenv("STATE_FILE", "/data/state/state.json"))
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_CONFIG = {"modes": {"normal": {}}, "schedule": [{"mode": "normal"}]}


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
            log.info("policies.yaml загружен")
        except yaml.YAMLError:
            log.exception("ошибка в policies.yaml — оставляю прежний конфиг")
            _cfg = _cfg or DEFAULT_CONFIG
    return _cfg


def tz() -> ZoneInfo:
    """Часовой пояс — TZ контейнера; в yaml только если нужно переопределить."""
    return ZoneInfo(config().get("timezone") or os.getenv("TZ") or "UTC")


def now() -> datetime:
    return datetime.now(tz())


def mode_conf(name: str) -> dict:
    return (config().get("modes") or {}).get(name, {})


def profile(speaker: str | None) -> dict:
    """Профиль говорящего ({} — не узнан или нет такого)."""
    p = profiles.store.get(speaker)
    return p.model_dump() if p else {}


def guest_role() -> str:
    return config().get("guest_role", "guest")


def role_of(speaker: str | None) -> str:
    return profile(speaker).get("role") or guest_role()


def role_conf(role: str) -> dict:
    return (config().get("roles") or {}).get(role, {})


def is_parent(speaker: str | None) -> bool:
    return bool(speaker) and bool(role_conf(role_of(speaker)).get("parent"))


def children() -> list[str]:
    """id профилей, на которых действуют детские правила (все, кроме родителей)."""
    return [p.id for p in profiles.store.all() if not role_conf(p.role).get("parent")]


def speaker_matches(who: list | None, speaker: str | None) -> bool:
    """who в правилах и командах — id профилей и/или имена ролей (who: [child])."""
    if not who:
        return True
    if speaker is None:
        return bool(config().get("unknown_speaker_matches", True))
    return speaker in who or role_of(speaker) in who


def access(speaker: str | None) -> dict:
    """Что можно этому говорящему: роль (yaml) сужается личными ограничениями (админка).
    {"modules": [..] | None (любые), "tags_deny": set, "age": int | None}"""
    p = profile(speaker)
    role = role_conf(role_of(speaker))
    mods = role.get("modules")
    if p.get("modules") is not None:
        mods = [m for m in p["modules"] if mods is None or m in mods]
    deny = {t.lower() for t in (role.get("tags_deny") or []) + (p.get("tags_deny") or [])}
    return {"modules": mods, "tags_deny": deny, "age": p.get("age")}


# ---------- состояние ----------

class State:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.data = {"overrides": [], "announced": {}, "budgets": {}}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text("utf-8")))
            except json.JSONDecodeError:
                log.exception("state.json повреждён — начинаю с чистого")

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    def prune(self) -> None:
        """Убираем истёкшие команды и старые отметки (старше двух суток)."""
        ts = time.time()
        self.data["overrides"] = [o for o in self.data["overrides"] if o["until_ts"] > ts]
        for key in ("announced", "budgets"):
            self.data[key] = {k: v for k, v in self.data[key].items() if ts - v < 2 * 86400}


state = State(STATE_PATH)


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


# ---------- итоговый режим ----------

@dataclass
class Resolved:
    mode: str
    conf: dict
    key: str                # уникален для «экземпляра» режима: одна ночь, одна команда
    until: datetime | None
    source: str             # override | schedule | default


def resolve(speaker: str | None, t: datetime | None = None) -> Resolved:
    t = t or now()
    with state.lock:
        state.prune()
        overrides = list(state.data["overrides"])
    for ov in reversed(overrides):  # последняя команда главнее
        if speaker_matches(ov.get("who"), speaker):
            until = datetime.fromtimestamp(ov["until_ts"], tz())
            return Resolved(ov["mode"], mode_conf(ov["mode"]), f"ov:{ov['id']}", until, "override")
    for i, rule in enumerate(config().get("schedule") or []):
        win = rule_window(rule, t)
        if win and speaker_matches(rule.get("who"), speaker):
            name = rule.get("mode", "normal")
            return Resolved(name, mode_conf(name), f"sch:{i}:{win[0]:%Y%m%d%H%M}", win[1], "schedule")
    name = config().get("default_mode", "normal")
    return Resolved(name, mode_conf(name), f"def:{name}", None, "default")


def children_allowed(res: Resolved) -> bool:
    return bool(res.conf.get("allow_children", True))


# ---------- бюджет сказок (ночной режим) ----------

def budget_remaining_sec(res: Resolved) -> int | None:
    """None — бюджета нет; иначе сколько секунд сказок ещё можно."""
    budget_min = res.conf.get("play_budget_min")
    if not budget_min:
        return None
    with state.lock:
        started = state.data["budgets"].get(res.key)
    if started is None:
        return int(budget_min * 60)
    return max(0, int(budget_min * 60 - (time.time() - started)))


def start_budget(res: Resolved) -> None:
    if not res.conf.get("play_budget_min"):
        return
    with state.lock:
        if res.key not in state.data["budgets"]:
            state.data["budgets"][res.key] = time.time()
            state.save()


def announce_once(res: Resolved) -> bool:
    """True — если про этот режим ещё не предупреждали (и отмечаем)."""
    with state.lock:
        if res.key in state.data["announced"]:
            return False
        state.data["announced"][res.key] = time.time()
        state.save()
        return True


def limits(res: Resolved) -> dict:
    """Что Pi должен соблюдать прямо сейчас."""
    return {
        "mode": res.mode,
        "allow_playback": children_allowed(res),
        "max_volume": int(res.conf.get("max_volume", 100)),
        "stop_after_sec": budget_remaining_sec(res),   # None — без таймера
        "until": res.until.isoformat(timespec="minutes") if res.until else None,
    }


def agent_context(res: Resolved, speaker: str | None, modules: dict[str, str] | None = None) -> str | None:
    """Служебная пометка для агента: кто говорит, режим, что доступно.
    modules — {имя: название} модулей, доступных говорящему сейчас."""
    parts = []
    p = profile(speaker)
    if p:
        name = p.get("name", speaker)
        parts.append(lang.t("context_speaker_age", name=name, age=p["age"]) if p.get("age")
                     else lang.t("context_speaker", name=name))
    if res.conf.get("prompt"):
        parts.append(res.conf["prompt"])
    if modules is not None:
        parts.append(lang.t("context_modules", modules=", ".join(modules.values()) or "—"))
    if res.conf.get("tags"):
        parts.append(lang.t("context_tags", tags=", ".join(res.conf["tags"])))
    remaining = budget_remaining_sec(res)
    if remaining is not None and remaining <= 0:
        parts.append(lang.t("budget_over_context"))
    return "; ".join(parts) or None


# ---------- команды родителя ----------

def add_override(mode: str, until: datetime, who: list[str] | None = None) -> dict:
    ov = {"id": uuid.uuid4().hex[:8], "mode": mode, "until_ts": until.timestamp(),
          "until": until.isoformat(timespec="minutes"), "who": who or None}
    with state.lock:
        state.data["overrides"].append(ov)
        state.prune()
        state.save()
    return ov


def clear_overrides(who: list[str] | None = None) -> int:
    with state.lock:
        before = len(state.data["overrides"])
        if who:
            state.data["overrides"] = [o for o in state.data["overrides"]
                                       if not o.get("who") or not set(o["who"]) & set(who)]
        else:
            state.data["overrides"] = []
        state.save()
        return before - len(state.data["overrides"])


def relax(who: list[str] | None = None) -> int:
    """Вернуть обычный режим: снять временные команды, а если режим всё ещё задаёт
    расписание (ночь, уроки) — перекрыть его командой default_mode до конца окна.
    Возвращает число снятых команд."""
    removed = clear_overrides(who)
    default = config().get("default_mode", "normal")
    if who:
        for sp in who:
            res = resolve(sp)
            if res.source == "schedule" and res.mode != default and res.until:
                add_override(default, res.until, [sp])
        return removed
    # для всех: проверяем неизвестного говорящего и каждого ребёнка
    speakers = [None] + children()
    untils = [r.until for r in map(resolve, speakers)
              if r.source == "schedule" and r.mode != default and r.until]
    if untils:
        add_override(default, max(untils), None)
    return removed


def mode_modules(res: Resolved) -> list[str] | None:
    """Какие модули разрешает режим (None — любые)."""
    mods = res.conf.get("modules")
    return list(mods) if mods is not None else None


def allowed_modules(res: Resolved, acc: dict | None) -> list[str] | None:
    """Режим ∩ доступ говорящего (None — любые)."""
    out = mode_modules(res)
    mods = (acc or {}).get("modules")
    if mods is not None:
        out = [m for m in mods if out is None or m in out]
    return out


def check_playback(res: Resolved, item: dict | None, acc: dict | None = None) -> str | None:
    """Можно ли сейчас включить/продолжить item = {"source", "tags": [..] | None, "age_min"}.
    acc — access(speaker): модули, запрещённые теги, возраст.
    item=None — ничего не играет (продолжать нечего, проверяем только режим и бюджет).
    tags=None — что играет, известно, а теги узнать не удалось: если режим требует теги — нельзя.
    None — можно; иначе фраза, которую сказать ребёнку."""
    acc = acc or {}
    if not children_allowed(res):
        return res.conf.get("message") or lang.t("blocked_default")
    if item is not None:
        mods = allowed_modules(res, acc)
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
    if acc.get("tags_deny") and tags is not None and acc["tags_deny"] & {t.lower() for t in tags}:
        return lang.t("tags_default")
    if acc.get("age") is not None and (item.get("age_min") or 0) > acc["age"]:
        return lang.t("age_denied")
    return None


def status() -> dict:
    with state.lock:
        state.prune()
        overrides = list(state.data["overrides"])
    return {"now": now().isoformat(timespec="minutes"), "overrides": overrides,
            "unknown_speaker": limits(resolve(None))}
