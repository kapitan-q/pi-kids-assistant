"""Профили, роли, доступы и админка /admin."""
import base64

import bcrypt
import pytest
from fastapi.testclient import TestClient

import admin
import app
import policy
import profiles
import voices

PW = "секрет-родителя"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_HASH", bcrypt.hashpw(PW.encode(), bcrypt.gensalt(4)))
    monkeypatch.setattr(admin, "ADMIN_USER", "parent")
    return TestClient(app.app)


def auth(user="parent", pw=PW):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_migrated_profiles():
    assert {p.id for p in profiles.store.all()} >= {"dad", "older", "younger"}
    assert policy.can_control("dad") and not policy.can_control("older") and not policy.can_control(None)


def test_access_combines_roles_deny_wins():
    assert policy.access(None)["modules"] == ["books"]               # гость
    p = profiles.store.create("Тест Доступ", age=6)
    assert policy.unverified(p.id) and policy.access(p.id)["modules"] == ["books"]   # без ролей = гость
    small = profiles.roles.create("Малыши")
    profiles.roles.update(small.id, deny_tags=["Страшные"])
    profiles.store.update(p.id, roles=["child", small.id])
    acc = policy.access(p.id)
    assert acc == {"modules": ["books"], "tags_deny": {"страшные"}, "age": 6}
    item = {"source": "books", "tags": ["страшные"], "age_min": None}
    assert policy.check_playback(policy.resolve(p.id), item, acc)
    profiles.roles.update(small.id, deny_modules=["books"])            # запрет сильнее разрешения «Дети»
    assert policy.access(p.id)["modules"] == []
    assert policy.access("dad")["modules"] == ["books"]                # админ: модули — тоже по роли
    profiles.store.delete(p.id)
    profiles.roles.delete(small.id)


def test_schedule_who_by_role(at, monkeypatch):
    at(2026, 11, 16, 15, 30)
    cfg = dict(policy.config())
    cfg["schedule"] = [{"from": "15:00", "to": "17:00", "mode": "homework", "who": ["child"]}, {"mode": "normal"}]
    monkeypatch.setattr(policy, "config", lambda: cfg)
    assert policy.resolve("younger").mode == "homework"
    assert policy.resolve("dad").mode == "normal"


def test_admin_requires_parent_password(client):
    assert client.get("/admin/api/profiles").status_code == 401
    assert client.get("/admin/api/profiles", headers=auth(pw="speaker-pass")).status_code == 401
    r = client.get("/admin/api/profiles", headers=auth())
    assert r.status_code == 200 and {"guest", "admin", "child"} <= {x["id"] for x in r.json()["roles"]}
    assert client.get("/admin", headers=auth()).headers["content-type"].startswith("text/html")


def test_admin_patch_profile_roles(client):
    p = profiles.store.create("Маша", age=6)
    r = client.patch(f"/admin/api/profiles/{p.id}", headers=auth(),
                     json={"roles": ["child", "guest"], "aliases": ["Маш"]})
    j = r.json()
    assert r.status_code == 200 and j["roles"] == ["child"] and j["aliases"] == ["маш"] and not j["unverified"]
    assert client.patch(f"/admin/api/profiles/{p.id}", headers=auth(), json={"roles": ["king"]}).status_code == 400
    r = client.patch(f"/admin/api/profiles/{p.id}", headers=auth(), json={"clear_age": True})
    assert r.json()["age"] is None
    profiles.store.delete(p.id)


def test_admin_roles_crud(client, monkeypatch):
    r = client.post("/admin/api/roles", headers=auth(), json={"title": "Младшие"})
    rid = r.json()["id"]
    r = client.patch(f"/admin/api/roles/{rid}", headers=auth(),
                     json={"allow_modules": ["books"], "deny_tags": [" страшные ", "страшные"]})
    assert r.json()["deny_tags"] == ["страшные"]
    assert client.patch(f"/admin/api/roles/{rid}", headers=auth(), json={"deny_modules": ["nope"]}).status_code == 400
    assert client.patch("/admin/api/roles/guest", headers=auth(), json={"control": True}).status_code == 400
    r = client.patch(f"/admin/api/roles/{rid}", headers=auth(), json={"control": True})
    assert r.json()["control"] and policy.can_control("younger") is False   # роль ещё не назначена
    assert client.delete("/admin/api/roles/guest", headers=auth()).status_code == 400
    profiles.store.update("younger", roles=["child", rid])
    orig = policy.config
    cfg = dict(orig())
    cfg["schedule"] = [{"mode": "quiet", "who": [rid]}]
    monkeypatch.setattr(policy, "config", lambda: cfg)
    assert client.delete(f"/admin/api/roles/{rid}", headers=auth()).status_code == 409
    monkeypatch.setattr(policy, "config", orig)
    assert client.delete(f"/admin/api/roles/{rid}", headers=auth()).json() == {"ok": True}
    assert profiles.store.get("younger").roles == ["child"]
    assert client.delete("/admin/api/roles/admin", headers=auth()).status_code == 200   # «Админ» — обычная роль
    profiles.roles.items["admin"] = profiles.Role(id="admin", title="Админ", control=True, allow_modules=["books"])
    profiles.store.update("dad", roles=["admin"])


