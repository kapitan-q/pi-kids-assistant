"""Пользователи, роли, права и админка /admin."""
import asyncio
import base64

import bcrypt
import pytest
from fastapi.testclient import TestClient

import admin
import app
import db
import policy
import profiles
import rights
import voices

PW = "секрет-родителя"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_PASSWORD_HASH", bcrypt.hashpw(PW.encode(), bcrypt.gensalt(4)))
    monkeypatch.setattr(admin, "ADMIN_USER", "parent")
    return TestClient(app.app)


def auth(user="parent", pw=PW):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture
def no_tts(monkeypatch):
    async def none(_text):
        return None
    monkeypatch.setattr(app, "synthesize", none)


def say(text, speaker=None, device="d-t"):
    return asyncio.run(app.handle_text(device, text, speaker))


# ---------- права ----------

def test_effective_flags_and_values():
    R = rights
    roles = [{"allow": {"books": [], "books.tags": ["сказки"]}, "deny": {}},
             {"allow": {"books": []}, "deny": {"books.tags": ["Страшные"], "youtube": []}}]
    g = R.effective(roles, 7)
    assert g.has("books") and not g.has("youtube")
    assert g.values["books.tags"] == {"allow": ["сказки"], "deny": ["страшные"]}
    assert g.age == 7
    assert R.effective([{"allow": {R.ANY_AGE: []}, "deny": {}}], 7).age is None
    g = R.effective([{"allow": {"books": []}, "deny": {}}, {"allow": {}, "deny": {"books": []}}], None)
    assert not g.has("books")                                        # запрет сильнее разрешения
    assert g.item_denied("books", {"tags": ["x"]}) is False
    g = R.effective(roles, None)
    assert g.item_denied("books", {"tags": ["сказки", "страшные"]})
    assert not g.item_denied("books", {"tags": ["Сказки"]})
    assert g.item_denied("books", {"tags": ["животные"]})            # не из белого списка
    assert g.item_denied("books", {"tags": None})                    # теги неизвестны — строго


def test_users_get_rights_by_roles():
    assert policy.can_control("dad") and policy.modes_exempt("dad")
    assert not policy.can_control("older") and not policy.can_control(None)
    assert policy.grants(None).modules() == ["books"]                # гость
    u = profiles.store.create("Тест Доступ", age=6)
    assert policy.unverified(u.id) and policy.grants(u.id).modules() == ["books"]
    assert profiles.store.get(u.id).age == 6 and profiles.store.get(u.id).birth_date.endswith("-01-01")
    profiles.store.delete(u.id)


def test_guest_cannot_control(reseed):
    profiles.roles.update("guest", allow={"books": [], rights.CONTROL: [], rights.MODES_EXEMPT: []})
    assert not policy.can_control(None) and not policy.modes_exempt(None)


def test_schedule_who_by_role(at, monkeypatch):
    at(2026, 11, 16, 15, 30)
    cfg = dict(policy.config())
    cfg["schedule"] = [{"from": "15:00", "to": "17:00", "mode": "homework", "who": ["child"]}, {"mode": "normal"}]
    monkeypatch.setattr(policy, "config", lambda: cfg)
    assert policy.resolve("younger").mode == "homework"
    assert policy.resolve("dad").mode == "normal"


def test_newcomer_is_under_restrictions_for_others(at):
    """Ребёнок познакомился под чужим именем → на новичка действуют ограничения с who, но не послабления."""
    at(2026, 11, 16, 15, 30)                                   # пн, уроки для older
    kid = profiles.store.create("Вася", age=9)
    assert policy.resolve("older").mode == "homework"
    assert policy.resolve(kid.id).mode == "homework"
    assert policy.resolve("younger").mode == "normal"
    policy.relax(["older"])
    assert policy.resolve("older").mode == "normal"
    assert policy.resolve(kid.id).mode == "homework"           # послабление новичка не касается
    profiles.store.update(kid.id, roles=["child"])
    assert policy.resolve(kid.id).mode == "normal"
    profiles.store.delete(kid.id)


def test_exempt_is_not_limited(at):
    at(2026, 11, 16, 23)
    assert policy.resolve("dad").source == "exempt" and policy.limits(policy.resolve("dad"))["max_volume"] == 100
    assert policy.resolve("older").mode == "night"


# ---------- голосовые команды режимов (Б1) ----------

@pytest.mark.parametrize("text", ["сними с паузы", "замолчи", "отмена", "сказка про уроки",
                                  "включи тихую сказку", "расскажи про ночь"])
