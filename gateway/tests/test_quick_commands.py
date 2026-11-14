import pytest

import quick_commands as qc


@pytest.mark.parametrize("text,action", [
    ("Стоп!", {"type": "stop"}),
    ("хватит", {"type": "stop"}),
    ("Выключи, пожалуйста.", {"type": "stop"}),
    ("подожди", None),
    ("Продолжай.", {"type": "resume"}),
    ("давай продолжай", {"type": "resume"}),
    ("перемотай", {"type": "seek", "seconds": 30, "mode": "relative"}),
    ("перемотай вперёд", {"type": "seek", "seconds": 30, "mode": "relative"}),
    ("перемотай сильно вперёд", {"type": "seek", "seconds": 120, "mode": "relative"}),
    ("чуть назад", {"type": "seek", "seconds": -15, "mode": "relative"}),
    ("я не расслышал", {"type": "seek", "seconds": -15, "mode": "relative"}),
    ("давай с самого начала", {"type": "seek", "seconds": 0, "mode": "absolute"}),
    ("Сначала", {"type": "seek", "seconds": 0, "mode": "absolute"}),
    ("потише", {"type": "volume", "change": -10}),
    ("Сделай потише, пожалуйста", {"type": "volume", "change": -10}),
    ("намного тише", {"type": "volume", "change": -30}),
    ("погромче", {"type": "volume", "change": 10}),
    ("громкость на максимум", {"type": "volume", "level": 100}),
    ("на минимум", {"type": "volume", "level": 10}),
    ("Громкость пятьдесят", {"type": "volume", "level": 50}),
    ("громкость 70", {"type": "volume", "level": 70}),
    ("громкость сорок пять", {"type": "volume", "level": 40}),
])
def test_quick(text, action):
    assert qc.parse(text) == {"action": action}


@pytest.mark.parametrize("text", [
    "включи сказку про колобка",
    "выключи свет и расскажи сказку",
    "почему небо голубое",
    "стоп а почему колобок убежал",
    "продолжай рассказывать про динозавров",
    "давай другую сказку",
    "громкость какая сейчас",
    "",
])
def test_goes_to_agent(text):
    assert qc.parse(text) is None