def test_admin_merge_moves_voice(client):
    new = profiles.store.create("Миша")
    voices.store.save(new.id, [b"RIFF"], [[1.0, 0.0]], "m1")
    profiles.store.update(new.id, voice={"samples": 1, "updated_at": 1.0})
    r = client.post(f"/admin/api/profiles/{new.id}/merge", headers=auth(), json={"into": "older"})
    assert r.status_code == 200 and r.json()["voice"]["samples"] == 1
    assert profiles.store.get(new.id) is None
    assert "older" in voices.store.embeddings()
    assert client.delete("/admin/api/profiles/older", headers=auth()).json() == {"ok": True}
    assert "older" not in voices.store.embeddings()
    profiles.store.items["older"] = profiles.Profile(id="older", name="Старший", roles=["child"], age=9,
                                                     aliases=["старш"])


def test_newcomer_is_under_restrictions_for_others(at):
    """Ребёнок познакомился под чужим именем → на новичка действуют ограничения с who, но не послабления."""
    at(2026, 11, 16, 15, 30)                                   # пн, уроки для older
    kid = profiles.store.create("Вася", age=9)
    assert policy.resolve("older").mode == "homework"
    assert policy.resolve(kid.id).mode == "homework"
    assert policy.resolve("younger").mode == "normal"          # подтверждённый профиль — только свои правила
    policy.relax(["older"])                                    # родитель снял уроки старшему
    assert policy.resolve("older").mode == "normal"
    assert policy.resolve(kid.id).mode == "homework"           # послабление новичка не касается
    assert policy.resolve(None).mode == "homework"
    profiles.store.update(kid.id, roles=["child"])             # родитель назначил роль в админке
    assert policy.resolve(kid.id).mode == "normal"
    policy.clear_overrides()
    profiles.store.delete(kid.id)


def test_admin_shows_and_relaxes_modes(client, at):
    at(2026, 11, 16, 15, 30)
    r = client.get("/admin/api/control", headers=auth())
    rows = {row["id"]: row for row in r.json()["people"]}
    assert rows["older"]["restricted"] and rows[None]["restricted"] and not rows["younger"]["restricted"]
    assert "dad" not in rows
    assert client.get("/admin/api/control").status_code == 401
    assert client.post("/admin/api/control/relax", headers=auth(), json={"who": ["nope"]}).status_code == 400
    r = client.post("/admin/api/control/relax", headers=auth(), json={"who": ["older"]})
    rows = {row["id"]: row for row in r.json()["people"]}
    assert not rows["older"]["restricted"] and rows[None]["restricted"]
    r = client.post("/admin/api/control/relax", headers=auth(), json={"who": None})
    assert not any(row["restricted"] for row in r.json()["people"])
    policy.clear_overrides()


def test_admin_onboarding_toggle(client):
    assert client.get("/admin/api/control", headers=auth()).json()["onboarding_enabled"] is True
    assert client.post("/admin/api/settings", json={"onboarding_enabled": False}).status_code == 401
    r = client.post("/admin/api/settings", headers=auth(), json={"onboarding_enabled": False})
    assert r.json()["onboarding_enabled"] is False and policy.onboarding_enabled() is False
    client.post("/admin/api/settings", headers=auth(), json={"onboarding_enabled": True})
    assert policy.onboarding_enabled()


def test_control_flag_voice_commands(at, monkeypatch):
    """«Может устанавливать и снимать ограничения»: ставит и снимает голосом; режимы его не блокируют."""
    import asyncio
    at(2026, 11, 16, 12, 0)

    async def no_tts(text):
        return None
    monkeypatch.setattr(app, "synthesize", no_tts)
    ask = lambda text, speaker: asyncio.run(app.handle_text("d-ctl", text, speaker))  # noqa: E731

    d = ask("тихий час на десять минут", "dad")                     # без кодовой фразы
    assert d.get("parent_command") and policy.resolve("older").mode == "quiet"
    assert not policy.check_playback(policy.resolve("dad"), {"source": "books", "tags": []},
                                     policy.access("dad"), modes=False)
    d = ask("капитан немо отбой тишины", "older")                   # кодовая фраза без права — нельзя
    assert "взрослый" in d["reply"] and policy.resolve("older").mode == "quiet"
    d = ask("отбой тишины", "dad")
    assert d.get("parent_command") and policy.resolve("older").mode == "normal"
