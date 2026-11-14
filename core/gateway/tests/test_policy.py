from datetime import timedelta

import policy

NIGHT = {"from": "21:30", "to": "07:00", "mode": "night"}
HOMEWORK = {"days": ["mon-fri"], "from": "15:00", "to": "17:00", "mode": "homework"}


def test_overnight_window(at):
    # 2026-11-16 — понедельник
    assert policy.rule_window(NIGHT, at(2026, 11, 16, 23)) is not None
    win = policy.rule_window(NIGHT, at(2026, 11, 17, 6))
    assert win and win[1].hour == 7 and win[1].day == 17
    assert policy.rule_window(NIGHT, at(2026, 11, 17, 8)) is None


def test_weekday_window(at):
    assert policy.rule_window(HOMEWORK, at(2026, 11, 16, 15, 30)) is not None
    assert policy.rule_window(HOMEWORK, at(2026, 11, 21, 15, 30)) is None  # суббота


def test_schedule_who(at):
    at(2026, 11, 16, 15, 30)
    assert policy.resolve("older").mode == "homework"
    assert policy.resolve("younger").mode == "normal"
    assert policy.resolve(None).mode == "homework"  # unknown_speaker_matches: true


def test_override_beats_schedule(at):
    now = at(2026, 11, 16, 12)
    policy.add_override("quiet", now + timedelta(minutes=30), ["younger"])
    assert policy.resolve("younger").mode == "quiet"
    assert policy.resolve("older").mode == "normal"


def test_relax_over_scheduled_night(at):
    now = at(2026, 11, 16, 23)
    assert policy.resolve(None).mode == "night"
    policy.relax(None)
    res = policy.resolve(None)
    assert res.mode == "normal" and res.source == "override"
    assert res.until.hour == 7 and res.until > now


def test_relax_for_one_child(at):
    at(2026, 11, 16, 15, 30)
    policy.relax(["older"])
    assert policy.resolve("older").mode == "normal"


def test_relax_removes_overrides(at):
    now = at(2026, 11, 16, 12)
    policy.add_override("quiet", now + timedelta(hours=1))
    assert policy.relax(None) == 1
    assert policy.resolve(None).mode == "normal"


def test_check_playback_night(at):
    at(2026, 11, 16, 23)
    res = policy.resolve(None)
    assert policy.check_playback(res, "животные") == "tags"
    assert policy.check_playback(res, "животные, на ночь") is None
    assert policy.check_playback(res, None) is None  # неизвестная сказка — только бюджет


def test_check_playback_budget(at, monkeypatch):
    at(2026, 11, 16, 23)
    res = policy.resolve(None)
    policy.start_budget(res)
    policy.state.data["budgets"][res.key] -= 61 * 60
    assert policy.check_playback(res, "на ночь") == "budget"


def test_check_playback_blocked(at):
    now = at(2026, 11, 16, 12)
    policy.add_override("quiet", now + timedelta(hours=1))
    assert policy.check_playback(policy.resolve(None), "на ночь")
