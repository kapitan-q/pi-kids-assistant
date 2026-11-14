"""Разбор родительских голосовых команд (после кодовой фразы).

Примеры:
  «тихий час»                         -> quiet на default_minutes
  «тихий час на сорок минут»          -> quiet на 40 мин
  «режим уроков для старшего до пяти» -> homework до 17:00 (ближайшие 5 или 17 часов)
  «ночной режим»                      -> night
  «отбой тишины» / «обычный режим»    -> снять все команды
"""
import re
from datetime import datetime, timedelta

NUM_WORDS = {
    "одну": 1, "один": 1, "две": 2, "два": 2, "три": 3, "четыре": 4, "пять": 5,
    "десять": 10, "пятнадцать": 15, "двадцать": 20, "тридцать": 30,
    "сорок": 40, "пятьдесят": 50, "девяносто": 90,
}
HOUR_WORDS = {
    "часу": 1, "одного": 1, "двух": 2, "трех": 3, "четырех": 4, "пяти": 5, "шести": 6,
    "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10, "одиннадцати": 11, "двенадцати": 12,
}

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
    if t.startswith(p):
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


def parse_until(t: str, now: datetime) -> datetime | None:
    t = re.sub(r"тих\w* час", " ", t)  # чтобы «тихий час» не считался длительностью
    if m := re.search(r"\bдо\s+(\d{1,2})(?:[:.\s](\d{2}))?\b", t):
        return _next_time(int(m.group(1)), int(m.group(2) or 0), now)
    if m := re.search(r"\bдо\s+(" + "|".join(HOUR_WORDS) + r")\b", t):
        return _next_time(HOUR_WORDS[m.group(1)], 0, now)
    minutes = None
    if m := re.search(r"(\d+)\s*мин", t):
        minutes = int(m.group(1))
    elif m := re.search(r"(\d+)\s*час", t):
        minutes = int(m.group(1)) * 60
    elif re.search(r"полчаса", t):
        minutes = 30
    elif re.search(r"полтора", t):
        minutes = 90
    elif m := re.search(r"(" + "|".join(NUM_WORDS) + r")\s+(минут|час)", t):
        n = NUM_WORDS[m.group(1)]
        minutes = n if m.group(2) == "минут" else n * 60
    elif re.search(r"\bна\s+час", t):
        minutes = 60
    return now + timedelta(minutes=minutes) if minutes else None


def parse_who(t: str, profiles: dict) -> list[str] | None:
    who = [pid for pid, p in profiles.items()
           if p.get("role") != "parent" and any(a.lower() in t for a in p.get("aliases", []))]
    return who or None


def parse(text: str, profiles: dict, now: datetime) -> dict | None:
    t = normalize(text)
    for mode, pattern in MODE_PATTERNS:
        if re.search(pattern, t):
            cmd = {"mode": mode, "who": parse_who(t, profiles)}
            if mode != "clear":
                cmd["until"] = parse_until(t, now)
            return cmd
    return None
