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
roles:
  parent: {title: Родитель, parent: true}
  child: {title: Ребёнок, modules: [books]}
  newcomer: {title: Новичок, modules: [books]}
  guest: {title: Гость, modules: [books]}
guest_role: guest
import_profiles:
  dad: {name: Папа, role: parent}
  older: {name: Старший, role: child, age: 9, aliases: [старш]}
  younger: {name: Младший, role: child, age: 7, aliases: [младш]}
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

profiles.store.migrate_from_yaml(policy.config()["import_profiles"])


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
