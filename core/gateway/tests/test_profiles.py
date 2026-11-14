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
    assert policy.is_parent("dad") and not policy.is_parent("older") and not policy.is_parent(None)


def test_access_role_and_personal_narrowing():
    assert policy.access(None)["modules"] == ["books"]               # гость
    p = profiles.store.create("Тест Доступ", "child", age=6)
    assert policy.access(p.id) == {"modules": ["books"], "tags_deny": set(), "age": 6}
    profiles.store.update(p.id, modules=["books", "youtube"], tags_deny=["Страшные"])
    acc = policy.access(p.id)
    assert acc["modules"] == ["books"] and acc["tags_deny"] == {"страшные"}   # профиль только сужает
    item = {"source": "books", "tags": ["страшные"], "age_min": None}
    assert policy.check_playback(policy.resolve(p.id), item, acc)
    profiles.store.delete(p.id)


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
    assert r.status_code == 200 and "child" in r.json()["roles"]
    assert client.get("/admin", headers=auth()).headers["content-type"].startswith("text/html")


def test_admin_patch_role_and_limits(client):
    p = profiles.store.create("Маша", "newcomer", age=6)
    r = client.patch(f"/admin/api/profiles/{p.id}", headers=auth(),
                     json={"role": "child", "tags_deny": ["страшные"], "aliases": ["Маш"]})
    assert r.status_code == 200 and r.json()["role"] == "child" and r.json()["aliases"] == ["маш"]
    assert client.patch(f"/admin/api/profiles/{p.id}", headers=auth(), json={"role": "king"}).status_code == 400
    assert client.patch(f"/admin/api/profiles/{p.id}", headers=auth(), json={"modules": ["nope"]}).status_code == 400
    r = client.patch(f"/admin/api/profiles/{p.id}", headers=auth(), json={"clear_age": True})
    assert r.json()["age"] is None
    profiles.store.delete(p.id)


def test_admin_merge_moves_voice(client):
    new = profiles.store.create("Миша", "newcomer")
    voices.store.save(new.id, [b"RIFF"], [[1.0, 0.0]], "m1")
    profiles.store.update(new.id, voice={"samples": 1, "updated_at": 1.0})
    r = client.post(f"/admin/api/profiles/{new.id}/merge", headers=auth(), json={"into": "older"})
    assert r.status_code == 200 and r.json()["voice"]["samples"] == 1
    assert profiles.store.get(new.id) is None
    assert "older" in voices.store.embeddings()
    assert client.delete("/admin/api/profiles/older", headers=auth()).json() == {"ok": True}
    assert "older" not in voices.store.embeddings()
    profiles.store.migrate_from_yaml({})   # ничего не делает: файл уже есть
    profiles.store.items["older"] = profiles.Profile(id="older", name="Старший", role="child", age=9,
                                                     aliases=["старш"])


def test_newcomer_is_under_restrictions_for_others(at):
    """Ребёнок познакомился под чужим именем → на новичка действуют ограничения с who, но не послабления."""
    at(2026, 11, 16, 15, 30)                                   # пн, уроки для older
    kid = profiles.store.create("Вася", "newcomer", age=9)
    assert policy.resolve("older").mode == "homework"
    assert policy.resolve(kid.id).mode == "homework"
    assert policy.resolve("younger").mode == "normal"          # подтверждённый профиль — только свои правила
    policy.relax(["older"])                                    # родитель снял уроки старшему
    assert policy.resolve("older").mode == "normal"
    assert policy.resolve(kid.id).mode == "homework"           # послабление новичка не касается
    assert policy.resolve(None).mode == "homework"
    profiles.store.update(kid.id, role="child")                # родитель подтвердил в админке
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
