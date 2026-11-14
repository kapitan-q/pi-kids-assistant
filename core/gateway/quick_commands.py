"""Мгновенные команды плееру без Claude: «стоп», «продолжай», «потише», «перемотай»...

Срабатывают, только если ВСЯ фраза — команда (плюс вежливые слова вроде «пожалуйста»).
Всё остальное («выключи свет и расскажи сказку») уходит агенту как раньше.
Ответ пустой: результат ребёнок и так услышит, а речь добавила бы задержку.
"""
import re

from parent_commands import NUM_WORDS, normalize

FILLER = r"(?:пожалуйста|давай|ну|а|ой|слушай|колонка|сказочник)"


def _cmd(body: str) -> re.Pattern:
    """Фраза целиком: необязательные вежливые слова вокруг команды."""
    return re.compile(rf"^(?:{FILLER}\s+)*(?:{body})(?:\s+{FILLER})*$")


STOP = _cmd(r"стоп|хватит|выключи|выключай|выключи сказку|останови|останови сказку|"
            r"стоп сказка|замолчи|не надо больше|всё хватит|все хватит")
PAUSE = _cmd(r"пауза|поставь на паузу|подожди|погоди")
RESUME = _cmd(r"продолжай|продолжи|продолжаем|продолжить|играй дальше|рассказывай дальше|"
              r"читай дальше|включи обратно|сними с паузы")
FROM_START = _cmd(r"(?:давай |включи |перемотай )?(?:с самого начала|с начала|сначала)")
FORWARD = _cmd(r"перемотай(?: вперед| дальше| немного вперед| чуть вперед)?|пропусти|промотай")
FORWARD_BIG = _cmd(r"(?:перемотай|промотай) (?:сильно|намного|далеко) вперед")
BACK = _cmd(r"(?:перемотай |отмотай )?(?:чуть |немного )?назад|отмотай|я не (?:расслышал|расслышала|услышал|услышала)")
VOL_DOWN = _cmd(r"(?:сделай |сделай звук |говори )?(?:потише|тише)|убавь(?: звук| громкость)?")
VOL_DOWN_BIG = _cmd(r"(?:сделай )?(?:намного|гораздо|совсем|сильно) (?:тише|потише)")
VOL_UP = _cmd(r"(?:сделай |сделай звук |говори )?(?:погромче|громче)|прибавь(?: звук| громкость)?")
VOL_UP_BIG = _cmd(r"(?:сделай )?(?:намного|гораздо|сильно) (?:громче|погромче)")
VOL_MAX = _cmd(r"(?:громкость |сделай )?на (?:максимум|полную)")
VOL_MIN = _cmd(r"(?:громкость |сделай )?на минимум")
VOL_LEVEL = re.compile(rf"^(?:{FILLER}\s+)*(?:сделай )?громкость (?:на )?(?P<n>[\w ]+?)(?:\s+{FILLER})*$")

TABLE = [
    (STOP, {"type": "stop"}),
    (PAUSE, None),                                    # Pi уже на паузе — просто ничего не делать
    (RESUME, {"type": "resume"}),
    (FROM_START, {"type": "seek", "seconds": 0, "mode": "absolute"}),
    (FORWARD_BIG, {"type": "seek", "seconds": 120, "mode": "relative"}),
    (FORWARD, {"type": "seek", "seconds": 30, "mode": "relative"}),
    (BACK, {"type": "seek", "seconds": -15, "mode": "relative"}),
    (VOL_DOWN_BIG, {"type": "volume", "change": -30}),
    (VOL_DOWN, {"type": "volume", "change": -10}),
    (VOL_UP_BIG, {"type": "volume", "change": 30}),
    (VOL_UP, {"type": "volume", "change": 10}),
    (VOL_MAX, {"type": "volume", "level": 100}),
    (VOL_MIN, {"type": "volume", "level": 10}),
]


def _number(words: str) -> int | None:
    words = words.replace("процентов", "").replace("процент", "").strip()
    if words.isdigit():
        return int(words)
    total, found = 0, False
    for w in words.split():
        if w not in NUM_WORDS:
            return None
        total, found = total + NUM_WORDS[w], True
    return int(total) if found else None


def parse(text: str) -> dict | None:
    """{"action": {...} | None} — команда распознана; None — пусть решает агент."""
    t = normalize(text).strip(" .")
    if not t:
        return None
    for pattern, action in TABLE:
        if pattern.match(t):
            return {"action": dict(action) if action else None}
    if m := VOL_LEVEL.match(t):
        n = _number(m.group("n"))
        if n is not None and 0 <= n <= 100:
            return {"action": {"type": "volume", "level": max(10, round(n / 10) * 10)}}
    return None
