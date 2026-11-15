"""Пользователи и роли (data/core/speaker.db, правятся в /admin).

Пользователь — имя, дата рождения (возраст считается сам), обращения для голосовых команд
(«старший», «машенька» — целыми словами), роли. Появляется после «давай познакомимся» без ролей.
Роль — какие права разрешены и какие запрещены (rights.py). Встроенная роль одна — «Гость»: её права
получают неузнанный голос и пользователь без ролей; управлять ограничениями гостю нельзя.
"""
import json
import re
import time
import uuid
from datetime import date

from pydantic import BaseModel, Field

import db

GUEST = "guest"
BUILTIN = (GUEST,)


def slug(text: str) -> str:
    return re.sub(r"[^\w-]", "", text.strip().lower().replace(" ", "-"))[:20] or "x"


def age_of(birth: str | None, today: date | None = None) -> int | None:
    if not birth:
        return None
    b, t = date.fromisoformat(birth), today or date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


class Voice(BaseModel):
    samples: int = 0
    updated_at: float | None = None


class User(BaseModel):
    id: str = Field(pattern=r"^[\w-]{1,32}$")
    name: str = Field(min_length=1, max_length=40)
    roles: list[str] = Field(default_factory=list)
    birth_date: str | None = None
    aliases: list[str] = Field(default_factory=list)
    voice: Voice = Field(default_factory=Voice)
    created_at: float = Field(default_factory=time.time)
    last_seen: float | None = None

    @property
    def age(self) -> int | None:
        return age_of(self.birth_date)

    def view(self) -> dict:
        return {**self.model_dump(), "age": self.age}


Profile = User   # старое имя


class Role(BaseModel):
    id: str
    title: str
    allow: dict[str, list[str]] = Field(default_factory=dict)   # право -> значения ([] — без списка)
    deny: dict[str, list[str]] = Field(default_factory=dict)
    created_at: float = 0

    @property
    def builtin(self) -> bool:
        return self.id in BUILTIN


def _new_id(table: str, text: str) -> str:
    s = slug(text)
    taken = {r[0] for r in db.q(f"SELECT id FROM {table}")} | {r[0] for r in db.q(  # noqa: S608
        "SELECT id FROM role" if table == "user" else "SELECT id FROM user")}
    return s if s not in taken else f"{s}-{uuid.uuid4().hex[:4]}"   # id людей и ролей не пересекаются


# ---------- пользователи ----------

def _user(row) -> User:
    roles = [r[0] for r in db.q("SELECT role_id FROM user_role WHERE user_id=? ORDER BY role_id", (row["id"],))]
    return User(id=row["id"], name=row["name"], roles=roles, birth_date=row["birth_date"],
                aliases=json.loads(row["aliases"] or "[]"),
                voice=Voice(samples=row["voice_samples"], updated_at=row["voice_updated"]),
                created_at=row["created_at"], last_seen=row["last_seen"])


