"""Знакомство голосом: «давай познакомимся» → имя → возраст → несколько фраз → новый профиль.

Конечный автомат на устройство, без Claude. Каждая реплика (с достаточной речью и похожая на
предыдущие) — образец голоса. Новый профиль получает роль newcomer_role; роль выше назначает
только родитель в /admin. Голос к существующему профилю здесь не привязывается — иначе ребёнок
мог бы назваться «Папой»; для этого в админке есть «объединить». Исключение — человек уже узнан
по голосу: тогда можно дописать образцы к его же профилю.
"""
import logging
import re
import time
from dataclasses import dataclass, field

import lang
import parent_commands
import policy
import profiles
import voices

log = logging.getLogger("onboarding")

TRIGGER = re.compile(r"^(?:ну |а |колонка )?(?:давай (?:познакомимся|знакомиться)|познакомься со мной|"
                     r"запомни (?:мой голос|меня)|давай я тебе (?:представлюсь|скажу как меня зовут))$")
CANCEL = re.compile(r"^(?:стоп|хватит|отмена|отмени|не надо|не хочу|потом|не сейчас)(?: .*)?$")
YES = {"да", "ага", "угу", "правильно", "верно", "точно", "конечно", "хочу", "давай"}
NO = {"нет", "неправильно", "неверно", "не", "ошиблась", "неа"}
NAME_PREFIX = re.compile(r"^(?:меня зовут|мое имя|моё имя|зовут|я|это|меня)\s+")
NO_AGE = re.compile(r"взросл|не скажу|секрет|большой|большая")
YEARS = re.compile(r"^(?:лет|год|года|годик|годика|мне)$")


@dataclass
class Utterance:
    text: str
    audio: bytes | None = None
    vector: list[float] | None = None
    speech_sec: float = 0.0
    model: str | None = None


@dataclass
class Session:
    state: str
    started: float = field(default_factory=time.time)
    touched: float = field(default_factory=time.time)
    name: str | None = None
    age: int | None = None
    target: str | None = None          # дописываем голос к этому профилю (уже узнан)
    phrase: int = 0
    retries: int = 0
    samples: list[bytes] = field(default_factory=list)
    vectors: list[list[float]] = field(default_factory=list)
    model: str | None = None


sessions: dict[str, Session] = {}


def conf() -> dict:
    return policy.config().get("voice") or {}


def phrases() -> list[str]:
    return list(lang.get("onboarding.phrases") or [])


def is_trigger(text: str) -> bool:
    return bool(TRIGGER.match(parent_commands.normalize(text)))


def active(device_id: str) -> Session | None:
    s = sessions.get(device_id)
    if s and time.time() - s.touched > float(conf().get("session_timeout_sec", 120)):
        log.info("знакомство на %s истекло", device_id)
        sessions.pop(device_id, None)
        return None
    return s


def _say(key: str, **kw) -> dict:
    return {"reply": lang.t(f"onboarding.{key}", **kw), "expect_reply": True}


def _end(device_id: str, key: str, **kw) -> dict:
    sessions.pop(device_id, None)
    return {"reply": lang.t(f"onboarding.{key}", **kw), "expect_reply": False}


def _collect(s: Session, u: Utterance) -> str:
    """Добавить образец. 'ok' | 'short' (мало речи) | 'other' (не похоже на прежние образцы)."""
    if not u.audio or not u.vector or u.speech_sec < float(conf().get("min_speech_sec", 0.8)):
        return "short"
    if s.model and u.model != s.model:
        return "short"
    if s.vectors and voices.cos(u.vector, voices.mean(s.vectors)) < float(conf().get("consistency", 0.5)):
        return "other"
    s.samples.append(u.audio)
    s.vectors.append(u.vector)
    s.model = u.model
    return "ok"


def parse_name(text: str) -> str | None:
    t = NAME_PREFIX.sub("", parent_commands.normalize(text)).strip(" .")
    words = [w for w in t.split() if w not in ("зовут", "меня")]
    if not words or len(words) > 3 or any(w.isdigit() for w in words):
        return None
    return " ".join(w.capitalize() for w in words)


