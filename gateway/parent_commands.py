"""Разбор родительских голосовых команд (после кодовой фразы).

Примеры:
  «тихий час»                         -> quiet на default_minutes
  «тихий час на сорок пять минут»     -> quiet на 45 мин
  «режим уроков для старшего до пяти» -> homework до 17:00 (ближайшие 5 или 17 часов)
  «ночной режим»                      -> night
  «отбой тишины» / «обычный режим»    -> снять (если разрешено в policies.yaml)
"""
import re
from datetime import datetime, timedelta

NUM_WORDS = {
    "ноль": 0, "одну": 1, "один": 1, "одна": 1, "две": 2, "два": 2, "три": 3, "четыре": 4,
    "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
    "одиннадцать": 11, "двенадцать": 12, "тринадцать": 13, "четырнадцать": 14,
    "пятнадцать": 15, "шестнадцать": 16, "семнадцать": 17, "восемнадцать": 18,
    "девятнадцать": 19, "двадцать": 20, "тридцать": 30, "сорок": 40, "пятьдесят": 50,
    "шестьдесят": 60, "семьдесят": 70, "восемьдесят": 80, "девяносто": 90, "сто": 100,
    "полтора": 1.5, "полторы": 1.5,
}
HOUR_WORDS = {
    "часу": 1, "одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6,
    "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10, "одиннадцати": 11, "двенадцати": 12,
}
MIN_RE = re.compile(r"^мин(ут[аы]?|уту|\.)?$")
HOUR_RE = re.compile(r"^час(а|ов|ик|ика)?$")

# порядок важен: «отбой тишины» — это снятие, а не тишина
MODE_PATTERNS = [
    ("clear", r"отбой|отмен|сними|выключи режим|обычн\w* режим|можно отвечать"),
    ("homework", r"урок"),
    ("night", r"ночн|режим сна"),
    ("quiet", r"тих\w* час|не отвечай|помолчи|тишин|молчи"),
]


def normalize(text: str) -> str:
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s:.]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def strip_passphrase(text: str, passphrase: str) -> str | None:
    """Если фраза начинается с кодовой — вернуть остаток, иначе None."""
    if not passphrase:
        return None
    t, p = normalize(text), normalize(passphrase)
    if t == p or t.startswith(p + " ") or t.startswith(p + "."):
        return t[len(p):].strip(" .")
    return None


def _next_time(hour: int, minute: int, now: datetime) -> datetime:
    """«до пяти» в 15:00 — это 17:00: берём ближайшее из h и h+12."""
    candidates = []
    for h in {hour % 24, (hour + 12) % 24} if hour < 12 else {hour % 24}:
        dt = now.replace(hour=h, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt += timedelta(days=1)
        candidates.append(dt)
    return min(candidates)


def _number_before(tokens: list[str], i: int) -> float | None:
    """Число словами или цифрами перед tokens[i]: «сорок пять», «45», «два с половиной»."""
    j, total, found, half = i - 1, 0.0, False, False
    if j >= 1 and tokens[j] == "половиной" and tokens[j - 1] == "с":
        half, j = True, j - 2
    while j >= 0:
        tok = tokens[j]
        if tok.isdigit() and not found:
            total, found = total + int(tok), True
        elif tok in NUM_WORDS:
            total, found = total + NUM_WORDS[tok], True
        else:
            break
        j -= 1
    if not found:
        return None
    return total + (0.5 if half else 0)


def parse_duration(t: str) -> int | None:
    """Длительность в минутах: «сорок пять минут», «1 час 30 минут», «полчаса», «на час»."""
    tokens = t.replace(".", " ").split()
    minutes, found = 0.0, False
    for i, tok in enumerate(tokens):
        if tok in ("полчаса", "полчасика"):
            minutes, found = minutes + 30, True
            continue
        is_min, is_hour = bool(MIN_RE.match(tok)), bool(HOUR_RE.match(tok))
        if not (is_min or is_hour):
            continue
        n = _number_before(tokens, i)
        if n is None:
            if not is_hour:
                continue
            n = 1  # «на час»
        minutes += n * (60 if is_hour else 1)
        found = True
    return int(minutes) if found and minutes > 0 else None


def parse_until(t: str, now: datetime) -> datetime | None:
    t = re.sub(r"тих\w* час\w*", " ", t)  # чтобы «тихий час» не считался длительностью
    if m := re.search(r"\bдо\s+(\d{1,2})(?:[:.\s](\d{2}))?\b", t):
        return _next_time(int(m.group(1)), int(m.group(2) or 0), now)
    if m := re.search(r"\bдо\s+(" + "|".join(HOUR_WORDS) + r")\b", t):
        return _next_time(HOUR_WORDS[m.group(1)], 0, now)
    minutes = parse_duration(t)
    return now + timedelta(minutes=minutes) if minutes else None


def parse_who(t: str, profiles: dict) -> list[str] | None:
    who = [pid for pid, p in profiles.items()
           if p.get("role") != "parent" and any(a.lower() in t for a in p.get("aliases", []))]
    return who or None


def parse(text: str, profiles: dict, now: datetime) -> dict | None:
    t = normalize(text)
    for mode, pattern in MODE_PATTERNS:
        if re.search(pattern, t):
            cmd = {"mode": mode, "who": parse_who(t, profiles), "until": None}
            if mode != "clear":
                cmd["until"] = parse_until(t, now)
            return cmd
    return None
