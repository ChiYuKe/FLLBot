from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from fllbot.app import create_app
from fllbot.onebot.models import parse_event
from fllbot.policy import agent_settings, effective_policy
from helpers import FakeConnection, build_bot, group_message, make_bot

AUTH = {"Authorization": "Bearer admin-token"}


def test_bot_api_validation_identity_and_restart(tmp_path: Path) -> None:
    bot = build_bot(tmp_path)
    conn = FakeConnection()
    conn.get_login_info = AsyncMock(return_value={"user_id": 10001, "nickname": "木鱼"})
    with TestClient(create_app(bot)) as client:
        bot.manager.add(conn)
        assert client.get("/api/console/bots").status_code == 401
        assert client.post("/api/console/bots/refresh", headers=AUTH).json()["items"][0]["nickname"] == "木鱼"
        url = "/api/console/bots/10001/defaults"
        values = {"reply_mode": "prefix", "persona": "concise", "provider": "", "wake_word": "小助手"}
        assert client.put(url, headers=AUTH, json={**values, "wake_word": "  小助手  "}).json()["defaults"] == values
        assert client.put(url, headers=AUTH, json={**values, "wake_word": "a" * 81}).status_code == 422
        assert client.put(url, headers=AUTH, json={**values, "provider": "missing"}).status_code == 400
        assert client.put(url, headers=AUTH, json={**values, "persona": "missing"}).status_code == 400
        assert client.put(url, headers=AUTH, json={**values, "reply_mode": "invalid"}).status_code == 422
        assert client.put(url, headers=AUTH, json={**values, "trigger_prefix": "ai"}).status_code == 422
        assert client.put("/api/console/bots/999/defaults", headers=AUTH, json=values).status_code == 404
        conn.get_login_info = AsyncMock(side_effect=RuntimeError("offline"))
        result = client.post("/api/console/bots/refresh", headers=AUTH).json()
        assert result["failed"] == 1 and result["items"][0]["nickname"] == "木鱼"
    restored = build_bot(tmp_path)
    with TestClient(create_app(restored)) as client:
        account = client.get("/api/console/bots", headers=AUTH).json()["items"][0]
        assert account == {"self_id": 10001, "nickname": "木鱼", "online": False, "defaults": values}


async def test_each_bot_uses_own_nickname_and_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-key"})
    complete = AsyncMock(return_value=SimpleNamespace(text="收到", tool_calls=[]))
    monkeypatch.setattr(bot.chat, "complete", complete)
    try:
        agent = bot.plugins.get("agent")
        await agent.settings.update({"auto_group": False, "cooldown_seconds": 60})
        first, second = FakeConnection(10001), FakeConnection(10002)
        first.nickname, second.nickname = "木鱼", "猫猫"
        for conn in (first, second):
            bot.manager.add(conn)
            await bot.bots.update(conn.self_id, defaults={"reply_mode": "prefix", "persona": "concise"})
        await bot.bots.update(10001, defaults={"reply_mode": "prefix", "persona": "concise", "wake_word": "小助手"})
        for conn, text, expected in [(first, "ai 你好", False), (first, "猫猫 你好", False),
                                     (first, "木鱼，你好", False), (first, "小助手，你好", True), (second, "木鱼 你好", False),
                                     (second, "猫猫 你好", True), (second, "猫猫", False), (first, "小助手 再问一次", False)]:
            complete.reset_mock()
            payload = group_message(text)
            payload["self_id"] = conn.self_id
            await bot.dispatcher.dispatch(parse_event(payload), conn)
            assert bool(complete.await_count) is expected
            if expected:
                assert complete.call_args.args[0][-1]["content"] == "测试用户(30003，成员)：你好"
                assert conn.nickname in complete.call_args.args[0][0]["content"]
                assert complete.call_args.kwargs["self_id"] == conn.self_id
        # Group overrides beat Bot defaults, and global switches still apply.
        await bot.permissions.save_group(123, enabled=True, config={"policy": {"auto_group": False}})
        assert agent_settings(bot, 123, 10001)["auto_group"] is False
        assert agent_settings(bot, 456, 10001)["auto_group"] is True
        second.nickname = ""
        assert effective_policy(bot, 456, 10002)["configured"] is False
        await bot.bots.update(10001, defaults={"reply_mode": "off"})
        assert agent_settings(bot, 456, 10001)["auto_group"] is False
        assert agent_settings(bot, 456, 10002)["auto_group"] is True
    finally:
        await bot.stop()


async def test_bot_model_persona_precedence_and_identity_refresh(tmp_path: Path) -> None:
    bot = await make_bot(tmp_path, llm={"enabled": True, "api_key": "fake-key", "default": "normal",
                                     "providers": {"normal": {"model": "normal"}, "other": {"model": "other"}}})
    try:
        conn = FakeConnection()
        bot.manager.add(conn)
        conn.get_login_info = AsyncMock(return_value={"user_id": 10001, "nickname": "木鱼"})
        await bot.bots.sync(conn)
        await bot.bots.update(10001, defaults={"reply_mode": "prefix", "provider": "other", "persona": "concise"})
        assert bot.chat.provider_name("group", 123, "agent", 10001) == "other"
        assert bot.chat.provider_name("group", 123, "agent", 10002) == "normal"
        assert bot.chat.persona_name("group", 123, 10001) == "concise"
        await bot.permissions.save_group(123, enabled=True, config={"llm": {"persona": "fllbot"}, "policy": {"provider": "normal"}})
        assert bot.chat.provider_name("group", 123, "agent", 10001) == "normal"
        assert bot.chat.persona_name("group", 123, 10001) == "fllbot"
        conn.get_login_info.return_value = {"user_id": 10001, "nickname": "新昵称"}
        await bot.bots.sync(conn)
        assert agent_settings(bot, 456, 10001)["trigger_prefix"] == "新昵称"
        conn.get_login_info.return_value = {"user_id": 99999, "nickname": "错误账号"}
        with pytest.raises(ValueError):
            await bot.bots.sync(conn)
        assert bot.bots.nickname(10001) == "新昵称"
        await bot.bots.update(10001, defaults={"reply_mode": "prefix", "wake_word": "助手"})
        conn.get_login_info.return_value = {"user_id": 10001, "nickname": "又改昵称"}
        await bot.bots.sync(conn)
        assert bot.bots.wake_word(10001) == "助手"
        await bot.bots.update(10001, defaults={"reply_mode": "prefix", "wake_word": ""})
        assert bot.bots.wake_word(10001) == "又改昵称"
    finally:
        await bot.stop()
