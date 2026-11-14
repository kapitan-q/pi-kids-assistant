"""Знакомство голосом — автомат без сети: «голос» — вектор, близкий к базовому вектору человека."""
import random

import pytest

import onboarding as ob
import policy
import profiles
import voices

MODEL = "test-model"


def voice(seed: int):
    rnd = random.Random(seed)
    base = [rnd.gauss(0, 1) for _ in range(64)]

    def utter(text: str, speech: float = 2.0) -> ob.Utterance:
        vec = [x + random.gauss(0, 0.25) for x in base]
        return ob.Utterance(text=text, audio=b"RIFF-fake", vector=vec, speech_sec=speech, model=MODEL)
    return utter


@pytest.fixture(autouse=True)
def clean():
    ob.sessions.clear()
    yield
    ob.sessions.clear()


def run(device, utter, texts):
    out = []
    for t in texts:
        out.append(ob.step(device, utter(t)))
    return out


def phrases():
    return ob.phrases()


def test_parsers():
    assert ob.is_trigger("Давай познакомимся!") and ob.is_trigger("запомни мой голос")
    assert not ob.is_trigger("давай познакомимся с лисой и включи сказку")
    assert ob.parse_name("Меня зовут маша") == "Маша"
    assert ob.parse_name("я Анна Мария") == "Анна Мария"
    assert ob.parse_name("включи мне пожалуйста сказку про колобка") is None
    assert ob.parse_age("мне восемь лет") == 8
    assert ob.parse_age("Мне шест лет") == 6              # так Whisper иногда слышит «шесть»
    assert ob.parse_age("двадцать пять") == 25
    assert ob.parse_age("7") == 7
    assert ob.parse_age("я уже взрослый") is None
    assert ob.parse_age("не знаю") == "retry"


def test_full_enrollment_creates_profile_without_roles():
    masha = voice(1)
    r = ob.start("d1", masha("давай познакомимся"), None)
    assert r["expect_reply"] and "зовут" in r["reply"]
    replies = run("d1", masha, ["меня зовут Маша", "да", "мне шесть лет", *phrases()])
    assert "Маша, правильно" in replies[0]["reply"]
    assert replies[-1]["expect_reply"] is False and "Маша" in replies[-1]["reply"]
    p = profiles.store.by_name("Маша")
    assert p and p.roles == [] and policy.unverified(p.id) and p.age == 6 and p.voice.samples >= 4
    pid, score = voices.store.identify(masha("проверка").vector)
    assert pid == p.id and score > 0.8
    other_pid, other_score = voices.store.identify(voice(99)("чужой").vector)
    assert other_score < 0.5
    profiles.store.delete(p.id)
    voices.store.delete(p.id)


def test_other_voice_is_not_mixed_in():
    kid, dad = voice(2), voice(3)
    ob.start("d2", kid("давай познакомимся"), None)
    run("d2", kid, ["Петя", "да", "девять"])
    r = ob.step("d2", dad(phrases()[0]))                 # вмешался взрослый
    assert "кто-то другой" in r["reply"]
    assert len(ob.sessions["d2"].vectors) == 3


def test_short_or_silent_sample_is_retried_then_fails():
    kid = voice(4)
    ob.start("d3", kid("давай познакомимся"), None)
    run("d3", kid, ["Оля", "да", "не скажу"])
    for _ in range(3 * len(phrases())):
        r = ob.step("d3", kid("м", speech=0.1))
        if not r["expect_reply"]:
            break
    assert "другой раз" in r["reply"] and profiles.store.by_name("Оля") is None


def test_cancel_and_timeout(monkeypatch):
    kid = voice(5)
    ob.start("d4", kid("давай познакомимся"), None)
    assert not ob.step("d4", kid("стоп"))["expect_reply"] and "d4" not in ob.sessions
    ob.start("d5", kid("давай познакомимся"), None)
    ob.sessions["d5"].touched -= 10_000
    assert ob.active("d5") is None


def test_name_correction():
    kid = voice(6)
    ob.start("d6", kid("давай познакомимся"), None)
    assert "Миша, правильно" in ob.step("d6", kid("Миша"))["reply"]
    assert "зовут" in ob.step("d6", kid("нет"))["reply"]
    assert "Маша, правильно" in ob.step("d6", kid("Маша"))["reply"]


def test_known_voice_adds_samples_to_own_profile_only():
    kid = voice(7)
    p = profiles.store.create("Ваня", ["child"], 8)
    r = ob.start("d7", kid("давай познакомимся"), p.id)
    assert "Ваня" in r["reply"]
    replies = run("d7", kid, ["да", *phrases()])
    assert "лучше" in replies[-1]["reply"]
    assert profiles.store.get(p.id).voice.samples == len(phrases())
    assert profiles.store.get(p.id).roles == ["child"]      # роли не тронуты
    profiles.store.delete(p.id)
    voices.store.delete(p.id)


def test_voice_only_and_profile_limit(monkeypatch):
    assert not ob.start("d8", ob.Utterance(text="давай познакомимся"), None)["expect_reply"]
    monkeypatch.setattr(ob.policy, "config", lambda: {"max_profiles": 1, "voice": {}})
    assert "много" in ob.start("d8", voice(8)("давай познакомимся"), None)["reply"]


def test_known_voice_cannot_become_someone_else():
    petya = voice(10)
    p = profiles.store.create("Петя", ["child"], 7)
    r = ob.start("d9", petya("давай познакомимся"), p.id)
    assert "Петя" in r["reply"]
    r = ob.step("d9", petya("нет я вася"))
    assert not r["expect_reply"] and "Ай-ай-ай" in r["reply"] and "Петя" in r["reply"]
    assert profiles.store.by_name("Вася") is None
    profiles.store.delete(p.id)


def test_recognized_at_finish_is_refused():
    """На старте голос не узнан (одна фраза), но по всем образцам — это Петя: второй профиль не заводим."""
    petya = voice(11)
    p = profiles.store.create("Петя", ["child"], 7)
    voices.store.save(p.id, [b"RIFF"] * 4, [petya("x").vector for _ in range(4)], MODEL)
    ob.start("d10", petya("давай познакомимся"), None)
    replies = run("d10", petya, ["Вася", "да", "семь", *phrases()])
    assert "Ай-ай-ай" in replies[-1]["reply"] and "Петя" in replies[-1]["reply"]
    assert profiles.store.by_name("Вася") is None
    profiles.store.delete(p.id)
    voices.store.delete(p.id)


def test_onboarding_can_be_closed():
    ob.policy.set_onboarding(False)
    try:
        r = ob.start("d11", voice(12)("давай познакомимся"), None)
        assert not r["expect_reply"] and "родител" in r["reply"]
        kid = voice(13)                                          # узнанный может перезаписать свой голос
        p = profiles.store.create("Таня", ["child"], 8)
        assert ob.start("d12", kid("давай познакомимся"), p.id)["expect_reply"]
        profiles.store.delete(p.id)
    finally:
        ob.policy.set_onboarding(True)
