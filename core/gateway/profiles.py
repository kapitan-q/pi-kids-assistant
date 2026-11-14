"""Люди и роли: data/core/profiles.json и data/core/roles.json, правятся в /admin.

Профиль создаётся знакомством голосом («давай познакомимся») без ролей — как гость.
Роль — набор разрешений: какие модули можно, какие модули и теги нельзя. У человека может быть
несколько ролей: разрешено то, что разрешает хоть одна, минус всё, что запрещает любая.
Флаг роли control — «может устанавливать и снимать ограничения»: команды режимов голосом, режимы
его самого не блокируют. Встроенная роль одна — guest (голос не узнан или ролей нет), ей control
нельзя. При первом запуске создаётся ещё admin (все модули + control) — обычная роль, её можно менять.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Generic, TypeVar

from pydantic import BaseModel, Field, model_validator

log = logging.getLogger("profiles")
DATA_DIR = Path(os.getenv("CORE_DATA_DIR", "/data/core"))
GUEST, ADMIN = "guest", "admin"
BUILTIN = (GUEST,)
LEGACY_PARENT = "parent"                  # прежняя встроенная роль «Родитель» → admin
LEGACY_ROLES = {"newcomer", GUEST}        # старая роль «новичок» = без ролей


def slug(text: str) -> str:
    return re.sub(r"[^\w-]", "", text.strip().lower().replace(" ", "-"))[:20] or "x"


class Voice(BaseModel):
    samples: int = 0
    updated_at: float | None = None


class Profile(BaseModel):
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    name: str = Field(min_length=1, max_length=40)
    roles: list[str] = Field(default_factory=list)            # пусто — гость
    age: int | None = Field(default=None, ge=1, le=120)
    aliases: list[str] = Field(default_factory=list)          # как называют в командах: «старш»
    voice: Voice = Field(default_factory=Voice)
    created_at: float = Field(default_factory=time.time)
    last_seen: float | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        """Старый формат: одна роль + личные ограничения (теперь ограничения — только в ролях)."""
        if isinstance(data, dict) and "role" in data:
            data = dict(data)
            role = data.pop("role")
            data.setdefault("roles", [] if role in LEGACY_ROLES else [role])
            if data.pop("modules", None) is not None or data.pop("tags_deny", None):
                log.warning("профиль %s: личные ограничения больше не поддерживаются — задайте их ролью",
                            data.get("id"))
        return data


class Role(BaseModel):
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    title: str = Field(min_length=1, max_length=40)
    control: bool = False                  # может устанавливать и снимать ограничения
    allow_modules: list[str] = Field(default_factory=list)
    deny_modules: list[str] = Field(default_factory=list)
    deny_tags: list[str] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

    @model_validator(mode="before")
    @classmethod
    def _legacy(cls, data):
        if isinstance(data, dict) and "parent" in data:
            data = dict(data)
            data.setdefault("control", bool(data.pop("parent")))
        return data


M = TypeVar("M", bound=BaseModel)


class JsonStore(Generic[M]):
    """Список моделей в JSON-файле: атомарная запись, потокобезопасно."""

    def __init__(self, path: Path, model: type[M], key: str) -> None:
        self.path, self.model, self.key = path, model, key
        self.lock = threading.RLock()
        self.items: dict[str, M] = {}
        self.loaded = False

    def _load(self) -> None:
        if self.loaded:
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self.items = {x["id"]: self.model.model_validate(x) for x in raw.get(self.key, [])}
        except FileNotFoundError:
            self.items = {}
            self.loaded = True
            self._loaded(fresh=True)
            return
        except (ValueError, KeyError):
            log.exception("%s повреждён — работаю без него (файл не трогаю)", self.path.name)
            self.items = {}
        self.loaded = True
        self._loaded(fresh=False)

    def _loaded(self, fresh: bool) -> None:
        """Хук после загрузки (значения по умолчанию); fresh — файла ещё не было."""

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {self.key: [x.model_dump() for x in self.items.values()]}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    def _new_id(self, text: str) -> str:
        s = slug(text)
        return s if s not in self.items else f"{s}-{uuid.uuid4().hex[:4]}"

    def all(self) -> list[M]:
        with self.lock:
            self._load()
            return sorted(self.items.values(), key=lambda x: x.created_at)

    def get(self, xid: str | None) -> M | None:
        if not xid:
            return None
        with self.lock:
            self._load()
            return self.items.get(xid)

    def update(self, xid: str, **fields) -> M:
        with self.lock:
            self._load()
            x = self.model.model_validate({**self.items[xid].model_dump(), **fields, "id": xid})
            self.items[xid] = x
            self._save()
            return x

    def delete(self, xid: str) -> bool:
        with self.lock:
            self._load()
            if self.items.pop(xid, None) is None:
                return False
            self._save()
            return True

    def reload(self) -> None:
        with self.lock:
            self.loaded = False


class ProfileStore(JsonStore[Profile]):
    def by_name(self, name: str) -> Profile | None:
        n = name.strip().lower()
        return next((p for p in self.all() if p.name.lower() == n), None)

    def create(self, name: str, roles: list[str] | None = None, age: int | None = None) -> Profile:
        with self.lock:
            self._load()
            p = Profile(id=self._new_id(name), name=name.strip(), roles=roles or [], age=age)
            self.items[p.id] = p
            self._save()
            return p

    def touch(self, pid: str) -> None:
        """Отметить, что человека только что узнали (без записи на диск чаще раза в минуту)."""
        with self.lock:
            self._load()
            p = self.items.get(pid)
            if p and (p.last_seen or 0) < time.time() - 60:
                p.last_seen = time.time()
                self._save()


class RoleStore(JsonStore[Role]):
    def _loaded(self, fresh: bool) -> None:
        """Гость есть всегда; при первом запуске — ещё «Админ». Переход со старых версий:
        «Родитель» (parent) → «Админ» (admin), роли из yaml, на которые ссылаются профили, — создаются
        (можно переименовать и настроить в админке)."""
        changed = False
        if GUEST not in self.items:
            self.items[GUEST] = Role(id=GUEST, title="Гость", allow_modules=default_modules(), created_at=0)
            changed = True
        self.items[GUEST].control = False
        users_of_parent = [p for p in profiles_store.all() if LEGACY_PARENT in p.roles]
        if fresh or LEGACY_PARENT in self.items or users_of_parent:
            self.items.pop(LEGACY_PARENT, None)
            if ADMIN not in self.items:
                self.items[ADMIN] = Role(id=ADMIN, title="Админ", control=True,
                                         allow_modules=default_modules(), created_at=1)
            for p in users_of_parent:
                profiles_store.update(p.id, roles=[ADMIN if r == LEGACY_PARENT else r for r in p.roles])
                log.info("профиль %s: роль «Родитель» → «Админ»", p.id)
            changed = True
        for p in profiles_store.all():
            for r in p.roles:
                if r not in self.items:
                    self.items[r] = Role(id=r, title=r, allow_modules=default_modules())
                    log.info("роль %s создана для профиля %s — настройте в /admin", r, p.id)
                    changed = True
        if changed:
            self._save()

    def create(self, title: str) -> Role:
        with self.lock:
            self._load()
            r = Role(id=self._new_id(title), title=title.strip())
            self.items[r.id] = r
            self._save()
            return r


def default_modules() -> list[str]:
    """Модули для роли по умолчанию: все включённые (меняется в админке)."""
    try:
        import modules
        return list(modules.enabled())
    except Exception:  # noqa: BLE001 — без modules.yaml (тесты, первый запуск)
        return []


store = profiles_store = ProfileStore(DATA_DIR / "profiles.json", Profile, "profiles")
roles = RoleStore(DATA_DIR / "roles.json", Role, "roles")
