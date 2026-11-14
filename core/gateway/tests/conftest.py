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
os.environ["STATE_FILE"] = str(TMP / "state.json")
os.environ["CORE_DATA_DIR"] = str(TMP / "core")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import policy  # noqa: E402
import profiles  # noqa: E402


def _seed():
    """Роли и люди как на titan: Дети (сказки), Папа — родитель, Старший и Младший — дети."""
    profiles.roles.update(profiles.GUEST, allow_modules=["books"])
    with profiles.roles.lock:
        profiles.roles.items["child"] = profiles.Role(id="child", title="Дети", allow_modules=["books"])
        profiles.roles._save()
    with profiles.store.lock:
        profiles.store._load()
        for p in (profiles.Profile(id="dad", name="Папа", roles=["parent"], created_at=1),
                  profiles.Profile(id="older", name="Старший", roles=["child"], age=9, aliases=["старш"], created_at=2),
                  profiles.Profile(id="younger", name="Младший", roles=["child"], age=7, aliases=["младш"],
                                   created_at=3)):
            profiles.store.items[p.id] = p
        profiles.store._save()


_seed()


@pytest.fixture(autouse=True)
def clean_state():
    policy.state.data = {"overrides": [], "announced": {}, "budgets": {}}
    yield


@pytest.fixture
def at(monkeypatch):
    """Заморозить policy.now() на заданном моменте."""
    def _set(y, mo, d, h, mi=0):
        from datetime import datetime
        t = datetime(y, mo, d, h, mi, tzinfo=policy.tz())
        monkeypatch.setattr(policy, "now", lambda: t)
        return t
    return _set
