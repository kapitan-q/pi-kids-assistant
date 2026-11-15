import os
import sys
import tempfile
from pathlib import Path

import pytest

TMP = Path(tempfile.mkdtemp())
CONFIG = TMP / "speaker.yaml"
CONFIG.write_text("""
timezone: Europe/Belgrade
unknown_speaker_matches: true
parent: {passphrase: "капитан немо"}
modes:
  normal: {title: Обычный режим}
  quiet: {title: Тихий час, allow_children: false, default_minutes: 60}
  homework: {title: Режим уроков, allow_children: false}
  night: {title: Ночной режим, max_volume: 30, play_budget_min: 60, tags: ["на ночь"],
          budget_message: budget, tags_message: tags}
schedule:
  - {days: [mon-fri], from: "15:00", to: "17:00", mode: homework, who: [older]}
  - {from: "21:30", to: "07:00", mode: night}
  - {mode: normal}
""", "utf-8")
os.environ["POLICY_FILE"] = str(CONFIG)
os.environ["STATE_FILE"] = str(TMP / "state" / "state.json")
os.environ["CORE_DATA_DIR"] = str(TMP / "core")
os.environ["CORE_DB"] = str(TMP / "core" / "speaker.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import policy  # noqa: E402
import profiles  # noqa: E402
import rights  # noqa: E402

BOOKS_TAGS = rights.Right("books.tags", "Теги книг", kind="values", module="books", applies_to="tags")
rights._module_rights["books"] = [BOOKS_TAGS]     # как будто модуль объявил
rights._fetched_at = 10 ** 12                      # не ходить в модули


def seed():
    """Роли и люди как на titan: Гость, Админ, Дети (сказки); Папа — админ, Старший и Младший — дети."""
    db.ex("DELETE FROM user")
    db.ex("DELETE FROM role WHERE id != 'guest'")
    profiles.roles.update("guest", allow={"books": []}, deny={})
    for rid, title, allow in (("admin", "Админ", {"books": [], rights.CONTROL: [], rights.MODES_EXEMPT: []}),
                              ("child", "Дети", {"books": []})):
        db.ex("INSERT INTO role(id, title, created_at) VALUES(?, ?, 1)", (rid, title))
        profiles.roles.update(rid, allow=allow)
    for uid, name, roles, age, aliases in (("dad", "Папа", ["admin"], None, []),
                                           ("older", "Старший", ["child"], 9, ["старш"]),
                                           ("younger", "Младший", ["child"], 7, ["младш"])):
        db.ex("INSERT INTO user(id, name, birth_date, aliases, created_at) VALUES(?, ?, ?, ?, ?)",
              (uid, name, db.birth_from_age(age), f'{aliases}'.replace("'", '"'), len(uid)))
        profiles.store.update(uid, roles=roles)


seed()


@pytest.fixture(autouse=True)
def clean_state():
    db.ex("DELETE FROM override")
    db.ex("DELETE FROM mark")
    db.ex("DELETE FROM setting")
    yield


@pytest.fixture
def reseed():
    yield
    seed()


@pytest.fixture
def at(monkeypatch):
    """Заморозить policy.now() на заданном моменте."""
    def _set(y, mo, d, h, mi=0):
        from datetime import datetime
        t = datetime(y, mo, d, h, mi, tzinfo=policy.tz())
        monkeypatch.setattr(policy, "now", lambda: t)
        return t
    return _set