def parse_age(text: str) -> int | None | str:
    """Возраст числом; None — не хочет говорить/взрослый; 'retry' — не понятно."""
    t = parent_commands.normalize(text)
    if NO_AGE.search(t):
        return None
    total, found = 0, False
    for w in t.split():
        if w.isdigit():
            total, found = total + int(w), True
        elif w in parent_commands.NUM_WORDS:
            total, found = total + int(parent_commands.NUM_WORDS[w]), True
        elif not YEARS.match(w) and found:
            break
    return total if found and 1 <= total <= 120 else "retry"


def _answer(text: str) -> str | None:
    words = parent_commands.normalize(text).split()
    if not words:
        return None
    if words[0] in YES:
        return "yes"
    if words[0] in NO:
        return "no"
    return None


def start(device_id: str, u: Utterance, known: str | None) -> dict:
    """Начать знакомство. known — профиль, который уверенно узнан по голосу (или None)."""
    if not u.audio:
        return {"reply": lang.t("onboarding.voice_only"), "expect_reply": False}
    if known and profiles.store.get(known):
        s = sessions[device_id] = Session(state="known", target=known, name=profiles.store.get(known).name)
        return _say("known", name=s.name)
    if len(profiles.store.all()) >= int(policy.config().get("max_profiles", 12)):
        return {"reply": lang.t("onboarding.too_many"), "expect_reply": False}
    sessions[device_id] = Session(state="name")
    log.info("знакомство на %s: начато", device_id)
    return _say("ask_name")


def step(device_id: str, u: Utterance) -> dict:
    s = active(device_id)
    if s is None:
        return {"reply": "", "expect_reply": False}
    s.touched = time.time()
    norm = parent_commands.normalize(u.text)
    if CANCEL.match(norm):
        return _end(device_id, "cancelled")

    if s.state == "known":
        if _answer(u.text) == "yes":
            s.state, s.phrase = "phrase", 0
            return _say("repeat_intro", phrase=phrases()[0])
        return _end(device_id, "known_no")

    if s.state == "name":
        name = parse_name(u.text)
        if not name:
            return _say("ask_name_again")
        _collect(s, u)
        s.name, s.state = name, "confirm"
        return _say("confirm_name", name=name)

    if s.state == "confirm":
        ans = _answer(u.text)
        if ans == "yes":
            _collect(s, u)
            s.state = "age"
            return _say("ask_age", name=s.name)
        if ans == "no":
            s.state = "name"
            return _say("ask_name_again")
        name = parse_name(u.text)                  # сразу сказал имя ещё раз
        if name:
            s.name = name
            return _say("confirm_name", name=name)
        return _say("confirm_name", name=s.name)

    if s.state == "age":
        age = parse_age(u.text)
        if age == "retry" and s.retries < 1:
            s.retries += 1
            return _say("ask_age_again")
        _collect(s, u)
        s.age = None if age == "retry" else age
        s.state, s.phrase, s.retries = "phrase", 0, 0
        return _say("repeat_intro", phrase=phrases()[0])

    if s.state == "phrase":
        items = phrases()
        got = _collect(s, u)
        if got != "ok" and s.retries < 2:
            s.retries += 1
            key = "repeat_other_voice" if got == "other" else "repeat_again"
            return _say(key, name=s.name, phrase=items[s.phrase])
        s.phrase, s.retries = s.phrase + 1, 0
        if s.phrase < len(items):
            return _say("repeat_next", phrase=items[s.phrase])
        return finish(device_id, s)

    return _end(device_id, "cancelled")


def finish(device_id: str, s: Session) -> dict:
    if len(s.vectors) < int(conf().get("min_samples", 4)):
        log.info("знакомство на %s: мало образцов (%d)", device_id, len(s.vectors))
        return _end(device_id, "failed")
    if s.target:
        n = voices.store.save(s.target, s.samples, s.vectors, s.model)
        profiles.store.update(s.target, voice={"samples": n, "updated_at": time.time()})
        log.info("знакомство на %s: дописан голос %s (%d образцов)", device_id, s.target, n)
        return _end(device_id, "done_again", name=s.name)
    p = profiles.store.create(s.name, policy.config().get("newcomer_role", "newcomer"), s.age)
    n = voices.store.save(p.id, s.samples, s.vectors, s.model)
    profiles.store.update(p.id, voice={"samples": n, "updated_at": time.time()}, last_seen=time.time())
    log.info("знакомство на %s: новый профиль %s (%s, %s лет, %d образцов)", device_id, p.id, s.name, s.age, n)
    return _end(device_id, "done", name=s.name)
