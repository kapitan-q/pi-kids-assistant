"""Мини-админка /admin: что действует сейчас (включить и снять режим), роли (права) и люди.

Вход — basic-auth родителя (ADMIN_USER + bcrypt ADMIN_PASSWORD_HASH), проверяет сам шлюз:
так одинаково за Traefik и в режиме «дома». Пароль колонки сюда не подходит.
Люди появляются только знакомством голосом с колонки.
"""
import base64
import contextlib
import hmac
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import db
import modules
import policy
import profiles
import rights
import voices

log = logging.getLogger("admin")
ADMIN_USER = os.getenv("ADMIN_USER", "parent")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").encode()
PAGE = Path(__file__).with_name("admin") / "index.html"

CHALLENGE = {"WWW-Authenticate": 'Basic realm="kids-speaker admin", charset="UTF-8"'}


def _credentials(header: str | None) -> tuple[str, str] | None:
    """Basic-auth в UTF-8 (HTTPBasic из FastAPI понимает только ASCII — кириллический пароль не прошёл бы)."""
    if not header or not header.lower().startswith("basic "):
        return None
    try:
        raw = base64.b64decode(header[6:].strip(), validate=True)
    except ValueError:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    user, sep, pw = text.partition(":")
    return (user, pw) if sep else None


def check_parent(authorization: str | None = Header(default=None)) -> None:
    cred = _credentials(authorization)
    ok = False
    if cred and ADMIN_PASSWORD_HASH:
        ok_user = hmac.compare_digest(cred[0].encode(), ADMIN_USER.encode())
        try:
            ok = bcrypt.checkpw(cred[1].encode(), ADMIN_PASSWORD_HASH) and ok_user
        except ValueError:
            ok = False
    if not ok:
        raise HTTPException(401, "bad credentials", headers=CHALLENGE)


router = APIRouter(prefix="/admin", dependencies=[Depends(check_parent)])


class UserPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    roles: list[str] | None = None
    birth_date: str | None = None          # YYYY-MM-DD; "" — убрать
    aliases: list[str] | None = None


class MergeIn(BaseModel):
    into: str


class RoleIn(BaseModel):
    title: str = Field(min_length=1, max_length=40)


class RolePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    allow: dict[str, list[str]] | None = None     # право -> значения (для flag — [])
    deny: dict[str, list[str]] | None = None


class RelaxIn(BaseModel):
    who: list[str] | None = None      # None — снять для всех (и для неузнанного голоса)


class OverrideIn(BaseModel):
    mode: str
    minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    until: str | None = None          # "HH:MM" — до ближайшего такого времени
    who: list[str] | None = None


class SettingsIn(BaseModel):
    onboarding_enabled: bool | None = None
    guest_enabled: bool | None = None


def _user_view(u: profiles.User) -> dict:
    g = policy.grants(u.id)
    return {**u.view(), "unverified": policy.unverified(u.id),
            "effective": {"flags": sorted(g.flags), "values": g.values, "age": g.age}}


def _role_view(r: profiles.Role) -> dict:
    return {**r.model_dump(), "builtin": r.builtin, "used_by": profiles.roles.users_of(r.id)}


def _clean(items: list[str], lower: bool = False) -> list[str]:
    out = []
    for x in items:
        x = x.strip().lower() if lower else x.strip()
        if x and x not in out:
            out.append(x)
    return out


def _check_rights(rid: str, rights_map: dict[str, list[str]], effect: str) -> dict[str, list[str]]:
    known = rights.by_id()
    out = {}
    for right, vals in rights_map.items():
        r = known.get(right)
        if not r:
            raise HTTPException(400, f"нет права {right}")
        if rid == profiles.GUEST and effect == "allow" and right in rights.GUEST_FORBIDDEN:
            raise HTTPException(400, "гостю нельзя управлять ограничениями — голос не узнан")
        out[right] = _clean(vals, lower=True) if r.kind == "values" else []
    return out


def _schedule_block(ids: list[str]) -> None:
    used = sorted(set(ids) & policy.schedule_refs())
    if used:
        raise HTTPException(409, f"на {', '.join(used)} ссылается расписание (config/speaker.yaml) — "
                                 "сначала поправьте его")


@router.get("")
@router.get("/")
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.get("/api/state")
async def state():
    await rights.refresh()
    return {
        "users": [_user_view(u) for u in profiles.store.all()],
        "roles": [_role_view(r) for r in profiles.roles.all()],
        "rights": [r.view() for r in rights.catalog()],
        "modules": modules.titles(),
        "control": policy.overview(),
    }


@router.patch("/api/users/{uid}")
def patch_user(uid: str, body: UserPatch):
    if not profiles.store.get(uid):
        raise HTTPException(404)
    fields = body.model_dump(exclude_none=True)
    if "roles" in fields:
        fields["roles"] = [r for r in _clean(fields["roles"]) if r != profiles.GUEST]
        unknown = [r for r in fields["roles"] if not profiles.roles.get(r)]
        if unknown:
            raise HTTPException(400, f"нет ролей {unknown}")
    if "birth_date" in fields:
        if fields["birth_date"]:
            try:
                d = date.fromisoformat(fields["birth_date"])
            except ValueError as e:
                raise HTTPException(400, "дата рождения — ГГГГ-ММ-ДД") from e
            if not date(1900, 1, 1) <= d <= date.today():
                raise HTTPException(400, "странная дата рождения")
        else:
            fields["birth_date"] = None
    if "aliases" in fields:
        fields["aliases"] = _clean(fields["aliases"], lower=True)
    u = profiles.store.update(uid, **fields)
    db.audit("admin", "user_changed", uid, **fields)
    return _user_view(u)