def test_ordinary_phrases_of_controller_are_not_mode_commands(text, at, no_tts, monkeypatch):
    at(2026, 11, 16, 12)

    async def agent(*_a, **_k):
        raise app.httpx.ConnectError("нет агента в тестах")
    monkeypatch.setattr(app, "http", type("H", (), {"post": agent})(), raising=False)
    d = say(text, "dad")
    assert not d.get("parent_command") and policy.overrides() == []


def test_controller_sets_and_clears_by_voice(at, no_tts):
    at(2026, 11, 16, 12)
    d = say("тихий час на десять минут", "dad")                   # без кодовой фразы
    assert d.get("parent_command") and policy.resolve("older").mode == "quiet"
    assert policy.resolve("dad").mode == "normal"                 # режимы его не блокируют
    d = say("капитан немо отбой тишины", "older")                 # кодовая фраза без права — нельзя
    assert "взрослый" in d["reply"] and policy.resolve("older").mode == "quiet"
    d = say("уроки для детей до пяти", "dad")                     # роль «Дети» по названию
    assert policy.overrides()[-1]["who"] == ["child"]
    d = say("отбой режимов", "dad")
    assert d.get("parent_command") and policy.resolve("older").mode == "normal"
    assert {a["action"] for a in db.audit_tail()} >= {"mode_on", "mode_off"}


# ---------- кто включил — тому и лимиты (Б2) ----------

def test_policy_limits_for_who_started(at, client):
    at(2026, 11, 16, 23)
    play = {"type": "play", "source": "books", "item_id": "x1", "tags": ["животные"], "path": "stream/x1"}
    res = policy.resolve("dad")
    act, _, _ = asyncio.run(app.enforce(dict(play), "", res, policy.limits(res), "pi-1", (None, None), "dad"))
    assert act["type"] == "play"
    lim = client.get("/policy", params={"device_id": "pi-1"}).json()
    assert lim["max_volume"] == 100 and lim["stop_after_sec"] is None
    res = policy.resolve("older")
    act, _, _ = asyncio.run(app.enforce({**play, "tags": ["на ночь"]}, "", res, policy.limits(res), "pi-1",
                                        (None, None), "older"))
    lim = client.get("/policy", params={"device_id": "pi-1"}).json()
    assert lim["max_volume"] == 30 and lim["stop_after_sec"]
    assert client.get("/policy").json()["mode"] == "night"        # старый Pi — как для неузнанного


def test_role_tags_rights_enforced(at, reseed):
    at(2026, 11, 16, 12)
    profiles.roles.update("child", allow={"books": []}, deny={"books.tags": ["страшные"]})
    res = policy.resolve("older")
    scary = {"type": "play", "source": "books", "item_id": "s", "tags": ["Страшные"], "path": "stream/s"}
    act, reply, _ = asyncio.run(app.enforce(scary, "", res, policy.limits(res), "d-tags", (None, None), "older"))
    assert act is None and reply
    assert app.agent_rights(res, "older") == {
        "modules": {"books": {"books.tags": {"allow": None, "deny": ["страшные"]}}}, "age": 9}


# ---------- гостевой доступ ----------

def test_guest_switch(no_tts, at, monkeypatch):
    at(2026, 11, 16, 12)
    policy.set_setting("guest_enabled", False)
    d = say("включи колобка")
    assert d.get("guest_off") and "не узнала" in d["reply"]
    assert say("стоп")["action"] == {"type": "stop"}              # плеер — можно
    kid = profiles.store.create("Новенький", age=6)
    assert "доступ" in say("включи колобка", kid.id)["reply"]
    profiles.store.delete(kid.id)


# ---------- админка ----------

def test_admin_requires_parent_password(client):
    assert client.get("/admin/api/state").status_code == 401
    assert client.get("/admin/api/state", headers=auth(pw="speaker-pass")).status_code == 401
    j = client.get("/admin/api/state", headers=auth()).json()
    assert {"guest", "admin", "child"} <= {x["id"] for x in j["roles"]}
    assert {"core.control", "books", "books.tags"} <= {r["id"] for r in j["rights"]}
    assert client.get("/admin", headers=auth()).headers["content-type"].startswith("text/html")


