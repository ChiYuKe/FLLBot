from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import parse_event
from helpers import FakeConnection, group_message, make_bot


@pytest.mark.parametrize(("modes", "text", "mention", "expected"), [
    (["at", "prefix"], "路过闲聊", None, False),
    (["at", "prefix"], "你好", 10001, True),
    (["at", "prefix"], "木鱼，你好", None, True),
    (["at", "prefix"], "木鱼，你好", 10001, True),
    (["at", "prefix"], "木鱼", None, True),
    (["all", "at"], "你好", None, True),
    (["all", "prefix"], "普通闲聊", None, True),
    (["all", "prefix"], "木鱼，你好", None, True),
    (["at"], "木鱼，你好", None, False),
    (["prefix"], "你好", 10001, False),
])
async def test_combined_triggers_match_any_condition_only_once(tmp_path, monkeypatch, modes, text, mention, expected):
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key"})
    try:
        await bot.permissions.save_group(20002, enabled=True, config={"policy": {
            "auto_group": True, "trigger_modes": modes, "cooldown_seconds": 0,
        }})
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.nickname = "木鱼"
        await bot.dispatcher.dispatch(parse_event(group_message(text, at=mention)), connection)
        assert complete.await_count == int(expected)
        assert len(connection.texts()) == int(expected)
        if expected and "prefix" in modes and text.startswith("木鱼") and text != "木鱼":
            assert complete.call_args.args[0][-1]["content"].endswith("你好")
            assert "木鱼，你好" not in complete.call_args.args[0][-1]["content"]
    finally:
        await bot.stop()


@pytest.mark.parametrize(("mode", "text", "mention", "expected", "expected_content"), [
    ("at", "你好", None, False, ""),
    ("at", "你好", 10001, True, "测试用户(30003，成员)：你好"),
    ("prefix", "你好", None, False, ""),
    ("prefix", "木鱼 你好", None, True, "测试用户(30003，成员)：你好"),
    ("prefix", "木鱼", None, True, "测试用户(30003，成员)：（用户只叫了你的名字"),
    ("all", "你好", None, True, "测试用户(30003，成员)：你好"),
])
async def test_agent_trigger_modes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    text: str,
    mention: int | None,
    expected: bool,
    expected_content: str,
) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-test-key"})
    try:
        agent = bot.plugins.get("agent")
        assert agent is not None
        await agent.settings.update({"auto_group": True, "trigger_mode": mode, "cooldown_seconds": 0})
        complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
        monkeypatch.setattr(bot.chat, "complete", complete)
        connection = FakeConnection()
        connection.nickname = "木鱼"
        await bot.dispatcher.dispatch(parse_event(group_message(text, at=mention)), connection)
        assert complete.await_count == int(expected)
        assert bool(connection.texts()) is expected
        if expected:
            assert complete.call_args.args[0][-1]["content"].startswith(expected_content)
        if mode == "prefix" and text == "木鱼":
            # 内部提示只发给模型，历史里仍记录用户原话。
            rows = await bot.storage.recent_chat("group", 20002)
            assert [row.content for row in rows if row.role == "user"] == ["木鱼"]
    finally:
        await bot.stop()
