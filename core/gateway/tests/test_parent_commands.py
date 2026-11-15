from datetime import datetime

import pytest

import parent_commands as pc

PROFILES = {"dad": {"role": "parent"},
            "older": {"role": "child", "aliases": ["старш"]},
            "younger": {"role": "child", "aliases": ["младш"]}}


@pytest.mark.parametrize("text,minutes", [
    ("на сорок пять минут", 45),
    ("на 40 минут", 40),
    ("на полтора часа", 90),
    ("на два с половиной часа", 150),
    ("на 1 час 30 минут", 90),
    ("на час", 60),
    ("на полчаса", 30),
    ("на двадцать минут", 20),
    ("на два часа", 120),
])
def test_duration(text, minutes):
    assert pc.parse_duration(pc.normalize(text)) == minutes


def test_no_duration():
    assert pc.parse_duration("тихий режим") is None


def test_quiet_hour_is_not_duration():
    now = datetime(2026, 11, 16, 12, 0)
    cmd = pc.parse("тихий час", PROFILES, now)
    assert cmd == {"mode": "quiet", "who": None, "until": None}


def test_quiet_hour_with_duration():
    now = datetime(2026, 11, 16, 12, 0)
    cmd = pc.parse("тихий час на сорок пять минут", PROFILES, now)
    assert cmd["mode"] == "quiet" and (cmd["until"] - now).seconds == 45 * 60


def test_homework_until_five_for_older():
    now = datetime(2026, 11, 16, 15, 0)
    cmd = pc.parse("режим уроков для старшего до пяти", PROFILES, now)
    assert cmd["mode"] == "homework" and cmd["who"] == ["older"]
    assert cmd["until"] == datetime(2026, 11, 16, 17, 0)


def test_until_digits():
    now = datetime(2026, 11, 16, 15, 0)
    cmd = pc.parse("ночной режим до 7:30", PROFILES, now)
    assert cmd["until"] == datetime(2026, 11, 16, 19, 30)


def test_clear():
    assert pc.parse("отбой тишины", PROFILES, datetime(2026, 11, 16))["mode"] == "clear"
    assert pc.parse("обычный режим", PROFILES, datetime(2026, 11, 16))["mode"] == "clear"


def test_passphrase():
    assert pc.strip_passphrase("Капитан Немо, тихий час", "капитан немо") == "тихий час"
    assert pc.strip_passphrase("капитан немо", "капитан немо") == ""
    assert pc.strip_passphrase("капитан немов включи", "капитан немо") is None
    assert pc.strip_passphrase("включи колобка", "капитан немо") is None


@pytest.mark.parametrize("text", ["сними с паузы", "замолчи", "отмена", "сказка про уроки", "молчи",
                                  "включи сказку про тишину", "расскажи про ночной режим у ежей"])
def test_not_commands(text):
    assert pc.parse(text, PROFILES, datetime(2026, 11, 16, 12)) is None


def test_who_by_name_and_role():
    targets = {**PROFILES, "masha": {"name": "Маша"}, "child": {"name": "Дети"}}
    now = datetime(2026, 11, 16, 12)
    assert pc.parse("тихий час для маши", targets, now)["who"] == ["masha"]
    assert pc.parse("уроки для детей до пяти", targets, now)["who"] == ["child"]
    assert pc.parse("отбой режимов для старшего", targets, now) == {"mode": "clear", "who": ["older"], "until": None}
