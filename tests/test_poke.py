"""poke 插件：戳一戳通知、AI 回应、自动回戳与 poke__poke 工具。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from fllbot.onebot.models import NoticeEvent, parse_event
from fllbot.plugin.context import Context
from helpers import FakeConnection, group_message, make_bot, poke_notice

ROOT = Path(__file__).resolve().parents[1]
GROUP = 20002
SELF = 10001
USER = 30003


async def setup_bot(tmp_path, monkeypatch, **settings):
    plugin_root = tmp_path / "plugins"
    plugin_root.mkdir()
    shutil.copytree(ROOT / "plugins" / "poke", plugin_root / "poke")
    bot = await make_bot(
        tmp_path,
        plugin_dirs=[plugin_root],
        llm={"enabled": True, "api_key": "fake", "context_window": 8192},
    )
    await bot.permissions.save_group(GROUP, enabled=True, config={})
    poke = bot.plugins.get("poke")
    assert poke is not None
    values = {
        "poke_reply": True,
        "poke_back": True,
        "poke_reply_seconds": 0,
        "poke_back_seconds": 0,
    }
    values.update(settings)
    await poke.settings.update(values)
    complete = AsyncMock(return_value=SimpleNamespace(text="喵", tool_calls=[]))
    monkeypatch.setattr(bot.chat, "complete", complete)
    return bot, complete


def poke_calls(connection: FakeConnection) -> list[dict]:
    return [params for action, params in connection.calls if action == "group_poke"]


async def test_poke_notice_parses_as_poke():
    event = parse_event(poke_notice())
    assert isinstance(event, NoticeEvent)
    assert event.is_poke
    assert (event.user_id, event.target_id, event.group_id) == (USER, SELF, GROUP)
    assert not parse_event(poke_notice(sub_type="lucky_king")).is_poke


async def test_poke_runs_ai_turn_and_pokes_back(tmp_path, monkeypatch):
    bot, complete = await setup_bot(tmp_path, monkeypatch)
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)

        # 自动回戳：戳的是发起者，走群接口。
        assert poke_calls(connection) == [{"group_id": GROUP, "user_id": USER}]
        # 走群聊模型回合，按人格回复；没有 message_id 时 @ 发起者。
        complete.assert_awaited_once()
        prompt = complete.call_args.args[0]
        assert prompt[-1]["content"] == "测试成员(30003)：戳了你一下"
        assert connection.last_text().strip() == "喵"

        history = await bot.storage.recent_chat("group", GROUP, self_id=SELF)
        assert [(row.role, row.content) for row in history] == [
            ("user", "戳了你一下"),
            ("assistant", "喵"),
        ]
    finally:
        await bot.stop()


async def test_poke_reply_cooldown_limits_model_turns(tmp_path, monkeypatch):
    bot, complete = await setup_bot(tmp_path, monkeypatch, poke_reply_seconds=60, poke_back_seconds=60)
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)
        await bot.dispatcher.dispatch(parse_event(poke_notice(user_id=30004)), connection)

        assert complete.await_count == 1
        # 回戳按人冷却：两位不同群友各回戳一次。
        assert poke_calls(connection) == [
            {"group_id": GROUP, "user_id": USER},
            {"group_id": GROUP, "user_id": 30004},
        ]
        assert len(connection.texts()) == 1
    finally:
        await bot.stop()


@pytest.mark.parametrize(
    "payload",
    [
        poke_notice(target_id=20002),  # 群友互戳，不是戳机器人
        poke_notice(user_id=SELF),  # 机器人自己发起的戳
        poke_notice(group_id=None),  # 私聊戳不处理
        poke_notice(sub_type="lucky_king"),  # 其他 notify 子类型
    ],
)
async def test_poke_ignores_unrelated_notices(tmp_path, monkeypatch, payload):
    bot, complete = await setup_bot(tmp_path, monkeypatch)
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(payload), connection)
        complete.assert_not_awaited()
        assert poke_calls(connection) == []
        assert connection.texts() == []
    finally:
        await bot.stop()


async def test_poke_switches_disable_reply_and_poke_back(tmp_path, monkeypatch):
    bot, complete = await setup_bot(tmp_path, monkeypatch, poke_reply=False, poke_back=False)
    try:
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)
        complete.assert_not_awaited()
        assert poke_calls(connection) == []
        assert connection.texts() == []

        # 只开回戳：戳回去但不调用模型。
        poke = bot.plugins.get("poke")
        await poke.settings.update({"poke_back": True})
        await bot.dispatcher.dispatch(parse_event(poke_notice(user_id=30004)), connection)
        assert poke_calls(connection) == [{"group_id": GROUP, "user_id": 30004}]
        complete.assert_not_awaited()
        assert connection.texts() == []
    finally:
        await bot.stop()


async def test_poke_reply_respects_group_ai_switch(tmp_path, monkeypatch):
    bot, complete = await setup_bot(tmp_path, monkeypatch)
    try:
        await bot.update_config({"llm": {"enable_group": False}})
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)
        # 群聊模型回复关闭时仍可回戳，但不调用模型。
        assert poke_calls(connection) == [{"group_id": GROUP, "user_id": USER}]
        complete.assert_not_awaited()
        assert connection.texts() == []
    finally:
        await bot.stop()


async def test_poke_reply_does_not_depend_on_agent_plugin(tmp_path, monkeypatch):
    bot, complete = await setup_bot(tmp_path, monkeypatch)
    try:
        await bot.permissions.set_plugin_enabled("agent", False)
        connection = FakeConnection()
        await bot.dispatcher.dispatch(parse_event(poke_notice()), connection)
        complete.assert_awaited_once()
        assert poke_calls(connection) == [{"group_id": GROUP, "user_id": USER}]
        assert connection.last_text().strip() == "喵"
    finally:
        await bot.stop()


async def test_poke_tool_pokes_current_scene_member(tmp_path, monkeypatch):
    bot, _ = await setup_bot(tmp_path, monkeypatch)
    try:
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message("戳他一下", at=SELF)))
        entry = bot.tools.snapshot().get("poke__poke")
        assert entry is not None
        owner, spec = entry.owner, entry.spec
        registry = {spec.qualified_name: (owner, spec)}

        output, silent = await bot.tool_executor.execute(
            ctx, "poke__poke", {"user_id": 30004}, registry, refresh=lambda _: ([], registry)
        )
        assert json.loads(output) == {"poked": 30004}
        assert silent is False
        assert poke_calls(connection) == [{"group_id": GROUP, "user_id": 30004}]
    finally:
        await bot.stop()


async def test_poke_tool_defaults_to_sender_and_rejects_self(tmp_path, monkeypatch):
    bot, _ = await setup_bot(tmp_path, monkeypatch)
    try:
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message("戳我一下", at=SELF)))
        entry = bot.tools.snapshot().get("poke__poke")
        assert entry is not None
        owner, spec = entry.owner, entry.spec
        registry = {spec.qualified_name: (owner, spec)}

        output, _ = await bot.tool_executor.execute(
            ctx, "poke__poke", {}, registry, refresh=lambda _: ([], registry)
        )
        assert json.loads(output) == {"poked": USER}

        output, _ = await bot.tool_executor.execute(
            ctx, "poke__poke", {"user_id": SELF}, registry, refresh=lambda _: ([], registry)
        )
        assert json.loads(output)["error"] == "invalid_target"
    finally:
        await bot.stop()


async def test_poke_tool_requires_addressed_turn(tmp_path, monkeypatch):
    bot, _ = await setup_bot(tmp_path, monkeypatch)
    try:
        connection = FakeConnection()
        ctx = Context(bot, connection, parse_event(group_message("他们聊得真热闹")))
        ctx.trigger_decision = SimpleNamespace(addressed=False)
        entry = bot.tools.snapshot().get("poke__poke")
        assert entry is not None
        owner, spec = entry.owner, entry.spec
        registry = {spec.qualified_name: (owner, spec)}

        output, _ = await bot.tool_executor.execute(
            ctx, "poke__poke", {"user_id": USER}, registry, refresh=lambda _: ([], registry)
        )
        assert json.loads(output)["error"] == "addressed_required"
        assert poke_calls(connection) == []
    finally:
        await bot.stop()
