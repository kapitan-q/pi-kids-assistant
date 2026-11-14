"""Мини-админка /admin: список профилей, роли и личные ограничения.

Вход — basic-auth родителя (ADMIN_USER + bcrypt ADMIN_PASSWORD_HASH), проверяет сам шлюз:
так одинаково за Traefik и в режиме «дома». Пароль колонки сюда не подходит.
Профили создаются только знакомством голосом с колонки.
"""
import hmac
import logging
import os
from pathlib import Path

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field

import modules
import policy
import profiles
import voices

log = logging.getLogger("admin")
ADMIN_USER = os.getenv("ADMIN_USER", "parent")
ADMIN_PASSWORD_HASH = os.getenv("ADMIN_PASSWORD_HASH", "").encode()
PAGE = Path(__file__).with_name("admin") / "index.html"

basic = HTTPBasic(realm="kids-speaker admin")


def check_parent(cred: HTTPBasicCredentials = Depends(basic)) -> None:
    ok_user = hmac.compare_digest(cred.username.encode(), ADMIN_USER.encode())
    try:
        ok_pw = bool(ADMIN_PASSWORD_HASH) and bcrypt.checkpw(cred.password.encode(), ADMIN_PASSWORD_HASH)
    except ValueError:
        ok_pw = False
    if not (ok_user and ok_pw):
        raise HTTPException(401, "bad credentials", headers={"WWW-Authenticate": 'Basic realm="kids-speaker admin"'})


router = APIRouter(prefix="/admin", dependencies=[Depends(check_parent)])


class ProfilePatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=40)
    role: str | None = None
    age: int | None = Field(default=None, ge=1, le=120)
    clear_age: bool = False
    aliases: list[str] | None = None
    modules: list[str] | None = None
    inherit_modules: bool = False     # true — снять личное ограничение модулей (как у роли)
    tags_deny: list[str] | None = None


class MergeIn(BaseModel):
    into: str


def _view(p: profiles.Profile) -> dict:
    d = p.model_dump()
    d["is_parent"] = bool(policy.role_conf(p.role).get("parent"))
    d["effective_modules"] = policy.access(p.id)["modules"]
    return d


@router.get("")
@router.get("/")
def page():
    return FileResponse(PAGE, media_type="text/html")


@router.get("/api/profiles")
def list_profiles():
    roles = policy.config().get("roles") or {}
    return {
        "profiles": [_view(p) for p in profiles.store.all()],
        "roles": {name: {"title": r.get("title", name), "modules": r.get("modules"),
                         "parent": bool(r.get("parent"))} for name, r in roles.items()},
        "modules": modules.titles(),
        "guest_role": policy.guest_role(),
    }


@router.patch("/api/profiles/{pid}")
def patch_profile(pid: str, body: ProfilePatch):
    if not profiles.store.get(pid):
        raise HTTPException(404)
    roles = policy.config().get("roles") or {}
    fields = body.model_dump(exclude_none=True, exclude={"clear_age", "inherit_modules"})
    if "role" in fields and fields["role"] not in roles:
        raise HTTPException(400, f"нет роли {fields['role']}")
    if "modules" in fields:
        unknown = set(fields["modules"]) - set(modules.enabled())
        if unknown:
            raise HTTPException(400, f"нет модулей {sorted(unknown)}")
    if body.clear_age:
        fields["age"] = None
    if body.inherit_modules:
        fields["modules"] = None
    if "aliases" in fields:
        fields["aliases"] = [a.strip().lower() for a in fields["aliases"] if a.strip()]
    if "tags_deny" in fields:
        fields["tags_deny"] = [t.strip() for t in fields["tags_deny"] if t.strip()]
    p = profiles.store.update(pid, **fields)
    log.info("профиль %s изменён: %s", pid, fields)
    return _view(p)


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


@router.delete("/api/profiles/{pid}")
def delete(pid: str):
    if not profiles.store.delete(pid):
        raise HTTPException(404)
    voices.store.delete(pid)
    log.info("профиль %s удалён вместе с голосом", pid)
    return {"ok": True}
