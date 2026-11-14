"""MySQL-библиотека сказок с полнотекстовым поиском (FULLTEXT ... WITH PARSER ngram)."""
import os
import re
import time
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

CONN = dict(
    host=os.getenv("MYSQL_HOST", "mysql"),
    port=int(os.getenv("MYSQL_PORT", "3306")),
    user=os.getenv("MYSQL_USER", "tales"),
    password=os.getenv("MYSQL_PASSWORD", "tales"),
    database=os.getenv("MYSQL_DATABASE", "tales"),
    charset="utf8mb4",
    cursorclass=DictCursor,
    autocommit=True,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tales (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    title          VARCHAR(255) NOT NULL,
    author         VARCHAR(255) NOT NULL DEFAULT '',
    description    TEXT NULL,
    tags           VARCHAR(500) NOT NULL DEFAULT '',   -- через запятую: животные, на ночь
    age_min        TINYINT UNSIGNED NULL,
    age_max        TINYINT UNSIGNED NULL,
    duration_sec   INT UNSIGNED NULL,
    file           VARCHAR(500) NULL,                  -- путь относительно /library
    text           MEDIUMTEXT NULL,
    play_count     INT UNSIGNED NOT NULL DEFAULT 0,
    last_played_at DATETIME NULL,
    created_at     DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_file (file),
    FULLTEXT KEY ft_title (title) WITH PARSER ngram,
    FULLTEXT KEY ft_all (title, author, description, tags) WITH PARSER ngram
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

PUBLIC_FIELDS = ("id", "title", "author", "description", "tags",
                 "age_min", "age_max", "duration_sec", "file", "play_count")

# Слова, которые ребёнок говорит, но которые не помогают искать
STOPWORDS = {
    "сказка", "сказку", "сказки", "сказочку", "про", "для", "как", "что", "это",
    "включи", "поставь", "расскажи", "почитай", "давай", "хочу", "можно", "мне",
    "какую", "нибудь", "новую", "другую",
}


@contextmanager
def cursor():
    conn = pymysql.connect(**CONN)
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def init(retries: int = 30) -> None:
    """Ждём, пока MySQL поднимется, и создаём таблицу."""
    for attempt in range(retries):
        try:
            with cursor() as cur:
                cur.execute(SCHEMA)
            return
        except pymysql.err.OperationalError:
            if attempt == retries - 1:
                raise
            time.sleep(2)


def row_to_dict(row: dict) -> dict:
    d = {k: row.get(k) for k in PUBLIC_FIELDS}
    d["description"] = d["description"] or ""
    if d["duration_sec"]:
        d["minutes"] = round(d["duration_sec"] / 60)
    d["has_audio"] = bool(d["file"])
    return d


VOWELS = set("аеёиоуыэюяйь")


def _stem(word: str) -> str:
    """Грубый стемминг: отрезаем окончание, чтобы «колобка» нашёл «Колобок», «лису» — «лиса»,
    «зиму» — «зима». ngram-индекс ищет слово как цепочку биграмм, т.е. по сути подстроку.
    Согласную на конце короткого слова не трогаем: «волк» → «вол» нашёл бы «волшебство»."""
    w = word if len(word) <= 4 else word[: max(4, len(word) - 2)]
    while len(w) > 3 and w[-1] in VOWELS:
        w = w[:-1]
    return w


# Короткие служебные слова — не ищем. Остальные двухбуквенные («ёж», «уж») — ищем.
SHORT_STOPWORDS = {"на", "по", "не", "ну", "он", "мы", "вы", "ты", "же", "ли", "бы", "из", "за",
                   "от", "до", "со", "во", "ко", "то", "да", "их", "ее", "ей", "им", "но", "а", "и",
                   "в", "с", "к", "у", "о", "я"}


def stems_of(query: str) -> list[str]:
    words = re.findall(r"[\w-]+", query.lower().replace("ё", "е"))
    out = []
    for w in words:
        if w in STOPWORDS or w in SHORT_STOPWORDS:
            continue
        s = _stem(w).replace("-", "")
        out.append(s)
        # уменьшительные: «ежик» → «еж», «волчок» → «волч», «зайчик» → «зайч»
        if len(s) >= 4 and s[-2:] in ("ик", "ок", "ек"):
            out.append(s[:-2])
    return list(dict.fromkeys(s for s in out if len(s) >= 2))


def build_ft_query(query: str) -> str | None:
    stems = stems_of(query)
    # в BOOLEAN MODE слова через пробел = «любое из»
    return " ".join(stems) if stems else None


def _filters(age: int | None, max_minutes: int | None, tag: str | None):
    where, params = [], []
    if age is not None:
        where += ["(age_min IS NULL OR age_min <= %s)", "(age_max IS NULL OR age_max >= %s)"]
        params += [age, age]
    if max_minutes is not None:
        where.append("(duration_sec IS NULL OR duration_sec <= %s)")
        params.append(max_minutes * 60)
    if tag:
        where.append("tags LIKE %s")
        params.append(f"%{tag}%")
    return where, params


def search(query: str = "", age: int | None = None, max_minutes: int | None = None,
           limit: int = 5) -> list[dict]:
    limit = max(1, min(int(limit), 20))
    where, params = _filters(age, max_minutes, None)
    ft = build_ft_query(query or "")
    with cursor() as cur:
        if ft:
            # ngram находит и середину слова («лис» в «Василисе», «вед» в «медведе»), поэтому сказки
            # с совпадением от начала слова идут первыми (ws), внутри — по релевантности
            bonus, bonus_params = "", []
            for s in stems_of(query):
                pattern = r"(^|[^\p{L}])" + s.replace("е", "[её]")  # «елка» ↔ «ёлка»
                bonus += " + (REGEXP_LIKE(CONCAT_WS(' ', title, tags), %s, 'i')) * 4" \
                         " + (REGEXP_LIKE(CONCAT_WS(' ', author, description), %s, 'i')) * 2"
                bonus_params += [pattern, pattern]
            sql = (
                "SELECT *, "
                " MATCH(title) AGAINST (%s IN BOOLEAN MODE) * 3 "
                " + MATCH(title, author, description, tags) AGAINST (%s IN BOOLEAN MODE)"
                + " AS score, 0" + bonus + " AS ws "
                "FROM tales WHERE MATCH(title, author, description, tags) AGAINST (%s IN BOOLEAN MODE)"
                + "".join(f" AND {w}" for w in where)
                + " ORDER BY ws DESC, score DESC LIMIT %s"
            )
            cur.execute(sql, [ft, ft, *bonus_params, ft, *params, limit])
        else:
            sql = ("SELECT * FROM tales" + (" WHERE " + " AND ".join(where) if where else "")
                   + " ORDER BY play_count DESC, title LIMIT %s")
            cur.execute(sql, [*params, limit])
        rows = cur.fetchall()
    return [row_to_dict(r) for r in rows]


def random_tale(tag: str | None = None, age: int | None = None,
                max_minutes: int | None = None) -> dict | None:
    where, params = _filters(age, max_minutes, tag)
    where.append("file IS NOT NULL")
    # реже слушанные выпадают чаще
    sql = ("SELECT * FROM tales WHERE " + " AND ".join(where)
           + " ORDER BY play_count + FLOOR(RAND() * 3), RAND() LIMIT 1")
    with cursor() as cur:
        cur.execute(sql, params)
        row = cur.fetchone()
    return row_to_dict(row) if row else None


def get(tale_id: int, with_text: bool = False) -> dict | None:
    with cursor() as cur:
        cur.execute("SELECT * FROM tales WHERE id = %s", (tale_id,))
        row = cur.fetchone()
    if not row:
        return None
    d = row_to_dict(row)
    if with_text and row.get("text"):
        d["text"] = row["text"]
    return d


def mark_played(tale_id: int) -> None:
    with cursor() as cur:
        cur.execute("UPDATE tales SET play_count = play_count + 1, last_played_at = NOW() "
                    "WHERE id = %s", (tale_id,))


def list_tags() -> list[str]:
    with cursor() as cur:
        cur.execute("SELECT tags FROM tales WHERE tags != ''")
        rows = cur.fetchall()
    tags = {t.strip().lower() for r in rows for t in r["tags"].split(",") if t.strip()}
    return sorted(tags)


def upsert(title: str, author: str = "", description: str = "", tags: str = "",
           age_min: int | None = None, age_max: int | None = None,
           duration_sec: int | None = None, file: str | None = None,
           text: str | None = None) -> int:
    with cursor() as cur:
        if file:
            cur.execute(
                "INSERT INTO tales (title, author, description, tags, age_min, age_max, "
                "duration_sec, file, text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) AS new "
                "ON DUPLICATE KEY UPDATE title=new.title, author=new.author, "
                "description=new.description, tags=new.tags, age_min=new.age_min, "
                "age_max=new.age_max, duration_sec=COALESCE(new.duration_sec, tales.duration_sec), "
                "text=COALESCE(new.text, tales.text), id=LAST_INSERT_ID(id)",
                (title, author, description, tags, age_min, age_max, duration_sec, file, text))
        else:
            # без файла уникального ключа нет — ищем запись без файла с тем же названием,
            # чтобы повторный add_tale обновлял, а не плодил дубликаты
            cur.execute("SELECT id FROM tales WHERE file IS NULL AND title = %s LIMIT 1", (title,))
            row = cur.fetchone()
            if row:
                cur.execute(
                    "UPDATE tales SET author=%s, description=%s, tags=%s, age_min=%s, age_max=%s, "
                    "duration_sec=COALESCE(%s, duration_sec), text=COALESCE(%s, text) WHERE id=%s",
                    (author, description, tags, age_min, age_max, duration_sec, text, row["id"]))
                return row["id"]
            cur.execute(
                "INSERT INTO tales (title, author, description, tags, age_min, age_max, "
                "duration_sec, file, text) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (title, author, description, tags, age_min, age_max, duration_sec, file, text))
        return cur.lastrowid


def files() -> dict[str, int]:
    """file -> id для всех записей с аудио."""
    with cursor() as cur:
        cur.execute("SELECT id, file FROM tales WHERE file IS NOT NULL")
        return {r["file"]: r["id"] for r in cur.fetchall()}


def delete(ids: list[int]) -> int:
    if not ids:
        return 0
    with cursor() as cur:
        cur.execute("DELETE FROM tales WHERE id IN (" + ",".join(["%s"] * len(ids)) + ")", ids)
        return cur.rowcount