def test_admin_patch_user(client):
    u = profiles.store.create("Маша", age=6)
    r = client.patch(f"/admin/api/users/{u.id}", headers=auth(),
                     json={"roles": ["child", "guest"], "aliases": ["Машенька"], "birth_date": "2019-05-03"})
    j = r.json()
    assert r.status_code == 200 and j["roles"] == ["child"] and j["aliases"] == ["машенька"]
    assert not j["unverified"] and j["birth_date"] == "2019-05-03" and j["age"] >= 7
    assert client.patch(f"/admin/api/users/{u.id}", headers=auth(), json={"roles": ["king"]}).status_code == 400
    assert client.patch(f"/admin/api/users/{u.id}", headers=auth(), json={"birth_date": "вчера"}).status_code == 400
    assert client.patch(f"/admin/api/users/{u.id}", headers=auth(), json={"birth_date": ""}).json()["age"] is None
    profiles.store.delete(u.id)


def test_admin_roles_crud(client, monkeypatch, reseed):
    rid = client.post("/admin/api/roles", headers=auth(), json={"title": "Младшие"}).json()["id"]
    r = client.patch(f"/admin/api/roles/{rid}", headers=auth(),
                     json={"allow": {"books": ["лишнее"]}, "deny": {"books.tags": [" Страшные ", "страшные"]}})
    assert r.json()["allow"] == {"books": []} and r.json()["deny"] == {"books.tags": ["страшные"]}
    assert client.patch(f"/admin/api/roles/{rid}", headers=auth(), json={"allow": {"nope": []}}).status_code == 400
    assert client.patch("/admin/api/roles/guest", headers=auth(),
                        json={"allow": {rights.CONTROL: []}}).status_code == 400
    assert client.delete("/admin/api/roles/guest", headers=auth()).status_code == 400
    profiles.store.update("younger", roles=["child", rid])
    cfg = dict(policy.config())
    cfg["schedule"] = [{"mode": "quiet", "who": [rid]}]
    orig = policy.config
    monkeypatch.setattr(policy, "config", lambda: cfg)
    assert client.delete(f"/admin/api/roles/{rid}", headers=auth()).status_code == 409
    monkeypatch.setattr(policy, "config", orig)
    assert client.delete(f"/admin/api/roles/{rid}", headers=auth()).json() == {"ok": True}
    assert profiles.store.get("younger").roles == ["child"]       # снялась каскадом


def test_admin_merge_and_schedule_refs(client, reseed):
    new = profiles.store.create("Миша")
    voices.store.save(new.id, [b"RIFF"], [[1.0, 0.0]], "m1")
    profiles.store.update(new.id, voice={"samples": 1, "updated_at": 1.0})
    r = client.post(f"/admin/api/users/{new.id}/merge", headers=auth(), json={"into": "older"})
    assert r.status_code == 200 and r.json()["voice"]["samples"] == 1
    assert profiles.store.get(new.id) is None and "older" in voices.store.embeddings()
    # older — в расписании: ни удалить, ни объединить в другого нельзя
    assert client.delete("/admin/api/users/older", headers=auth()).status_code == 409
    assert client.post("/admin/api/users/older/merge", headers=auth(), json={"into": "younger"}).status_code == 409
    voices.store.delete("older")


def test_admin_control(client, at):
    at(2026, 11, 16, 15, 30)
    c = client.get("/admin/api/control", headers=auth()).json()
    rows = {row["id"]: row for row in c["people"]}
    assert rows["older"]["restricted"] and rows[None]["restricted"] and "dad" not in rows
    assert {m["id"] for m in c["modes"]} >= {"quiet", "night"}
    r = client.post("/admin/api/control/override", headers=auth(), json={"mode": "quiet", "minutes": 30,
                                                                       "who": ["younger"]})
    assert {row["id"]: row for row in r.json()["people"]}["younger"]["mode"] == "quiet"
    r = client.post("/admin/api/control/override", headers=auth(), json={"mode": "night", "until": "19:00"})
    assert {row["id"]: row for row in r.json()["people"]}["older"]["until"][11:16] == "19:00"
    assert client.post("/admin/api/control/override", headers=auth(), json={"mode": "nope"}).status_code == 400
    r = client.post("/admin/api/control/relax", headers=auth(), json={"who": None})
    assert not any(row["restricted"] for row in r.json()["people"])
    assert client.get("/admin/api/audit", headers=auth()).json()[0]["via"] == "admin"


def test_admin_settings(client):
    assert client.post("/admin/api/settings", json={"guest_enabled": False}).status_code == 401
    r = client.post("/admin/api/settings", headers=auth(), json={"guest_enabled": False, "onboarding_enabled": False})
    assert r.json()["guest_enabled"] is False and r.json()["onboarding_enabled"] is False
    assert not policy.guest_enabled() and not policy.onboarding_enabled()
