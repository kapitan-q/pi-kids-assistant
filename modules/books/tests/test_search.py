import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import catalog as cat  # noqa: E402


def T(i, title, tags="", description="", author="", dur=600, files=1, age=(None, None)):
    return {"id": i, "title": title, "author": author, "description": description, "tags": tags,
            "age_min": age[0], "age_max": age[1], "duration_sec": dur,
            "files": [(f"{i}/{n}.mp3", dur / files) for n in range(files)]}


@pytest.fixture
def c(tmp_path, monkeypatch):
    monkeypatch.setattr(cat, "MERGED_DIR", tmp_path)
    c = cat.Catalog()
    c.plays = cat.Plays(tmp_path / "plays.json")
    c.loaded_at = 10 ** 12                      # не ходить в ABS
    for t in [
        T("vasilisa", "Василиса Прекрасная", "народная, баба-яга", "Мачеха посылает Василису за огнём"),
        T("kuma", "О хитрой куме лисе", "лиса, животные", "Приключения умной лисички", age=(6, 10)),
        T("teterev", "Тетерев и лиса", "басня, лиса, на ночь", "Лиса зовёт тетерева", dur=140),
        T("medved", "Три медведя", "медведи, на ночь", "Девочка попала в дом трёх медведей"),
        T("serdce", "Хрустальное сердце", "ведьма, лёд", "Злая ледяная ведьма Аскольда", dur=6000),
        T("ezh", "Ёж и заяц", "ёж, заяц", "Ёж с ежихой перехитрили зайца"),
        T("volsh", "Волшебное кольцо", "волшебство", "Чудеса"),
        T("volk", "Волк и семеро козлят", "волк, коза", "Волк пробирается к козлятам"),
        T("konek", "Конёк-горбунок", "ершов", "Иван и Конёк-горбунок", files=8, dur=7300),
    ]:
        c.tales[t["id"]] = t
    return c


def ids(res):
    return [t["id"] for t in res]


@pytest.mark.parametrize("q,first,absent", [
    ("лиса", {"kuma", "teterev"}, None),
    ("про лису", {"kuma", "teterev"}, None),
    ("ведьма", {"serdce"}, None),
    ("про ёжика", {"ezh"}, None),
    ("про волка", {"volk"}, "volsh"),
    ("конька горбунка", {"konek"}, None),
])
def test_search_ranking(c, q, first, absent):
    res = ids(c.search(q))
    assert set(res[:len(first)]) == first, res
    if absent:
        assert absent not in res


def test_substring_ranks_below_word_start(c):
    res = ids(c.search("лиса"))
    assert res.index("vasilisa") > res.index("kuma")


def test_filters(c):
    assert "kuma" not in ids(c.search("лиса", age=4))      # рано: от 6 лет
    assert "kuma" in ids(c.search("лиса", age=12))         # «вырос» — всё равно находим
    assert "serdce" not in ids(c.search("ведьма", max_minutes=30))


def test_multifile_not_playable_until_merged(c, tmp_path):
    konek = c.tales["konek"]
    assert c.audio_file(konek) is None
    assert c.public(konek)["has_audio"] is False
    (tmp_path / "konek.mp3").write_bytes(b"x")
    assert c.audio_file(konek) == tmp_path / "konek.mp3"
    assert c.audio_file(c.tales["ezh"]) == cat.LIBRARY_DIR / "ezh/0.mp3"


def test_player_contract(c, monkeypatch):
    """play_* отдают ядру команду плееру: только относительный путь, теги списком, без source и url."""
    monkeypatch.setattr(c, "resume_from", lambda t: None)
    p = c.player(c.tales["kuma"])
    assert p == {"type": "play", "item_id": "kuma", "title": "О хитрой куме лисе",
                 "tags": ["лиса", "животные"], "age_min": 6, "path": "stream/kuma"}
    monkeypatch.setattr(c, "resume_from", lambda t: 125)
    assert c.player(c.tales["ezh"])["start"] == 125


def test_random_respects_tag(c):
    for _ in range(20):
        assert "на ночь" in c.random(tag="на ночь")["tags"]


def test_stems():
    assert cat.stems_of("про лису") == ["лис"]
    assert cat.stems_of("про ёжика") == ["ежик", "еж"]
    assert cat.stems_of("волк") == ["волк"]


def test_rights_filter(c):
    ids = lambda res: {t["id"] for t in res}  # noqa: E731
    assert "teterev" not in ids(c.search("лиса", deny=["басня"]))
    assert ids(c.search("", limit=20, allow=["На ночь"])) == {"teterev", "medved"}
    assert not c.permitted(c.get("kuma"), age=5) and c.permitted(c.get("kuma"), age=6)


def test_listener_headers():
    import app as books

    class Req:
        headers = {"x-ks-rights": '{"books.tags": {"allow": null, "deny": ["страшные"]}}', "x-ks-age": "7"}

    class Ctx:
        request_context = type("RC", (), {"request": Req()})()
    assert books.listener(Ctx()) == {"allow": None, "deny": ["страшные"], "age": 7}
    assert books.listener(None) == {"allow": None, "deny": None, "age": None}
    assert books._narrow_age(10, 7) == 7 and books._narrow_age(None, 7) == 7 and books._narrow_age(5, None) == 5
