"""Права: единый словарь «что можно» для ядра и модулей.

Право — константа с id:
  core.*          — права ядра (ниже, CORE);
  <модуль>        — доступ к модулю (ядро заводит само для каждого включённого модуля);
  <модуль>.<имя>  — права, которые модуль объявляет сам служебным инструментом rights().
Вид права:
  flag   — есть или нет;
  values — со списком значений (например, books.tags — теги книг). В «разрешено» — только эти
           значения (белый список), в «запрещено» — эти нельзя. Не задано ни в одной роли — без фильтра.
Роль — какие права разрешены и какие запрещены. У человека несколько ролей: разрешено то, что
разрешила хоть одна роль, минус всё, что запретила любая.

Ядро вычисляет итоговые права (effective) и передаёт модулю его часть; модуль по ним фильтрует,
ядро ещё раз проверяет при включении (policy.check_playback). Сами модули права людям не выдают.
"""
import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field

import modules

log = logging.getLogger("rights")
REFRESH_SEC = int(os.getenv("MODULE_RIGHTS_REFRESH_SEC", "300"))

CONTROL, MODES_EXEMPT, ANY_AGE = "core.control", "core.modes_exempt", "core.any_age"


@dataclass(frozen=True)
class Right:
    id: str
    title: str
    kind: str = "flag"                     # flag | values
    module: str = "core"
    applies_to: str | None = None          # values: к какому полю команды плееру относится («tags»)
    hint: tuple[str, ...] = ()             # подсказки значений для админки (теги каталога)
    description: str = ""

    def view(self) -> dict:
        return {"id": self.id, "title": self.title, "kind": self.kind, "module": self.module,
                "applies_to": self.applies_to, "hint": list(self.hint), "description": self.description}


CORE = [
    Right(CONTROL, "Ставить и снимать ограничения",
          description="Голосом («тихий час», «отбой режимов») и без кодовой фразы."),
    Right(MODES_EXEMPT, "Режимы на него не действуют",
          description="Тихий час, уроки и ночь его не блокируют и не ограничивают громкость."),
    Right(ANY_AGE, "Без возрастных ограничений",
          description="Не учитывать возраст при поиске и включении."),
]
GUEST_FORBIDDEN = {CONTROL}               # неузнанному голосу управлять ограничениями нельзя

_module_rights: dict[str, list[Right]] = {}   # модуль -> объявленные им права (последний ответ)
_fetched_at = 0.0
ID_RE = re.compile(r"^[a-z][\w-]{0,31}\.[a-z][\w.-]{0,63}$")


def _parse(module: str, items) -> list[Right]:
    out = []
    for x in items or []:
        if not isinstance(x, dict) or not ID_RE.match(str(x.get("id", ""))) \
                or not str(x["id"]).startswith(module + "."):
            log.warning("модуль %s: право %r отброшено (id должен начинаться с «%s.»)", module, x, module)
            continue
        kind = x.get("kind", "flag")
        out.append(Right(id=x["id"], title=str(x.get("title") or x["id"])[:80],
                         kind=kind if kind in ("flag", "values") else "flag", module=module,
                         applies_to=x.get("applies_to"), description=str(x.get("description") or "")[:200],
                         hint=tuple(str(h) for h in (x.get("hint") or [])[:500])))
    return out


async def refresh(force: bool = False) -> None:
    """Опросить модули: какие права они объявляют. Недоступный модуль — остаётся прежний список."""
    global _fetched_at
    if not force and time.time() - _fetched_at < REFRESH_SEC:
        return
    _fetched_at = time.time()
    names = list(modules.enabled())
    results = await asyncio.gather(*(modules.call_svc(m, "rights", {}) for m in names))
    for m, res in zip(names, results):
        if res is not None:
            _module_rights[m] = _parse(m, res.get("rights"))


async def refresh_loop() -> None:
    while True:
        try:
            await refresh(force=True)
        except Exception:  # noqa: BLE001
            log.exception("права модулей не обновились")
        await asyncio.sleep(REFRESH_SEC)


def catalog() -> list[Right]:
    """Все известные права: ядра, доступ к каждому включённому модулю и права модулей."""
    out = list(CORE)
    for name, title in modules.titles().items():
        out.append(Right(name, f"Доступ: {title}", module=name))
        out += _module_rights.get(name, [])
    return out


def by_id() -> dict[str, Right]:
    return {r.id: r for r in catalog()}


@dataclass
class Grants:
    """Итоговые права человека."""
    flags: set[str] = field(default_factory=set)
    values: dict[str, dict] = field(default_factory=dict)   # id -> {"allow": [..] | None, "deny": [..]}
    age: int | None = None

    def has(self, right: str) -> bool:
        return right in self.flags

    def modules(self) -> list[str]:
        return [m for m in modules.enabled() if m in self.flags]

    def for_module(self, module: str) -> dict:
        """Часть прав, которую ядро передаёт модулю: его values-права (флаги ему не нужны — без доступа
        к модулю агент его вовсе не видит)."""
        return {rid: v for rid, v in self.values.items() if rid.startswith(module + ".")}

    def item_denied(self, module: str, item: dict) -> bool:
        """Нарушает ли элемент (команда плееру) values-права модуля — например, books.tags по тегам."""
        known = by_id()
        for rid, v in self.for_module(module).items():
            r = known.get(rid)
            if not r or not r.applies_to:
                continue
            got = item.get(r.applies_to)
            got = {str(x).lower() for x in got} if isinstance(got, list) else None
            if v.get("allow") is not None and (got is None or not got & set(v["allow"])):
                return True
            if v.get("deny") and (got is None or got & set(v["deny"])):
                return True
        return False


def effective(roles: list[dict], age: int | None) -> Grants:
    """roles — [{"allow": {right: [vals]}, "deny": {right: [vals]}}] (см. profiles.Role)."""
    g = Grants()
    denied_flags: set[str] = set()
    allow_vals: dict[str, set] = {}
    deny_vals: dict[str, set] = {}
    known = by_id()
    for role in roles:
        for rid, vals in role.get("allow", {}).items():
            r = known.get(rid)
            if r and r.kind == "values":
                allow_vals.setdefault(rid, set()).update(v.lower() for v in vals)
            else:
                g.flags.add(rid)
        for rid, vals in role.get("deny", {}).items():
            r = known.get(rid)
            if r and r.kind == "values" and vals:
                deny_vals.setdefault(rid, set()).update(v.lower() for v in vals)
            else:
                denied_flags.add(rid)
    g.flags -= denied_flags
    for rid in set(allow_vals) | set(deny_vals):
        g.values[rid] = {"allow": sorted(allow_vals[rid]) if allow_vals.get(rid) else None,
                         "deny": sorted(deny_vals.get(rid, set()))}
    g.age = None if g.has(ANY_AGE) else age
    return g
