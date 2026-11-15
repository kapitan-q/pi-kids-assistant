"""Импорт прежних JSON (profiles/roles/state) в SQLite при первом запуске."""
import json
import time

import db


def test_import_json(tmp_path, monkeypatch):
    core = tmp_path / "core"
    core.mkdir()
    (core / "roles.json").write_text(json.dumps({"roles": [
        {"id": "guest", "title": "Гость", "control": False, "allow_modules": ["books"], "deny_modules": [],
         "deny_tags": [], "created_at": 0},
        {"id": "admin", "title": "Админ", "control": True, "allow_modules": ["books"], "deny_modules": [],
         "deny_tags": [], "created_at": 1},
        {"id": "child", "title": "Дети", "control": False, "allow_modules": ["books"], "deny_modules": ["yt"],
         "deny_tags": ["страшные"], "created_at": 2}]}), "utf-8")
    (core / "profiles.json").write_text(json.dumps({"profiles": [
        {"id": "dad", "name": "Папа", "roles": ["admin"], "age": None, "aliases": [], "voice": {"samples": 0},
         "created_at": 1},
        {"id": "older", "name": "Старший", "roles": ["child"], "age": 9, "aliases": ["старш"],
         "voice": {"samples": 4, "updated_at": 5.0}, "created_at": 2},
        {"id": "old", "name": "Старый", "role": "newcomer", "created_at": 3}]}), "utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"overrides": [{"id": "o1", "mode": "quiet", "until_ts": time.time() + 600,
                                                "who": ["older"]}],
                                 "announced": {"k": time.time()}, "budgets": {}, "onboarding_enabled": False}))
    monkeypatch.setattr(db, "DATA_DIR", core)
    monkeypatch.setattr(db, "DB_PATH", core / "speaker.db")
    monkeypatch.setattr(db, "STATE_PATH", state)
    saved = db._conn
    db._conn = None
    try:
        rows = {r["role_id"] + ":" + r["right_id"] + ":" + r["effect"]: json.loads(r["vals"])
                for r in db.q("SELECT * FROM role_right")}
        assert "admin:core.control:allow" in rows and "admin:core.modes_exempt:allow" in rows
        assert rows["child:books.tags:deny"] == ["страшные"] and "child:yt:deny" in rows
        assert "guest:books:allow" in rows and not any(k.startswith("parent:") for k in rows)
        users = {r["id"]: dict(r) for r in db.q("SELECT * FROM user")}
        assert users["older"]["birth_date"].endswith("-01-01") and users["older"]["voice_samples"] == 4
        assert [r[0] for r in db.q("SELECT role_id FROM user_role WHERE user_id='old'")] == []
        assert db.q1("SELECT who FROM override WHERE id='o1'")["who"] == '["older"]'
        assert db.setting("onboarding_enabled") is False
        assert (core / "profiles.json.migrated").exists() and not (core / "roles.json").exists()
        assert db.q1("PRAGMA user_version")[0] == len(db.MIGRATIONS)
    finally:
        db._conn.close()
        db._conn = saved
