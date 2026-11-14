"""Профили людей у колонки: data/core/profiles.json.

Создаются знакомством голосом («давай познакомимся»), роль и ограничения назначает родитель в /admin.
Роли (что можно по умолчанию) — в config/speaker.yaml. При первом запуске профили переносятся
из старого раздела profiles в yaml (без голоса).
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path

from pydantic import BaseModel, Field

log = logging.getLogger("profiles")
DATA_DIR = Path(os.getenv("CORE_DATA_DIR", "/data/core"))
PATH = DATA_DIR / "profiles.json"


class Voice(BaseModel):
    samples: int = 0
    updated_at: float | None = None


class Profile(BaseModel):
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    name: str = Field(min_length=1, max_length=40)
    role: str = Field(pattern=r"^[\w-]{1,32}$")
    age: int | None = Field(default=None, ge=1, le=120)
    aliases: list[str] = Field(default_factory=list)          # как называют в командах: «старш»
    modules: list[str] | None = None                          # личное сужение роли (None — как у роли)
    tags_deny: list[str] = Field(default_factory=list)        # личные запреты тегов
    voice: Voice = Field(default_factory=Voice)
    created_at: float = Field(default_factory=time.time)
    last_seen: float | None = None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.items: dict[str, Profile] = {}
        self.loaded = False

    def _load(self) -> None:
        if self.loaded:
            return
        try:
            raw = json.loads(self.path.read_text("utf-8"))
            self.items = {p["id"]: Profile.model_validate(p) for p in raw.get("profiles", [])}
        except FileNotFoundError:
            self.items = {}
        except (ValueError, KeyError):
            log.exception("profiles.json повреждён — работаю без профилей (файл не трогаю)")
            self.items = {}
        self.loaded = True

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        data = {"profiles": [p.model_dump() for p in self.items.values()]}
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(self.path)

    def migrate_from_yaml(self, profiles: dict) -> None:
        """Однократно: профили из старого policies.yaml → profiles.json."""
        with self.lock:
            self._load()
            if self.path.exists() or not profiles:
                return
            for pid, p in profiles.items():
                self.items[pid] = Profile(id=pid, name=p.get("name", pid), role=p.get("role", "child"),
                                          age=p.get("age"), aliases=p.get("aliases") or [])
            self._save()
            log.info("профили перенесены из yaml: %s", list(self.items))

    def all(self) -> list[Profile]:
        with self.lock:
            self._load()
            return sorted(self.items.values(), key=lambda p: p.created_at)

    def get(self, pid: str | None) -> Profile | None:
        if not pid:
            return None
        with self.lock:
            self._load()
            return self.items.get(pid)

    def by_name(self, name: str) -> Profile | None:
        n = name.strip().lower()
        return next((p for p in self.all() if p.name.lower() == n), None)

    def create(self, name: str, role: str, age: int | None = None) -> Profile:
        slug = re.sub(r"[^\w-]", "", name.lower().replace(" ", "-"))[:20] or "p"
        with self.lock:
            self._load()
            pid = slug if slug not in self.items else f"{slug}-{uuid.uuid4().hex[:4]}"
            p = Profile(id=pid, name=name.strip(), role=role, age=age)
            self.items[pid] = p
            self._save()
            return p

    def update(self, pid: str, **fields) -> Profile:
        with self.lock:
            self._load()
            p = self.items[pid]
            p = Profile.model_validate({**p.model_dump(), **fields, "id": pid})
            self.items[pid] = p
            self._save()
            return p

    def delete(self, pid: str) -> bool:
        with self.lock:
            self._load()
            if self.items.pop(pid, None) is None:
                return False
            self._save()
            return True

    def touch(self, pid: str) -> None:
        """Отметить, что человека только что узнали (без записи на диск чаще раза в минуту)."""
        with self.lock:
            self._load()
            p = self.items.get(pid)
            if p and (p.last_seen or 0) < time.time() - 60:
                p.last_seen = time.time()
                self._save()

    def reload(self) -> None:
        with self.lock:
            self.loaded = False


store = Store(PATH)
