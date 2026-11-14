"""Разбор хода агента без Claude: фейковые сообщения SDK.

Запуск (в контейнере, config смонтирован в /config):
  docker compose run --rm --no-deps -v ./core/agent/tests:/app/tests agent \
    sh -c "pip install -q pytest && python -m pytest -q tests"
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import player  # noqa: E402
from claude_agent_sdk import (AssistantMessage, ResultMessage, SystemMessage, TextBlock,  # noqa: E402
                              ToolResultBlock, ToolUseBlock, UserMessage)


def text(obj) -> list[dict]:
    return [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}]


PLAY = {"player": {"type": "play", "item_id": "kolobok", "title": "Колобок", "tags": ["народная"],
                   "path": "stream/kolobok"}, "playing": "Колобок"}


def test_tool_server():
    assert app.tool_server("mcp__books__play_tale") == "books"
    assert app.tool_server("mcp__player__stop") == "player"
    assert app.tool_server("Bash") is None


def test_module_play_gets_source_from_server_name():
    act = app.player_from_result("books", text(PLAY))
    assert act["source"] == "books" and act["item_id"] == "kolobok"


def test_module_cannot_spoof_source_or_control_player():
    spoof = {"player": {**PLAY["player"], "source": "youtube"}}
    assert app.player_from_result("books", text(spoof))["source"] == "books"
    assert app.player_from_result("books", text({"player": {"type": "volume", "level": 100}})) is None
    assert app.player_from_result("books", text({"player": {"type": "stop"}})) is None


def test_player_server_controls_only():
    assert app.player_from_result("player", text({"player": {"type": "stop"}})) == {"type": "stop"}
    assert app.player_from_result("player", text(PLAY)) is None


def test_player_in_any_text_block():
    content = [{"type": "text", "text": "не json"}, {"type": "image"}, *text(PLAY)]
    assert app.player_from_result("books", content)["item_id"] == "kolobok"
    assert app.player_from_result("books", "просто текст") is None
    assert app.player_from_result("books", text(["список", "результатов"])) is None


def test_volume_action():
    assert player.volume_action({"level": 47}) == {"type": "volume", "level": 50}
    assert player.volume_action({"level": 3}) == {"type": "volume", "level": 10}
    assert player.volume_action({"change": -4}) == {"type": "volume", "change": -10}
    assert player.volume_action({"change": 300}) == {"type": "volume", "change": 90}


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def query(self, _text):
        pass

    async def receive_response(self):
        for m in self.messages:
            yield m


def turn(messages):
    s = app.Session()
    s.client = FakeClient(messages)
    return asyncio.run(app.run_turn(s, "x")), s


def result(is_error=False):
    return ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
                         num_turns=1, session_id="s", total_cost_usd=0.001)


def test_run_turn_play_and_reply():
    r, s = turn([
        SystemMessage("init", {"mcp_servers": [{"name": "books", "status": "connected"}]}),
        AssistantMessage([ToolUseBlock("t1", "mcp__books__play_tale", {"tale_id": "kolobok"})], model="m"),
        UserMessage([ToolResultBlock("t1", text(PLAY), None)]),
        AssistantMessage([TextBlock("Включаю сказку Колобок")], model="m"),
        result(),
    ])
    assert r["reply"] == "Включаю сказку Колобок"
    assert r["action"]["source"] == "books" and r["action"]["type"] == "play"
    assert not s.stale


def test_run_turn_tool_error_gives_no_action():
    r, _ = turn([
        AssistantMessage([ToolUseBlock("t1", "mcp__books__play_tale", {"tale_id": "x"})], model="m"),
        UserMessage([ToolResultBlock("t1", text(PLAY), True)]),
        AssistantMessage([TextBlock("Такой сказки нет")], model="m"),
        result(),
    ])
    assert r["action"] is None and r["reply"] == "Такой сказки нет"


def test_run_turn_marks_session_stale_when_module_down():
    _, s = turn([SystemMessage("init", {"mcp_servers": [{"name": "books", "status": "failed"}]}),
                 AssistantMessage([TextBlock("Привет")], model="m"), result()])
    assert s.stale


def test_run_turn_api_error_is_not_read_aloud():
    r, _ = turn([AssistantMessage([TextBlock("Invalid API key")], model="m"), result(is_error=True)])
    assert r["reply"] == app.FALLBACK_REPLY