class UserStore:
    def all(self) -> list[User]:
        return [_user(r) for r in db.q("SELECT * FROM user ORDER BY created_at")]

    def get(self, uid: str | None) -> User | None:
        if not uid:
            return None
        row = db.q1("SELECT * FROM user WHERE id=?", (uid,))
        return _user(row) if row else None

    def by_name(self, name: str) -> User | None:
        n = name.strip().lower()
        return next((u for u in self.all() if u.name.lower() == n), None)

    def create(self, name: str, roles: list[str] | None = None, age: int | None = None) -> User:
        with db.tx() as c:
            uid = _new_id("user", name)
            c.execute("INSERT INTO user(id, name, birth_date, created_at) VALUES(?, ?, ?, ?)",
                      (uid, name.strip(), db.birth_from_age(age), time.time()))
            for r in roles or []:
                c.execute("INSERT INTO user_role(user_id, role_id) VALUES(?, ?)", (uid, r))
        return self.get(uid)

    def update(self, uid: str, **fields) -> User:
        """name, roles, birth_date, age (→ дата рождения), aliases, voice ({samples, updated_at}), last_seen."""
        if "age" in fields:
            fields["birth_date"] = db.birth_from_age(fields.pop("age"))
        cols = {"name": "name", "birth_date": "birth_date", "last_seen": "last_seen"}
        with db.tx() as c:
            for key, col in cols.items():
                if key in fields:
                    c.execute(f"UPDATE user SET {col}=? WHERE id=?", (fields[key], uid))  # noqa: S608
            if "aliases" in fields:
                c.execute("UPDATE user SET aliases=? WHERE id=?",
                          (json.dumps(fields["aliases"], ensure_ascii=False), uid))
            if "voice" in fields:
                v = fields["voice"]
                v = v.model_dump() if isinstance(v, Voice) else v
                c.execute("UPDATE user SET voice_samples=?, voice_updated=? WHERE id=?",
                          (v.get("samples", 0), v.get("updated_at"), uid))
            if "roles" in fields:
                c.execute("DELETE FROM user_role WHERE user_id=?", (uid,))
                for r in fields["roles"]:
                    c.execute("INSERT INTO user_role(user_id, role_id) VALUES(?, ?)", (uid, r))
        return self.get(uid)

    def delete(self, uid: str) -> bool:
        return db.ex("DELETE FROM user WHERE id=?", (uid,)) > 0

    def merge(self, src: str, dst: str, samples: int, voice_updated: float | None) -> User:
        """Голос src уже перенесён в dst (voices.move): обновить dst и удалить src — одной транзакцией."""
        with db.tx() as c:
            seen = c.execute("SELECT max(coalesce(last_seen, 0)) FROM user WHERE id IN (?, ?)",
                             (src, dst)).fetchone()[0]
            c.execute("UPDATE user SET voice_samples=?, voice_updated=?, last_seen=? WHERE id=?",
                      (samples, voice_updated, seen or None, dst))
            c.execute("DELETE FROM user WHERE id=?", (src,))
        return self.get(dst)

    def touch(self, uid: str) -> None:
        """Отметить, что человека только что узнали (не чаще раза в минуту)."""
        db.ex("UPDATE user SET last_seen=? WHERE id=? AND coalesce(last_seen, 0) < ?",
              (time.time(), uid, time.time() - 60))


# ---------- роли ----------

def _role(row) -> Role:
    allow, deny = {}, {}
    for r in db.q("SELECT right_id, effect, vals FROM role_right WHERE role_id=? ORDER BY right_id", (row["id"],)):
        (allow if r["effect"] == "allow" else deny)[r["right_id"]] = json.loads(r["vals"] or "[]")
    return Role(id=row["id"], title=row["title"], allow=allow, deny=deny, created_at=row["created_at"])


class RoleStore:
    def all(self) -> list[Role]:
        return [_role(r) for r in db.q("SELECT * FROM role ORDER BY created_at, id")]

    def get(self, rid: str | None) -> Role | None:
        if not rid:
            return None
        row = db.q1("SELECT * FROM role WHERE id=?", (rid,))
        return _role(row) if row else None

    def create(self, title: str) -> Role:
        rid = _new_id("role", title)
        db.ex("INSERT INTO role(id, title, created_at) VALUES(?, ?, ?)", (rid, title.strip(), time.time()))
        return self.get(rid)

    def update(self, rid: str, title: str | None = None, allow: dict | None = None,
               deny: dict | None = None) -> Role:
        with db.tx() as c:
            if title is not None:
                c.execute("UPDATE role SET title=? WHERE id=?", (title.strip(), rid))
            for effect, rights in (("allow", allow), ("deny", deny)):
                if rights is None:
                    continue
                c.execute("DELETE FROM role_right WHERE role_id=? AND effect=?", (rid, effect))
                for right, vals in rights.items():
                    c.execute("INSERT INTO role_right(role_id, right_id, effect, vals) VALUES(?, ?, ?, ?)",
                              (rid, right, effect, json.dumps(vals or [], ensure_ascii=False)))
        return self.get(rid)

    def delete(self, rid: str) -> bool:
        return db.ex("DELETE FROM role WHERE id=?", (rid,)) > 0   # у людей снимется каскадом

    def users_of(self, rid: str) -> list[str]:
        return [r[0] for r in db.q("SELECT user_id FROM user_role WHERE role_id=?", (rid,))]


store = users = UserStore()
roles = RoleStore()
