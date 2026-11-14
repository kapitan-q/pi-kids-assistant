"""Схема команд плееру и страховка шлюза (enforce) — без сети."""
import asyncio

import pytest

import actions
import app
import policy

MODS = {"books": {"url": "http://books:8000"}}
PLAY = {"type": "play", "source": "books", "item_id": "abc-1", "title": "Колобок",
        "tags": ["народная"], "path": "stream/abc-1", "age_min": 5}


def test_validate_play():
    act = actions.validate(PLAY, MODS)
    assert act["item_id"] == "abc-1" and act["path"] == "stream/abc-1"


@pytest.mark.parametrize("bad", [
    {**PLAY, "source": "youtube"},                       # модуль не подключён
    {**PLAY, "item_id": "../etc"},                        # кривой id
    {**PLAY, "path": "https://evil.example/x.mp3"},       # абсолютный url
    {**PLAY, "path": "stream/../../etc/passwd"},
    {**PLAY, "path": None},
    {"type": "explode"},
    {"type": "volume"},                                   # ни level, ни change
    {"type": "volume", "level": 500},
    {"type": "seek", "mode": "relative"},                 # нет seconds
])
def test_validate_rejects(bad):
    assert actions.validate(bad, MODS) is None


def test_to_pi_builds_url_and_legacy_id():
    act = app.to_pi(actions.validate(PLAY, MODS))
    assert act["url"] == f"{app.PUBLIC_BASE_URL}/m/books/stream/abc-1"
    assert act["tale_id"] == "abc-1" and "path" not in act


def run(action, device="d1", playing=(None, None), speaker=None):
    res = policy.resolve(speaker)
    return asyncio.run(app.enforce(action, "ok", res, policy.limits(res), device, playing, speaker, False))


def test_enforce_night_tags(at):
    at(2026, 11, 16, 23)
    act, reply, _ = run(actions.validate(PLAY, MODS))
    assert act is None and reply == "tags"
    act, _, lim = run(actions.validate({**PLAY, "tags": ["на ночь"]}, MODS))
    assert act["type"] == "play" and lim["stop_after_sec"]


def test_enforce_resume_uses_now_playing_cache(at):
    at(2026, 11, 16, 12)
    run(actions.validate({**PLAY, "tags": ["на ночь"]}, MODS), device="d2")   # днём включили
    at(2026, 11, 16, 23)
    act, _, _ = run({"type": "resume"}, device="d2", playing=("books", "abc-1"))
    assert act == {"type": "resume"}


def test_enforce_resume_unknown_tags_is_denied(at, monkeypatch):
    at(2026, 11, 16, 23)

    async def no_module(*_a, **_k):
        return None
    monkeypatch.setattr(app.modules, "call_svc", no_module)
    act, reply, _ = run({"type": "resume"}, device="d3", playing=("books", "other"))
    assert act == {"type": "stop"} and reply == "tags"


def test_enforce_resume_nothing_playing_is_allowed(at):
    at(2026, 11, 16, 23)
    act, _, _ = run({"type": "resume"}, device="d4")
    assert act == {"type": "resume"}


def test_enforce_age_by_profile(at):
    at(2026, 11, 16, 12)
    act, reply, _ = run(actions.validate({**PLAY, "age_min": 8}, MODS), speaker="younger")   # 7 лет
    assert act is None and reply
    act, _, _ = run(actions.validate({**PLAY, "age_min": 8}, MODS), speaker="older")         # 9 лет
    assert act["type"] == "play"


def test_enforce_volume_clamped_at_night(at):
    at(2026, 11, 16, 23)
    act, _, _ = run({"type": "volume", "level": 100})
    assert act["level"] == 30
