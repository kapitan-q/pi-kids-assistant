"""Мини-админка /admin: что действует сейчас (и снять), роли (что можно) и люди (какие у них роли).

Вход — basic-auth родителя (ADMIN_USER + bcrypt ADMIN_PASSWORD_HASH), проверяет сам шлюз:
так одинаково за Traefik и в режиме «дома». Пароль колонки сюда не подходит.
Профили создаются только знакомством голосом с колонки.
"""
import base64
import hmac
import logging
import os
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import modules
import policy
import profiles
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


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    roles: list[str] | None = None
    age: int | None = Field(default=None, ge=1, le=120)
    clear_age: bool = False
    aliases: list[str] | None = None


class MergeIn(BaseModel):
    into: str


class RoleIn(BaseModel):
    title: str = Field(min_length=1, max_length=40)


class RolePatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=40)
    allow_modules: list[str] | None = None
    deny_modules: list[str] | None = None
    deny_tags: list[str] | None = None
    control: bool | None = None       # может устанавливать и снимать ограничения


def _view(p: profiles.Profile) -> dict:
    d = p.model_dump()
    acc = policy.access(p.id)
    d["can_control"] = policy.can_control(p.id)
    d["unverified"] = policy.unverified(p.id)
    d["effective"] = {"modules": acc["modules"], "deny_tags": sorted(acc["tags_deny"])}
    return d


def _role_view(r: profiles.Role) -> dict:
    d = r.model_dump()
    d["builtin"] = r.id in profiles.BUILTIN
    d["used_by"] = [p.id for p in profiles.store.all() if r.id in p.roles]
    return d


def _clean(items: list[str], lower: bool = False) -> list[str]:
    out = []
    for x in items:
        x = x.strip().lower() if lower else x.strip()
        if x and x not in out:
            out.append(x)
    return out


def _schedule_refs(role_id: str) -> bool:
    cfg = policy.config()
    return any(role_id in (rule.get("who") or []) for rule in cfg.get("schedule") or [])


@router.get("")
@router.get("/")
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.get("/api/profiles")
def list_profiles():
    return {
        "profiles": [_view(p) for p in profiles.store.all()],
        "roles": [_role_view(r) for r in profiles.roles.all()],
        "modules": modules.titles(),
    }


@router.patch("/api/profiles/{pid}")
def patch_profile(pid: str, body: ProfilePatch):
    if not profiles.store.get(pid):
        raise HTTPException(404)
    fields = body.model_dump(exclude_none=True, exclude={"clear_age"})
    if "roles" in fields:
        fields["roles"] = [r for r in _clean(fields["roles"]) if r != profiles.GUEST]
        unknown = [r for r in fields["roles"] if not profiles.roles.get(r)]
        if unknown:
            raise HTTPException(400, f"нет ролей {unknown}")
    if body.clear_age:
        fields["age"] = None
    if "aliases" in fields:
        fields["aliases"] = _clean(fields["aliases"], lower=True)
    p = profiles.store.update(pid, **fields)
    log.info("профиль %s изменён: %s", pid, fields)
    return _view(p)


@router.post("/api/roles")
def create_role(body: RoleIn):
    r = profiles.roles.create(body.title)
    log.info("роль %s создана", r.id)
    return _role_view(r)


@router.patch("/api/roles/{rid}")
def patch_role(rid: str, body: RolePatch):
    r = profiles.roles.get(rid)
    if not r:
        raise HTTPException(404)
    fields = body.model_dump(exclude_none=True)
    if rid == profiles.GUEST and fields.get("control"):
        raise HTTPException(400, "гостю нельзя управлять ограничениями — голос не узнан")
    for key in ("allow_modules", "deny_modules"):
        if key in fields:
            fields[key] = _clean(fields[key])
            unknown = set(fields[key]) - set(modules.enabled())
            if unknown:
                raise HTTPException(400, f"нет модулей {sorted(unknown)}")
    if "deny_tags" in fields:
        fields["deny_tags"] = _clean(fields["deny_tags"])
    r = profiles.roles.update(rid, **fields)
    log.info("роль %s изменена: %s", rid, fields)
    return _role_view(r)


@router.delete("/api/roles/{rid}")
def delete_role(rid: str):
    if rid in profiles.BUILTIN:
        raise HTTPException(400, "встроенную роль удалить нельзя")
    if not profiles.roles.get(rid):
        raise HTTPException(404)
    if _schedule_refs(rid):
        raise HTTPException(409, "роль используется в расписании (config/speaker.yaml) — сначала уберите её оттуда")
    for p in profiles.store.all():
        if rid in p.roles:
            profiles.store.update(p.id, roles=[x for x in p.roles if x != rid])
    profiles.roles.delete(rid)
    log.info("роль %s удалена", rid)
    return {"ok": True}


@router.post("/api/profiles/{pid}/merge")
def merge(pid: str, body: MergeIn):
    """Голос профиля pid переносится в профиль into (например, «Маша» из знакомства → «Старший»), pid удаляется."""
    src, dst = profiles.store.get(pid), profiles.store.get(body.into)
    if not src or not dst or pid == body.into:
        raise HTTPException(400, "нужны два разных существующих профиля")
    samples = voices.store.move(pid, body.into)
    profiles.store.update(body.into, voice={"samples": samples, "updated_at": src.voice.updated_at},
                          last_seen=max(src.last_seen or 0, dst.last_seen or 0) or None)
    profiles.store.delete(pid)
    log.info("профиль %s объединён с %s (%d образцов)", pid, body.into, samples)
    return _view(profiles.store.get(body.into))


class RelaxIn(BaseModel):
    who: list[str] | None = None      # None — снять для всех (и для неузнанного голоса)


class SettingsIn(BaseModel):
    onboarding_enabled: bool


@router.post("/api/settings")
def settings(body: SettingsIn):
    policy.set_onboarding(body.onboarding_enabled)
    log.info("новые знакомства %s из админки", "разрешены" if body.onboarding_enabled else "запрещены")
    return policy.overview()


@router.get("/api/control")
def control():
    return policy.overview()


@router.post("/api/control/relax")
def relax(body: RelaxIn):
    """Вернуть обычный режим (в т.ч. поверх расписания до конца окна) — когда голос родителя не узнан."""
    if body.who and not all(profiles.store.get(pid) for pid in body.who):
        raise HTTPException(400, "нет такого профиля")
    removed = policy.relax(body.who or None)
    log.info("ограничения сняты из админки: %s (команд снято: %d)", body.who or "все", removed)
    return policy.overview()


@router.delete("/api/profiles/{pid}")
def delete(pid: str):
    if not profiles.store.delete(pid):
        raise HTTPException(404)
    voices.store.delete(pid)
    log.info("профиль %s удалён вместе с голосом", pid)
    return {"ok": True}