@router.post("/api/users/{uid}/merge")
def merge(uid: str, body: MergeIn):
    """Голос uid переносится в пользователя into (знакомство → существующий человек), uid удаляется."""
    src, dst = profiles.store.get(uid), profiles.store.get(body.into)
    if not src or not dst or uid == body.into:
        raise HTTPException(400, "нужны два разных существующих пользователя")
    _schedule_block([uid])
    samples = voices.store.move(uid, body.into)
    u = profiles.store.merge(uid, body.into, samples, src.voice.updated_at or dst.voice.updated_at)
    db.audit("admin", "user_merged", uid, into=body.into, samples=samples)
    return _user_view(u)


@router.delete("/api/users/{uid}")
def delete_user(uid: str):
    if not profiles.store.get(uid):
        raise HTTPException(404)
    _schedule_block([uid])
    profiles.store.delete(uid)
    voices.store.delete(uid)
    db.audit("admin", "user_deleted", uid)
    return {"ok": True}


@router.post("/api/roles")
def create_role(body: RoleIn):
    r = profiles.roles.create(body.title)
    db.audit("admin", "role_created", r.id, title=r.title)
    return _role_view(r)


@router.patch("/api/roles/{rid}")
def patch_role(rid: str, body: RolePatch):
    if not profiles.roles.get(rid):
        raise HTTPException(404)
    allow = _check_rights(rid, body.allow, "allow") if body.allow is not None else None
    deny = _check_rights(rid, body.deny, "deny") if body.deny is not None else None
    r = profiles.roles.update(rid, title=body.title, allow=allow, deny=deny)
    db.audit("admin", "role_changed", rid, title=body.title, allow=allow, deny=deny)
    return _role_view(r)


@router.delete("/api/roles/{rid}")
def delete_role(rid: str):
    if rid in profiles.BUILTIN:
        raise HTTPException(400, "встроенную роль удалить нельзя")
    if not profiles.roles.get(rid):
        raise HTTPException(404)
    _schedule_block([rid])
    profiles.roles.delete(rid)
    db.audit("admin", "role_deleted", rid)
    return {"ok": True}


@router.post("/api/settings")
def settings(body: SettingsIn):
    for key, value in body.model_dump(exclude_none=True).items():
        policy.set_setting(key, value)
        db.audit("admin", "setting", None, **{key: value})
    return policy.overview()


@router.get("/api/control")
def control():
    return policy.overview()


@router.post("/api/control/override")
def override(body: OverrideIn):
    """Включить режим из админки: на сколько минут или до какого времени, для кого (люди и роли)."""
    if body.mode not in {m["id"] for m in policy.modes_list()}:
        raise HTTPException(400, f"нет режима {body.mode}")
    who = _clean(body.who or []) or None
    if who and not all(profiles.store.get(w) or profiles.roles.get(w) for w in who):
        raise HTTPException(400, "нет такого человека или роли")
    now = policy.now()
    if body.until:
        try:
            t = datetime.strptime(body.until, "%H:%M")
        except ValueError as e:
            raise HTTPException(400, "до — ЧЧ:ММ") from e
        until = now.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        if until <= now:
            until += timedelta(days=1)
    else:
        until = now + timedelta(minutes=body.minutes or policy.mode_conf(body.mode).get("default_minutes", 60))
    policy.add_override(body.mode, until, who, "admin")
    return policy.overview()


@router.post("/api/control/relax")
def relax(body: RelaxIn):
    """Вернуть обычный режим (в т.ч. поверх расписания до конца окна) — когда голос родителя не узнан."""
    if body.who and not all(profiles.store.get(w) or profiles.roles.get(w) for w in body.who):
        raise HTTPException(400, "нет такого человека или роли")
    policy.relax(body.who or None, "admin")
    return policy.overview()


@router.get("/api/audit")
def audit(limit: int = 50):
    return db.audit_tail(max(1, min(limit, 500)))


# ---------- приложение для телефона (android-client, кладётся scripts/publish_apk.sh) ----------

APP_DIR = Path(os.getenv("APP_DIR", "/data/app"))


@router.get("/api/app")
def app_info():
    apk = APP_DIR / "kids-speaker.apk"
    if not apk.is_file():
        return {"available": False}
    meta = {}
    with contextlib.suppress(OSError, ValueError):
        meta = json.loads((APP_DIR / "version.json").read_text("utf-8"))
    return {"available": True, "size_mb": round(apk.stat().st_size / 2**20, 1),
            "updated": int(apk.stat().st_mtime), **meta}


@router.get("/app.apk")
def app_apk():
    apk = APP_DIR / "kids-speaker.apk"
    if not apk.is_file():
        raise HTTPException(404, "приложение ещё не собрано (scripts/publish_apk.sh)")
    return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="kids-speaker.apk")
